"""One OpenCode server per run: OSIS-AI (T5) or stock OpenCode with none of its vendor configuration (T2, ``bare``).

Mirrors the install's start.bat. The install pack ``<install>/.agents`` is linked in as is (AGENTS.md,
commands, prompts, sub-agent config, plugins) with these substitutions:
skills = the campaign snapshot; model = the platform gateway (through the run's recorder);
MCP = the run's MCP servers (remote, through the run's proxies, not pinned); no WeKnora;
plugins listed in ``osisai.exclude_plugins`` left out. Sessions and config state live in the run dir;
long-term memory stays in ~/.osisai/memory (cleared when a campaign starts).
``bare`` runs the upstream binary (``osisai.vanilla_opencode``) with only the skills, model, MCP and an allow-all
permission, loaded the stock way (``OPENCODE_CONFIG_DIR``): no AGENTS.md, commands, prompts, plugins, sub-agents
or permission rules.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from bench.dispatch import proxy_env

PROVIDER = "bench"
START_TIMEOUT_S = 120
# Agents have killed the platform's own processes (osis-bench, by PID) twice despite INSTRUCTIONS; last match wins
KILL_GUARD = {"*": "allow", **{p: "deny" for p in ("*taskkill*", "*Stop-Process*", "*tskill*", "kill *", "pkill *")}}


def install_dir(osisai: dict[str, Any]) -> Path:
    path = Path(str(osisai.get("install_dir") or ""))
    if not (path / "opencode.exe").is_file() or not (path / ".agents").is_dir():
        raise FileNotFoundError(f"no OSIS-AI install (opencode.exe + .agents) at {path}; set osisai.install_dir")
    return path


def _link(source: Path, target: Path) -> None:
    """Directories as junctions (no admin rights needed), files as hard links; copy when linking fails."""
    if source.is_dir():
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(source), str(target))
        else:
            target.symlink_to(source, target_is_directory=True)
        return
    try:
        os.link(source, target)
    except OSError:  # e.g. another volume
        shutil.copy2(source, target)


def build_pack(request: dict[str, Any], install: Path, dest: Path, *, bare: bool = False) -> Path:
    pack = dest / "agents"
    pack.mkdir(parents=True)
    source = install / ".agents"
    excluded = set(request["osisai"].get("exclude_plugins") or [])
    for entry in [] if bare else source.iterdir():
        if entry.name in {"skills", "opencode.json"} or entry.name.startswith("opencode.json."):
            continue
        if entry.name == "plugins":
            (pack / "plugins").mkdir()
            for plugin in entry.iterdir():
                if plugin.name.split(".")[0] not in excluded:  # by name without extension: .ts, .js, .off
                    _link(plugin, pack / "plugins" / plugin.name)
        else:
            _link(entry, pack / entry.name)
    _link(Path(request["skills_dir"]), pack / "skills")

    official = json.loads((source / "opencode.json").read_text(encoding="utf-8"))
    model = request["model"]
    limits = next((m for p in official.get("provider", {}).values() for k, m in (p.get("models") or {}).items()
                   if k == model["name"]), {"name": model["name"], "reasoning": True})
    if model.get("context_tokens"):  # OpenCode compacts the session when it gets close to limit.context
        limits = {**limits, "limit": {**(limits.get("limit") or {"output": 128000}), "context": int(model["context_tokens"])}}
    options: dict[str, Any] = {"reasoningEffort": model.get("reasoning_effort") or "low"}
    if model.get("temperature") is not None:
        options["temperature"] = float(model["temperature"])
    if bare:
        official = {k: official[k] for k in ("$schema", "share") if k in official} | {"permission": {"*": "allow"}}
    official.update({
        "model": f"{PROVIDER}/{model['name']}", "small_model": f"{PROVIDER}/{model['name']}",
        "provider": {PROVIDER: {"npm": "@ai-sdk/openai-compatible", "name": "osis-bench gateway",
                                "options": {"baseURL": model["base_url"], "apiKey": f"{{env:{model['api_key_env']}}}"},
                                "models": {model["name"]: {**limits, "options": options}}}},
        # per-server timeout (ms) is also the tool-call cap; unset, the SDK's 60 s would cut a solve short
        "mcp": {name: {"type": "remote", "url": s["url"], "enabled": True, "timeout": int(request["timeout_s"]) * 1000}
                for name, s in (request.get("mcp_servers") or {}).items()},
        "permission": {k: v for k, v in (official.get("permission") or {"*": "allow"}).items()
                       if not k.startswith("weknora")} | {"bash": KILL_GUARD},
    })
    (pack / "opencode.json").write_text(json.dumps(official, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return pack


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Server:
    def __init__(self, request: dict[str, Any], *, bare: bool = False) -> None:
        self.request, self.bare = request, bare
        self.root = Path(request["run_dir"]) / "osisai"
        self.install = install_dir(request["osisai"])
        self.binary = Path(str(request["osisai"].get("vanilla_opencode") or "")) if bare else self.install / "opencode.exe"
        if not self.binary.is_file():
            raise FileNotFoundError(f"no opencode.exe at {self.binary}; set osisai.vanilla_opencode")
        self.process: subprocess.Popen | None = None
        self.url = ""

    def __enter__(self) -> Server:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def start(self) -> str:
        if self.root.exists():
            shutil.rmtree(self.root)
        pack = build_pack(self.request, self.install, self.root, bare=self.bare)
        osisai = Path.home() / ".osisai"
        venv = osisai / ".venv"
        mingit = self.install / "mingit"
        env = {**proxy_env(dict(os.environ), self.request["model"]),
               # the vendor pack is an extra layer only the OSIS fork reads; bare uses the stock config dir
               **({"OPENCODE_CONFIG_DIR": str(pack)} if self.bare else
                  {"OSIS_EXTRA_CONFIG_DIR": str(pack), "OPENCODE_CONFIG_DIR": str(self.root / "user")}),
               "XDG_CONFIG_HOME": str(self.root / "config"),
               "XDG_DATA_HOME": str(self.root / "data"),             # sessions stay in the run
               "XDG_STATE_HOME": str(self.root / "state"),
               # provider packages are cached here; stock OpenCode keeps its own default cache, not the fork's
               **({} if self.bare else {"XDG_CACHE_HOME": str(osisai / "cache")}),
               "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
               "OPENCODE_DISABLE_CLAUDE_CODE": "1", "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
               "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
               "PYTHONIOENCODING": "utf-8", "PYTHONHOME": "", "PYTHONPATH": "", "OSIS_VENV": str(venv),
               "SHELL": str(mingit / "usr" / "bin" / "sh.exe"),
               "PATH": os.pathsep.join([str(venv / "Scripts"), str(osisai / "data" / "opencode" / "bin"),
                                        str(osisai / "cache" / "opencode" / "bin"), str(mingit / "cmd"),
                                        str(mingit / "usr" / "bin"), os.environ.get("PATH", "")])}
        for name in ("OSIS_HTTP_PORT", "OSIS_URL"):  # set by OSIS for its pinned agent; not here
            env.pop(name, None)
        for name in ("user", "config", "data", "state"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        port = _free_port()
        self.url = f"http://127.0.0.1:{port}"
        log = (self.root / "server.log").open("w", encoding="utf-8")
        self.process = subprocess.Popen([str(self.binary), "serve", "--port", str(port)],
                                        cwd=self.install, env=env, stdout=log, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"OSIS-AI server exited {self.process.returncode}; see {self.root / 'server.log'}")
            try:
                if httpx.get(f"{self.url}/config", timeout=2, trust_env=False).status_code == 200:
                    return self.url
            except httpx.HTTPError:
                pass
            time.sleep(1)
        self.stop()
        raise TimeoutError(f"OSIS-AI server not ready in {START_TIMEOUT_S}s; see {self.root / 'server.log'}")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            # opencode spawns MCP children; kill the whole tree
            subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True)
        self.process = None
