from __future__ import annotations

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from bench.recorder import Recorder

USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
         "prompt_tokens_details": {"cached_tokens": 64}, "completion_tokens_details": {"reasoning_tokens": 15}}


class _Gateway(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.received.append(request)
        if request.get("stream"):
            body = (f'data: {json.dumps({"choices": [{"delta": {"reasoning_content": "hm", "tool_calls": []}}]})}\n\n'
                    f'data: {json.dumps({"choices": [{"delta": {"content": "hi"}}]})}\n\n'
                    f'data: {json.dumps({"choices": [{"finish_reason": "stop"}], "usage": USAGE})}\n\n'
                    "data: [DONE]\n\n").encode()
            kind = "text/event-stream"
        else:
            body = json.dumps({"model": "m", "choices": [{"finish_reason": "tool_calls"}], "usage": USAGE}).encode()
            kind = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def test_recorder_passes_through_and_records_usage(tmp_path: Path) -> None:
    gateway = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        with Recorder(f"http://127.0.0.1:{gateway.server_address[1]}/v1", tmp_path) as rec:
            plain = httpx.post(f"{rec.url}/chat/completions", json={"model": "m", "messages": [{}]}, trust_env=False)
            streamed = httpx.post(f"{rec.url}/chat/completions", json={"model": "m", "stream": True}, trust_env=False)
        summary = rec.summary()
    finally:
        gateway.shutdown()
    assert plain.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert "data: [DONE]" in streamed.text and '"hm"' in streamed.text
    assert "tool_calls" not in streamed.text  # empty arrays dropped for the method, kept in the record
    assert summary["calls"] == 2 and summary["failed_calls"] == 0
    assert summary["cached_tokens"] == 128 and summary["reasoning_tokens"] == 30 and summary["total_tokens"] == 240
    calls = [json.loads(line) for line in (tmp_path / "model_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [c["finish_reason"] for c in calls] == ["tool_calls", "stop"] and calls[1]["stream"]
    with gzip.open(tmp_path / "model_calls.full.jsonl.gz", "rt", encoding="utf-8") as f:
        full = [json.loads(line) for line in f]
    assert full[0]["request"]["messages"] == [{}] and "[DONE]" in full[1]["response"] and '"tool_calls": []' in full[1]["response"]


def test_recorder_adds_one_cache_key_per_run(tmp_path: Path) -> None:
    gateway = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    _Gateway.received.clear()
    try:
        with Recorder(f"http://127.0.0.1:{gateway.server_address[1]}/v1", tmp_path, cache_key="osis-k") as rec:
            for body in ({"model": "m", "messages": [{}]}, {"model": "m", "input": "x", "prompt_cache_key": "own"}):
                httpx.post(f"{rec.url}/chat/completions", json=body, trust_env=False)
    finally:
        gateway.shutdown()
    assert [r.get("prompt_cache_key") for r in _Gateway.received] == ["osis-k", "own"]  # a method's own key stays


class _Mcp(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if request["method"] == "tools/list":
            result = {"tools": [{"name": "api_read"}, {"name": "execute_python"}, {"name": "show_model"}]}
        else:
            result = {"content": [{"type": "text", "text": self.headers.get("Authorization") or "ran"}], "isError": False}
        body = f'event: message\ndata: {json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})}\n\n'
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args: object) -> None:
        pass


def test_mcp_proxy_hides_tools_refuses_and_records(tmp_path: Path) -> None:
    from bench.recorder import McpProxy

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Mcp)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    def rpc(url: str, method: str, params: dict | None = None) -> dict:
        r = httpx.post(url, json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}}, trust_env=False)
        return json.loads(next(l[5:] for l in r.text.splitlines() if l.startswith("data:")) if "data:" in r.text
                          else r.text)

    try:
        with McpProxy("osis", f"http://127.0.0.1:{upstream.server_address[1]}/mcp", tmp_path,
                      hide=["show_model"], require={"instance_id": "solver"}) as proxy:
            tools = rpc(proxy.url, "tools/list")["result"]["tools"]
            ok = rpc(proxy.url, "tools/call", {"name": "execute_python", "arguments": {"instance_id": "solver"}})
            refused = rpc(proxy.url, "tools/call", {"name": "execute_python", "arguments": {"instance_id": "default"}})
            hidden = rpc(proxy.url, "tools/call", {"name": "show_model", "arguments": {}})
        summary = proxy.summary()
    finally:
        upstream.shutdown()
    assert [t["name"] for t in tools] == ["api_read", "execute_python"]
    assert ok["result"]["content"][0]["text"] == "ran" and ok["id"] == 7
    assert refused["result"]["isError"] and "solver" in refused["result"]["content"][0]["text"]
    assert hidden["result"]["isError"]
    assert summary["calls"] == 3 and summary["refused"] == 2
    lines = (tmp_path / "mcp_calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["arguments"] == {"instance_id": "solver"} and json.loads(lines[0])["result"] == "ran"


def test_mcp_proxy_allow_list_and_upstream_headers(tmp_path: Path) -> None:
    from bench.recorder import McpProxy

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Mcp)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    def rpc(url: str, method: str, params: dict | None = None) -> dict:
        r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, trust_env=False)
        return json.loads(next(line[5:] for line in r.text.splitlines() if line.startswith("data:"))
                          if "data:" in r.text else r.text)  # a refusal is answered as plain JSON

    try:
        with McpProxy("kb", f"http://127.0.0.1:{upstream.server_address[1]}/mcp", tmp_path, allow=["api_read"],
                      headers={"Authorization": "Bearer t"}) as proxy:
            tools = rpc(proxy.url, "tools/list")["result"]["tools"]
            ok = rpc(proxy.url, "tools/call", {"name": "api_read", "arguments": {}})
            hidden = rpc(proxy.url, "tools/call", {"name": "execute_python", "arguments": {}})
    finally:
        upstream.shutdown()
    assert [t["name"] for t in tools] == ["api_read"]
    assert ok["result"]["content"][0]["text"] == "Bearer t"
    assert hidden["result"]["isError"]


def test_a_call_the_gateway_never_answers_shows_as_waiting(tmp_path: Path) -> None:
    release = threading.Event()

    class Silent(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            release.wait(10)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Silent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with Recorder(f"http://127.0.0.1:{server.server_address[1]}/v1", tmp_path) as rec:
            try:
                httpx.post(f"{rec.url}/chat/completions", json={"model": "m"}, timeout=0.5, trust_env=False)
            except httpx.TimeoutException:
                pass
            summary = rec.summary()
            assert summary["calls"] == 0 and summary["waiting_s"] >= 0.5
            release.set()
    finally:
        server.shutdown()


def test_an_unreachable_gateway_is_recorded_and_answered_502(tmp_path: Path) -> None:
    import socket

    with socket.socket() as s:  # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with Recorder(f"http://127.0.0.1:{port}/v1", tmp_path) as rec:
        r = httpx.post(f"{rec.url}/chat/completions", json={"model": "m"}, timeout=30, trust_env=False)
        summary = rec.summary()
    assert r.status_code == 502 and summary["calls"] == 1 and summary["last_error"].startswith("ConnectError")


def test_recorder_waits_out_a_429(tmp_path: Path, monkeypatch) -> None:
    import bench.recorder as recorder

    monkeypatch.setattr(recorder, "RATE_LIMIT_WAITS", (0.01, 0.01))
    hits = []

    class Limited(_Gateway):
        def do_POST(self) -> None:
            hits.append(1)
            if len(hits) <= 2:  # the first two attempts are rate limited
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            super().do_POST()

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), Limited)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        with Recorder(f"http://127.0.0.1:{gateway.server_address[1]}/v1", tmp_path) as rec:
            reply = httpx.post(f"{rec.url}/chat/completions", json={"model": "m", "messages": [{}]}, trust_env=False)
    finally:
        gateway.shutdown()
    assert reply.status_code == 200 and len(hits) == 3
