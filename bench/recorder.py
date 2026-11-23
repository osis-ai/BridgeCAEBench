"""Per-run pass-through proxies that record what a method sends out.

``Recorder``: the model gateway. The method's ``base_url`` points here, so every framework is measured
the same way, whatever it reports itself (any path is forwarded: chat/completions, responses, messages).
Writes ``model_calls.jsonl`` (timing, status, full usage incl. cached and reasoning tokens) and
``model_calls.full.jsonl.gz`` (full request and response bodies).

``McpProxy``: one MCP server (streamable HTTP). Hides tools, enforces argument rules, and writes every
``tools/call`` to ``mcp_calls.jsonl`` (full arguments and result, duration).
"""

from __future__ import annotations

import contextlib
import gzip
import itertools
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

_HOP = {"host", "content-length", "connection", "transfer-encoding", "keep-alive", "accept-encoding",
        "content-encoding", "proxy-connection"}


RATE_LIMIT_WAITS = (3, 6, 12, 24, 45, 60)  # seconds between retries of a 429


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _messages(text: str) -> list[Any]:
    """JSON bodies of a JSON or SSE response."""
    lines = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    out = []
    for raw in lines or [text]:
        if raw and raw != "[DONE]":
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    return out


class _Proxy:
    """Forward every request to ``upstream``; subclasses inspect, rewrite or answer it themselves."""

    buffered = False  # True: read the whole upstream response before answering (needed to rewrite it)

    def __init__(self, upstream: str, run_dir: Path, *, bypass_proxy: bool = True,
                 headers: dict[str, str] | None = None) -> None:
        self.upstream = urlparse(upstream)
        self.run_dir = run_dir
        self.headers = dict(headers or {})  # added to every upstream request (e.g. the server's auth token)
        self.client = httpx.Client(trust_env=not bypass_proxy, timeout=httpx.Timeout(None))
        self.lock = threading.Lock()
        self.open: dict[int, float] = {}  # requests still waiting on the upstream: id -> monotonic start
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                proxy._forward(self)

            def do_POST(self) -> None:
                proxy._forward(self)

            def do_DELETE(self) -> None:
                proxy._forward(self)

            def log_message(self, *args: Any) -> None:
                pass

        class Server(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                pass  # a client dropping its connection (method killed, OpenCode shut down) is not a proxy error

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}{self.upstream.path}"

    def __enter__(self) -> _Proxy:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.client.close()

    # hooks
    def intercept(self, request: Any) -> dict[str, Any] | None:
        """A JSON body to answer with instead of forwarding, or None."""
        return None

    def outgoing(self, request: Any) -> dict[str, Any] | None:
        """A changed JSON body to forward instead of the method's, or None."""
        return None

    def rewrite(self, request: Any, text: str) -> str:
        return text

    def relay(self, line: bytes) -> bytes:
        """One line of a streamed answer, as passed on to the method (the record keeps the upstream's)."""
        return line

    def record(self, request: Any, body: bytes, text: str, meta: dict[str, Any]) -> None:
        pass

    def _send(self, handler: BaseHTTPRequestHandler, status: int, headers: dict[str, str], data: bytes) -> None:
        handler.send_response(status)
        for k, v in headers.items():
            handler.send_header(k, v)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    @contextlib.contextmanager
    def _open(self, method: str, target: str, body: bytes, headers: dict[str, str]) -> Any:
        """The upstream's answer; a 429 (rate limit) is waited out and retried, up to ~3 min, before the method sees it."""
        for wait in (*RATE_LIMIT_WAITS, None):
            response = self.client.send(self.client.build_request(method, target, content=body, headers=headers), stream=True)
            if response.status_code != 429 or wait is None:
                break
            response.close()
            time.sleep(wait)
        try:
            yield response
        finally:
            response.close()

    def _forward(self, handler: BaseHTTPRequestHandler) -> None:
        body = handler.rfile.read(int(handler.headers.get("Content-Length") or 0))
        try:
            request = json.loads(body) if body else {}
        except json.JSONDecodeError:
            request = {"_raw": body.decode("utf-8", "replace")}
        meta: dict[str, Any] = {"started_at": _now(), "path": handler.path, "status": 0, "content_type": "",
                                "ttfb_s": None, "error": None}
        t0 = time.monotonic()
        answer = self.intercept(request)
        if answer is not None:
            text = json.dumps(answer, ensure_ascii=False)
            self._send(handler, 200, {"Content-Type": "application/json"}, text.encode("utf-8"))
            meta.update(status=200, content_type="application/json", intercepted=True,
                        duration_s=round(time.monotonic() - t0, 3), response_bytes=len(text))
            self.record(request, body, text, meta)
            return
        if (changed := self.outgoing(request)) is not None:
            request, body = changed, json.dumps(changed, ensure_ascii=False).encode("utf-8")
        target = f"{self.upstream.scheme}://{self.upstream.netloc}{handler.path}"
        headers = {**{k: v for k, v in handler.headers.items() if k.lower() not in _HOP}, **self.headers}
        chunks: list[bytes] = []
        with self.lock:
            self.open[id(meta)] = t0
        try:
            with self._open(handler.command, target, body, headers) as response:
                meta.update(status=response.status_code, content_type=response.headers.get("content-type", ""))
                out_headers = {k: v for k, v in response.headers.items() if k.lower() not in _HOP}
                if self.buffered:
                    chunks = list(response.iter_bytes())
                    meta["ttfb_s"] = round(time.monotonic() - t0, 3)
                else:
                    handler.send_response(response.status_code)
                    for k, v in out_headers.items():
                        handler.send_header(k, v)
                    handler.end_headers()
                    pending = b""  # relay() sees whole lines; a chunk may end mid-line
                    for chunk in itertools.chain(response.iter_bytes(), [None]):
                        if chunk is not None:
                            if meta["ttfb_s"] is None:
                                meta["ttfb_s"] = round(time.monotonic() - t0, 3)
                            chunks.append(chunk)
                            *lines, pending = (pending + chunk).split(b"\n")
                            out = b"".join(self.relay(line) + b"\n" for line in lines)
                        else:
                            out = self.relay(pending) if pending else b""
                        try:
                            if out:
                                handler.wfile.write(out)
                                handler.wfile.flush()
                        except OSError as exc:  # client went away (e.g. method killed at timeout)
                            meta["error"] = f"client disconnected: {exc}"
                            break
        except httpx.HTTPError as exc:
            meta["error"] = f"{type(exc).__name__}: {exc}"
            if not meta["status"]:
                # the status line is latin-1 only (Windows errors come localized): the reason goes in the body
                handler.send_error(502, "upstream unreachable", meta["error"][:200])
        finally:
            with self.lock:
                self.open.pop(id(meta), None)
        text = b"".join(chunks).decode("utf-8", "replace")
        if self.buffered and meta["status"]:
            text = self.rewrite(request, text)
            self._send(handler, meta["status"], out_headers, text.encode("utf-8"))
        meta.update(duration_s=round(time.monotonic() - t0, 3), response_bytes=len(text.encode("utf-8")))
        self.record(request, body, text, meta)


def usage_of(usage: dict[str, Any] | None) -> dict[str, int]:
    """OpenAI-style usage, flattened; cached/reasoning are 0 when the gateway does not report them."""
    u = usage or {}
    prompt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    completion = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    details_in = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    details_out = u.get("completion_tokens_details") or u.get("output_tokens_details") or {}
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": int(u.get("total_tokens") or prompt + completion),
            "cached_tokens": int(details_in.get("cached_tokens") or u.get("prompt_cache_hit_tokens")
                                 or u.get("cache_read_input_tokens") or 0),
            "cache_write_tokens": int(details_in.get("cache_write_tokens") or u.get("cache_creation_input_tokens")
                                      or u.get("prompt_cache_write_tokens") or 0),
            "reasoning_tokens": int(details_out.get("reasoning_tokens") or 0)}


SLOW_GATEWAY_S = 240  # a gateway this slow to start answering outlasts the methods' request timeouts (300 s and up)


def network_failure(gateway: dict[str, Any]) -> str | None:
    """Why the last LLM call failed on the gateway's side (rate limit, server error, no connection, an answer so
    slow the method's client gave up), else None.

    A method that fails right after such a call failed because of the network, not by itself."""
    status, error = gateway.get("last_status"), gateway.get("last_error")
    if status == 429 or (status or 0) >= 500:
        return f"gateway returned {status}"
    if status == 400 and "hosted outside" in str(error):  # the gateway's account region switch, nothing in the request
        return "gateway rejected the region (400)"
    if (gateway.get("waiting_s") or 0) >= SLOW_GATEWAY_S:  # the method gave up on a call the gateway never answered
        return f"gateway had not answered after {gateway['waiting_s']:.0f} s"
    if error and (gateway.get("last_ttfb_s") or 0) >= SLOW_GATEWAY_S:
        return f"gateway took {gateway['last_ttfb_s']:.0f} s to answer ({error})"
    if status in (400, 413):  # the method's own request was bad, such as a context grown past the model's limit
        return None
    if error and not str(error).startswith("client disconnected"):  # the method going away is the method's doing
        return str(error)
    return None


class Recorder(_Proxy):
    def __init__(self, gateway_url: str, run_dir: Path, *, bypass_proxy: bool = True, cache_key: str = "") -> None:
        super().__init__(gateway_url, run_dir, bypass_proxy=bypass_proxy)
        self.calls: list[dict[str, Any]] = []
        self.cache_key = cache_key

    def outgoing(self, request: Any) -> dict[str, Any] | None:
        # a gateway that spreads calls over upstream accounts hits the prompt cache only with a sticky key; set
        # here, one per run, so every method gets it alike
        if self.cache_key and isinstance(request, dict) and "model" in request and "prompt_cache_key" not in request:
            return {**request, "prompt_cache_key": self.cache_key}
        return None

    def relay(self, line: bytes) -> bytes:
        # some gateways send "tool_calls": [] in every delta; OpenCode's SDK then closes the reasoning at each one
        # and stores it a token per part, which its compaction later sends whole (opencode#47875, vercel/ai#20203)
        if not line.startswith(b"data: {") or b'"tool_calls"' not in line:
            return line
        try:
            event = json.loads(line[6:])
        except json.JSONDecodeError:
            return line
        empty = [c["delta"] for c in event.get("choices") or [] if (c.get("delta") or {}).get("tool_calls") == []]
        for delta in empty:
            del delta["tool_calls"]
        return b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8") if empty else line

    def record(self, request: Any, body: bytes, text: str, meta: dict[str, Any]) -> None:
        request = request if isinstance(request, dict) else {}
        usage = finish = served = error = None
        for chunk in _messages(text):
            if isinstance(chunk, dict):
                usage = chunk.get("usage") or usage
                served = chunk.get("model") or served
                error = chunk.get("error") or error
                for choice in chunk.get("choices") or []:
                    finish = choice.get("finish_reason") or finish
                finish = chunk.get("stop_reason") or finish  # Anthropic-style
        call = {
            "started_at": meta["started_at"], "path": meta["path"], "status": meta["status"],
            "stream": bool(request.get("stream")) or "event-stream" in meta["content_type"],
            "model": request.get("model"), "served_model": served,
            "messages": len(request.get("messages") or request.get("input") or []),
            "tools": len(request.get("tools") or []),
            "request_bytes": len(body), "response_bytes": meta["response_bytes"],
            "ttfb_s": meta["ttfb_s"], "duration_s": meta["duration_s"], "finish_reason": finish,
            "usage": usage_of(usage), "usage_raw": usage,
            "error": meta["error"] or (str(error)[:500] if error else None),
        }
        with self.lock:
            call["i"] = len(self.calls)
            self.calls.append(call)
            with (self.run_dir / "model_calls.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(call, ensure_ascii=False) + "\n")
            with gzip.open(self.run_dir / "model_calls.full.jsonl.gz", "at", encoding="utf-8") as f:
                f.write(json.dumps({"i": call["i"], "request": request, "response": text}, ensure_ascii=False) + "\n")

    def summary(self) -> dict[str, Any]:
        with self.lock:
            calls = list(self.calls)
            waiting = max((time.monotonic() - t for t in self.open.values()), default=0.0)
        total = {k: sum(c["usage"][k] for c in calls) for k in
                 ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_write_tokens",
                  "reasoning_tokens")}
        ttfb = [c["ttfb_s"] for c in calls if c["ttfb_s"] is not None]
        return {"calls": len(calls), "failed_calls": sum(1 for c in calls if c["status"] != 200 or c["error"]),
                "last_status": calls[-1]["status"] if calls else None, "last_error": calls[-1]["error"] if calls else None,
                "last_ttfb_s": calls[-1]["ttfb_s"] if calls else None,
                **total, "model_time_s": round(sum(c["duration_s"] for c in calls), 3),
                "mean_ttfb_s": round(sum(ttfb) / len(ttfb), 3) if ttfb else None, "waiting_s": round(waiting, 3)}


class McpProxy(_Proxy):
    """``hide``: tool names removed from tools/list and refused in tools/call; ``allow``: if set, only these
    tools are kept. ``require``: {argument: value} that every call carrying that argument must use
    (e.g. instance_id=solver)."""

    buffered = True

    def __init__(self, name: str, url: str, run_dir: Path, *, hide: list[str] | None = None,
                 allow: list[str] | None = None, require: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(url, run_dir, bypass_proxy=True, headers=headers)  # MCP servers here are local
        self.name, self.hide, self.require = name, set(hide or []), dict(require or {})
        self.allow = set(allow) if allow else None
        self.calls: list[dict[str, Any]] = []

    def intercept(self, request: Any) -> dict[str, Any] | None:
        if not (isinstance(request, dict) and request.get("method") == "tools/call"):
            return None
        params = request.get("params") or {}
        tool, args = params.get("name"), params.get("arguments") or {}
        reason = f"tool {tool!r} is not available in this benchmark" if self.hidden(tool) else next(
            (f"{k} must be {v!r} in this benchmark (got {args[k]!r})"
             for k, v in self.require.items() if k in args and args[k] != v), None)
        if reason is None:
            return None
        return {"jsonrpc": "2.0", "id": request.get("id"),
                "result": {"content": [{"type": "text", "text": f"Refused: {reason}"}], "isError": True}}

    def hidden(self, tool: Any) -> bool:
        return tool in self.hide or (self.allow is not None and tool not in self.allow)

    def rewrite(self, request: Any, text: str) -> str:
        if not (isinstance(request, dict) and request.get("method") == "tools/list"
                and (self.hide or self.allow is not None)):
            return text
        sse = any(line.startswith("data:") for line in text.splitlines())
        out = []
        for message in _messages(text):
            tools = ((message.get("result") or {}).get("tools") if isinstance(message, dict) else None)
            if isinstance(tools, list):
                message["result"]["tools"] = [t for t in tools if not self.hidden(t.get("name"))]
            out.append(json.dumps(message, ensure_ascii=False))
        return "".join(f"event: message\ndata: {m}\n\n" for m in out) if sse else (out[0] if out else text)

    def record(self, request: Any, body: bytes, text: str, meta: dict[str, Any]) -> None:
        if not (isinstance(request, dict) and request.get("method") == "tools/call"):
            return
        params = request.get("params") or {}
        result = next((m.get("result") for m in _messages(text) if isinstance(m, dict) and "result" in m), None) or {}
        content = "".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
        call = {"started_at": meta["started_at"], "server": self.name, "tool": params.get("name"),
                "arguments": params.get("arguments"), "duration_s": meta["duration_s"], "status": meta["status"],
                "is_error": bool(result.get("isError")), "refused": bool(meta.get("intercepted")),
                "result_chars": len(content), "result": content, "error": meta["error"]}
        with self.lock:
            self.calls.append(call)
            with (self.run_dir / "mcp_calls.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(call, ensure_ascii=False) + "\n")

    def summary(self) -> dict[str, Any]:
        with self.lock:
            calls = list(self.calls)
        return {"calls": len(calls), "errors": sum(1 for c in calls if c["is_error"] or c["error"]),
                "refused": sum(1 for c in calls if c["refused"]),
                "tool_s": round(sum(c["duration_s"] for c in calls), 3),
                "by_tool": {t: sum(1 for c in calls if c["tool"] == t) for t in sorted({c["tool"] for c in calls})}}
