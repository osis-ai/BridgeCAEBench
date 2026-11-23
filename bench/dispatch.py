"""Run one method adapter in its own Python process."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bench import weknora_mcp
from bench.config import ROOT
from bench.parent import Parent
from bench.recorder import McpProxy, Recorder

LAUNCHERS = {"weknora": weknora_mcp.serve}


class DispatchError(RuntimeError):
    """Infrastructure failure: the run is retried on the next invocation, not scored."""


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def proxy_env(env: dict[str, str], model: dict[str, Any]) -> dict[str, str]:
    """Talk to the gateway and localhost directly when ``model.bypass_proxy`` is set.

    ALL_PROXY goes too: this machine sets a socks5h:// one, and httpx builds its proxy
    transport at client creation, so NO_PROXY alone still crashes a client without socksio.
    """
    gateway = urlparse(str(model.get("base_url") or "")).hostname
    if not (gateway and model.get("bypass_proxy")):
        return env
    env = {k: v for k, v in env.items() if k.lower() != "all_proxy"}
    for var in ("NO_PROXY", "no_proxy"):
        hosts = {h.strip() for h in env.get(var, "").split(",") if h.strip()}
        env[var] = ",".join(sorted(hosts | {gateway, "127.0.0.1", "localhost", "::1"}))
    return env


def method_python(spec: dict[str, Any], parent: Parent) -> Path:
    kind = spec["python"]
    if kind == "main":
        return Path(sys.executable)
    path = parent.python if kind == "parent" else venv_python(ROOT / ".venvs" / spec["extra"])
    if not path.is_file():
        hint = "create the parent repo .venv" if kind == "parent" else f"run: osis-bench env --method {spec['id']}"
        raise DispatchError(f"{spec['id']} python missing at {path}; {hint}")
    return path


def dispatch(spec: dict[str, Any], request: dict[str, Any], parent: Parent,
             mcp: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run one method with the model gateway and each MCP server behind a per-run recording proxy.

    The method sees ``request["model"]["base_url"]`` and ``request["mcp_servers"][name]["url"]`` pointing at
    the proxies; ``mcp`` is the platform config of the servers the task mounts (url or launch, hide, allow, require).
    """
    run_dir, model = Path(request["run_dir"]), request.get("model") or {}
    with ExitStack() as stack:
        key = "osis-" + hashlib.sha1(str(run_dir).encode("utf-8")).hexdigest()[:16] if model.get("prompt_cache_key") else ""
        rec = stack.enter_context(Recorder(model["base_url"], run_dir, bypass_proxy=bool(model.get("bypass_proxy")),
                                           cache_key=key))
        servers = {}
        for name, cfg in (mcp or {}).items():
            # launch: the platform starts the server for this run; otherwise cfg["url"] is already running
            url, headers = stack.enter_context(LAUNCHERS[cfg["launch"]](cfg, run_dir)) if cfg.get("launch")                 else (cfg["url"], {})
            servers[name] = stack.enter_context(McpProxy(name, url, run_dir, hide=cfg.get("hide"),
                                                         allow=cfg.get("allow"), require=cfg.get("require"),
                                                         headers=headers))
        result = _dispatch(spec, {**request, "model": {**model, "base_url": rec.url, "gateway_url": model["base_url"]},
                                  "mcp_servers": {n: {"url": p.url, "transport": "http"} for n, p in servers.items()}},
                           parent)
    result["gateway"] = rec.summary()
    result["mcp"] = {name: proxy.summary() for name, proxy in servers.items()}
    # tool_s = harness tools timed in the method process + MCP calls timed here
    result["tool_s"] = round(float(result.get("tool_s") or 0.0) + sum(s["tool_s"] for s in result["mcp"].values()), 3)
    return result


def _dispatch(spec: dict[str, Any], request: dict[str, Any], parent: Parent) -> dict[str, Any]:
    run_dir = Path(request["run_dir"])
    request_path = run_dir / "request.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    record = run_dir / "generation.json"
    record.unlink(missing_ok=True)
    command = [str(method_python(spec, parent)), "-m", spec["module"], "--request", str(request_path)]
    env = proxy_env({**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8",
                     "BENCH_WORK_DIR": str(request.get("work_dir") or ""), "BENCH_RUN_DIR": str(run_dir)}, request.get("model") or {})
    with (run_dir / "method.log").open("w", encoding="utf-8") as log:
        try:
            completed = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       timeout=float(request["timeout_s"]))
        except subprocess.TimeoutExpired:
            # Wall-time limit is part of the protocol: the method is scored as a timeout, not retried.
            return {"method": spec["id"], "status": "failed", "stop_reason": "timeout",
                    "error": f"TimeoutError: method exceeded {request['timeout_s']}s",
                    "elapsed_s": float(request["timeout_s"])}
    if not record.is_file():
        raise DispatchError(f"{spec['id']} exited {completed.returncode} without generation.json; see method.log")
    return json.loads(record.read_text(encoding="utf-8"))
