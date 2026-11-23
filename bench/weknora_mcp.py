"""One WeKnora MCP server per run, with the template knowledge base cut off.

``serve(cfg, run_dir)`` starts the official ``weknora_mcp_server`` (streamable HTTP, random bearer token)
in the Python that has it (OSIS-AI's venv) by running this file as a script. As a script it patches the
server's WeKnora client before serving, so every tool goes through ``guard``: knowledge bases named in
``deny_kbs`` are dropped from listings, and any request that touches one (by id in the path or the body,
by name through resolution, or by a knowledge entry that belongs to it) is refused. Fails closed: a deny
name that matches no knowledge base, or an entry whose owner cannot be looked up, stops the request.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

START_TIMEOUT_S = 60
_ENTRY = re.compile(r"^/(?:knowledge|chunks)/([0-9a-fA-F-]{36})")


class Refused(PermissionError):
    pass


def guard(request: Callable[..., Any], deny_names: list[str]) -> Callable[..., Any]:
    """Wrap ``WeKnoraClient._request(method, endpoint, **kwargs)``."""
    listing = request("GET", "/knowledge-bases").get("data") or []
    deny = {kb["id"] for kb in listing if kb.get("name") in deny_names or kb.get("id") in deny_names}
    missing = set(deny_names) - {kb.get("name") for kb in listing} - {kb.get("id") for kb in listing}
    if missing:
        raise SystemExit(f"deny_kbs not found in WeKnora: {sorted(missing)}")
    owners: dict[str, str] = {}

    def owner(entry: str) -> str:
        if entry not in owners:
            owners[entry] = str((request("GET", f"/knowledge/{entry}").get("data") or {})["knowledge_base_id"])
        return owners[entry]

    def guarded(method: str, endpoint: str, **kwargs: Any) -> Any:
        sent = endpoint + json.dumps(kwargs, ensure_ascii=False, default=str)
        entry = _ENTRY.match(endpoint)
        if any(kb in sent for kb in deny) or (entry and owner(entry.group(1)) in deny):
            raise Refused("this knowledge base is not available in this benchmark")
        data = request(method, endpoint, **kwargs)
        if method == "GET" and endpoint.rstrip("/") == "/knowledge-bases" and isinstance(data, dict):
            data = {**data, "data": [kb for kb in data.get("data") or [] if kb.get("id") not in deny]}
        return data

    return guarded


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def serve(cfg: dict[str, Any], run_dir: Path) -> Iterator[tuple[str, dict[str, str]]]:
    """Yield (MCP url, headers the proxy must add) for a server that lives as long as the block."""
    python = Path(os.path.expanduser(str(cfg["python"])))
    key = os.environ.get(cfg.get("api_key_env") or "OSIS_WEKNORA_API_KEY", "").strip()
    if not python.is_file():
        raise FileNotFoundError(f"mcp weknora python not found: {python}")
    if not key:
        raise ValueError(f"WeKnora key missing: set {cfg.get('api_key_env') or 'OSIS_WEKNORA_API_KEY'}")
    from bench.dispatch import proxy_env  # here: this file also runs as a script in the OSIS-AI venv

    token, port = secrets.token_urlsafe(24), _free_port()
    # weknora_mcp >= 1.0.6 reads OSIS_WEKNORA_*; older versions read WEKNORA_*
    env = {**proxy_env(dict(os.environ), cfg), "WEKNORA_API_KEY": key, "WEKNORA_BASE_URL": str(cfg["base_url"]),
           "OSIS_WEKNORA_API_KEY": key, "OSIS_WEKNORA_BASE_URL": str(cfg["base_url"]),
           "MCP_SERVER_AUTH_TOKEN": token, "BENCH_DENY_KBS": json.dumps(cfg.get("deny_kbs") or [], ensure_ascii=False),
           "PYTHONIOENCODING": "utf-8"}
    log = (run_dir / "weknora_server.log").open("w", encoding="utf-8")
    process = subprocess.Popen([str(python), "-P", __file__, "--port", str(port)], env=env, stdout=log,
                               stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + START_TIMEOUT_S
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"WeKnora MCP server exited {process.returncode}; see {run_dir / 'weknora_server.log'}")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"WeKnora MCP server not up in {START_TIMEOUT_S}s") from None
                time.sleep(0.5)
        yield f"http://127.0.0.1:{port}/mcp", {"Authorization": f"Bearer {token}"}
    finally:
        process.kill()
        process.wait()
        log.close()


def _main() -> None:
    """Script mode, inside the Python that has weknora_mcp_server."""
    import asyncio

    import weknora_mcp_server as server

    port = int(sys.argv[sys.argv.index("--port") + 1])
    server.client._request = guard(server.client._request, json.loads(os.environ["BENCH_DENY_KBS"]))
    asyncio.run(server.run_http("127.0.0.1", port))


if __name__ == "__main__":
    _main()
