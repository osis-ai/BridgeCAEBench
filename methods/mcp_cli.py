"""A minimal MCP client (streamable HTTP, JSON-RPC) and the ``osis-mcp`` command line built on it.

Methods without an MCP client (T1 mini-SWE-agent's bash) or with a fixed flow (T3, T4's gate) reach the
run's MCP servers here. The URLs are the run's platform proxies, so hide/require and mcp_calls.jsonl apply.

    osis-mcp list-tools [server]
    osis-mcp call <server> <tool> '<json-args>'

Servers come from the env var ``OSIS_MCP_SERVERS`` ({name: url}).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

PROTOCOL = "2025-03-26"


class Session:
    def __init__(self, url: str, timeout_s: float = 600) -> None:
        self.url, self.timeout_s, self.id, self.sid = url, timeout_s, 0, ""

    def _post(self, body: dict[str, Any]) -> dict[str, Any] | None:
        headers = {"Accept": "application/json, text/event-stream"}
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        response = httpx.post(self.url, json=body, headers=headers, timeout=self.timeout_s + 30, trust_env=False)
        response.raise_for_status()
        self.sid = response.headers.get("mcp-session-id", self.sid)
        if "id" not in body:  # a notification: no reply
            return None
        data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
        for raw in data or [response.text]:
            message = json.loads(raw)
            if isinstance(message, dict) and message.get("id") == body["id"]:
                if "error" in message:
                    raise RuntimeError(f"MCP {body['method']}: {message['error']}")
                return message.get("result") or {}
        raise RuntimeError(f"MCP {body['method']}: no reply")

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.sid and method != "initialize":
            self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                        "clientInfo": {"name": "osis-mcp", "version": "1"}})
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.id += 1
        return self._post({"jsonrpc": "2.0", "id": self.id, "method": method, "params": params}) or {}

    def list_tools(self) -> list[dict[str, Any]]:
        return list(self.request("tools/list", {}).get("tools") or [])

    def call(self, tool: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """(text content, is_error)."""
        result = self.request("tools/call", {"name": tool, "arguments": arguments})
        text = "".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
        return text, bool(result.get("isError"))


def servers() -> dict[str, str]:
    return json.loads(os.environ.get("OSIS_MCP_SERVERS") or "{}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    known = servers()
    usage = "usage: osis-mcp list-tools [server] | osis-mcp call <server> <tool> '<json-args>'"
    if not args or args[0] not in {"list-tools", "call"}:
        print(usage, file=sys.stderr)
        return 2
    names = args[1:2] or sorted(known)
    unknown = [n for n in names if n not in known]
    if unknown:
        print(f"unknown server {unknown[0]!r}; servers: {sorted(known)}", file=sys.stderr)
        return 2
    timeout = float(os.environ.get("OSIS_MCP_TIMEOUT_S") or 600)
    if args[0] == "list-tools":
        for name in names:
            for tool in Session(known[name], timeout).list_tools():
                schema = json.dumps((tool.get("inputSchema") or {}).get("properties") or {}, ensure_ascii=False)
                print(f"[{name}] {tool['name']}: {tool.get('description') or ''}\n    args: {schema}")
        return 0
    if len(args) < 3:
        print(usage, file=sys.stderr)
        return 2
    try:
        arguments = json.loads(args[3]) if len(args) > 3 else {}
    except json.JSONDecodeError as exc:
        print(f"json-args is not valid JSON: {exc}", file=sys.stderr)
        return 2
    text, is_error = Session(known[args[1]], timeout).call(args[2], arguments)
    print(text)
    return 1 if is_error else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    raise SystemExit(main())
