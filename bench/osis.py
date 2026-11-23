"""OSIS through the local OSIS Broker, for the platform's own checks (scoring rebuild + solve).

Methods reach the Broker as a plain MCP server (``mcp.osis`` in the config), through a per-run proxy.
Scripts here run in the Broker's solver-only mode: a fresh solver per call that opens or creates
``<project>.sis`` and saves it on success, so runs never touch a user's OSIS window.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any

import httpx

SCRIPT_S = 600  # Broker default for one execute_python call; long solves pass their own (osis.solve_timeout_s)
# Machine trouble, not a model error: out of memory, solver crash, timeout. Retry later, never cache or judge.
TRANSIENT = ("超时", "bad allocation", "Memory allocation", "PySolver_unhandled")
_HEAVY: MachineSlots | None = None
_HEAVY_LOCK = threading.Lock()


def work_dir(osis_cfg: dict[str, Any]) -> Path:
    """Where temporary solve projects go (``osis.work_dir``): a big model writes GBs of results."""
    path = Path(osis_cfg["work_dir"]).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def transient(error: str | None) -> bool:
    return any(t in (error or "") for t in TRANSIENT)


class MachineSlots:
    """``n`` slots shared by every thread and process on this machine (one lock file each in ``lock_dir``).
    ``acquire``/``release`` are called from the same thread, like a semaphore's."""

    def __init__(self, n: int, lock_dir: Path, name: str = "heavy") -> None:
        self.paths = [lock_dir / f"osis-bench-{name}-{i}.lock" for i in range(max(1, n))]
        self.held = threading.local()

    def _try(self, path: Path) -> Any:
        f = path.open("a+b")
        try:
            f.seek(0)  # one byte at offset 0 (msvcrt locks from the current position)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            f.close()
            return None

    def acquire(self, blocking: bool = True) -> bool:
        """``blocking=False`` (the runner, which retries in its own loop) also yields while a blocking caller waits:
        the runner frees a slot and takes it again within milliseconds, so a 2 s poller would never get one.
        A blocking holder marks again on release: a tool solving case after case (reference, then start) frees
        the slot for a few seconds in between, and the runner must not take it then."""
        wait = self.paths[0].with_suffix(".wait")
        while True:
            if blocking or not (wait.is_file() and time.time() - wait.stat().st_mtime < 60):
                for path in self.paths:
                    if (f := self._try(path)) is not None:
                        self.held.f, self.held.blocking = f, blocking
                        if blocking:
                            wait.unlink(missing_ok=True)
                        return True
            if not blocking:
                return False
            wait.touch()
            time.sleep(2)

    def release(self) -> None:
        f = self.held.f
        f.close()  # closing the handle drops its lock
        self.held.f = None
        if self.held.blocking:
            self.paths[0].with_suffix(".wait").touch()

    def __enter__(self) -> MachineSlots:
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def heavy_slots(osis_cfg: dict[str, Any], bridge: str) -> MachineSlots | None:
    """The ``osis.heavy.jobs`` slots bridges in ``osis.heavy.bridges`` (big models, 5-11 GB of RAM each) share
    across threads and campaigns on this machine, or None for other bridges (limited only by the caller's pool)."""
    global _HEAVY
    heavy = osis_cfg.get("heavy") or {}
    if bridge not in (heavy.get("bridges") or []):
        return None
    with _HEAVY_LOCK:
        if _HEAVY is None:
            _HEAVY = MachineSlots(int(heavy.get("jobs") or 1), Path(heavy.get("lock_dir") or tempfile.gettempdir()))
    return _HEAVY


def slot(osis_cfg: dict[str, Any], bridge: str) -> AbstractContextManager:
    """Hold while solving: one of ``heavy_slots`` for a big bridge, nothing otherwise."""
    return heavy_slots(osis_cfg, bridge) or nullcontext()


class BrokerError(RuntimeError):
    """The Broker itself failed (not running, protocol error): infrastructure, not a model mistake."""


def call(tool: str, arguments: dict[str, Any], broker: str, timeout_s: float = 60) -> dict[str, Any]:
    url = broker.rstrip("/") + "/mcp"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    try:
        response = httpx.post(url, json=body, timeout=timeout_s + 30, trust_env=False,
                              headers={"Accept": "application/json, text/event-stream"})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise BrokerError(f"OSIS Broker unreachable at {url}: {type(exc).__name__}: {exc}") from exc
    data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
    message = json.loads(data[-1] if data else response.text)
    if "error" in message:
        raise BrokerError(f"Broker {tool}: {message['error']}")
    text = message["result"]["content"][0]["text"]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"ok": not message["result"].get("isError"), "text": text}


def run_script(script: Path, broker: str, timeout_s: int = SCRIPT_S) -> dict[str, Any]:
    """Run one script under ``<project>/py/`` in solver mode. Raises BrokerError only for infrastructure."""
    timeout_s = max(1, int(timeout_s))
    return call("execute_python", {"instance_id": "solver", "file": str(script), "cwd": str(script.parent),
                                   "timeout": timeout_s}, broker, timeout_s)


def pyosis_version(python: str) -> str:
    """Installed osis-python version in the Broker's environment (``osis.python``), or "unknown"."""
    site = Path(python).expanduser().parents[1] / "Lib" / "site-packages"
    found = sorted(d.name.split("-")[1].removesuffix(".dist") for d in site.glob("osis_python-*.dist-info"))
    return found[-1] if found else "unknown"


_SOLVE = "from pyosis import OSISEngine\n\nengine = OSISEngine()\nengine.solve(timeout={timeout})\nprint(engine.model_summary())\n"


_CHECK = """import json
import runpy
from pathlib import Path

from pyosis import OSISEngine

runpy.run_path(str(Path(__file__).parent / "post" / "main.py"), run_name="__main__")
tables = OSISEngine().result.check_all()
results = {name: sorted(set(df["结果"].astype(str))) if "结果" in df else [] for name, df in tables.items()}
Path(__file__).with_name("_bench_check.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
"""


def ng_items(tables: dict[str, list[str]]) -> list[str]:
    """NG check items, such as "PC正截面短期抗裂验算", from {table name: values of its 结果 column}."""
    # table name: <material or load group>_<check item>_<envelope>
    return sorted({name.split("_")[1] for name, values in tables.items() if "NG" in values})


def check(py: Path, broker: str, timeout_s: int = SCRIPT_S) -> tuple[str | None, list[str], dict[str, Any]]:
    """Run ``py/post/main.py`` (combinations + code checks) on the solved project.
    Returns (error or None, NG check items, raw Broker result)."""
    script = py / "_bench_check.py"
    script.write_text(_CHECK, encoding="utf-8")
    result = run_script(script, broker, timeout_s)
    if not result.get("ok"):
        exc = result.get("exception") or result.get("error") or {}
        return f"check: {exc.get('type', '')}: {exc.get('message', '')}"[:800], [], result
    tables = json.loads(script.with_name("_bench_check.json").read_text(encoding="utf-8"))
    return None, ng_items(tables), result


# The model as OSIS holds it, however the code was written: the solver's own command stream (export_apdl), turned back
# into template-layout code by pyosis.transfer. The scorers read <model>/py, not the agent's code.
_EXPORT = """
from pyosis.transfer.out_to_python import write_prep_outputs


def export_model(engine, model):
    (model / "py" / "prep").mkdir(parents=True, exist_ok=True)  # OSIS does not create the folder
    engine.export_apdl(str(model / "OSIS.out"))
    write_prep_outputs(model / "OSIS.out", model / "py" / "prep")
"""

_INSPECT = """import json

from pyosis import OSISEngine

engine = OSISEngine()
out = {"summary": engine.model_summary()}
try:  # rebuilding clears results, so rows here mean the saved model was solved after its last rebuild
    out["reactions"] = len(engine.result.support_reaction())
except Exception as exc:  # noqa: BLE001
    out["reactions"] = 0
    out["reactions_error"] = f"{type(exc).__name__}: {exc}"[:300]
tables = engine.result.check_all()  # also cleared by a rebuild
out["checks"] = {name: sorted(set(df["结果"].astype(str))) if "结果" in df else [] for name, df in tables.items()}
try:
    export_model(engine, MODEL)
except Exception as exc:  # noqa: BLE001
    out["model_error"] = f"{type(exc).__name__}: {exc}"[:300]
Path(__file__).with_name("_bench_inspect.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
"""


def _script(path: Path, model: Path, body: str) -> Path:
    """A probe script: ``MODEL`` (where ``export_model`` writes) and ``export_model`` defined, then ``body``."""
    path.write_text(f"from pathlib import Path\n\nMODEL = Path({str(model)!r})\n" + _EXPORT + body, encoding="utf-8")
    return path


def inspect(py: Path, broker: str, model: Path, timeout_s: int = SCRIPT_S) -> dict[str, Any]:
    """Read the state an agent left in its project with pyosis, without rebuilding or solving:
    ``summary`` (model_summary), ``solved`` (results present), ``ng`` (NG items of the code checks it ran,
    None if it ran none), ``checks`` ({table name: values of its 结果 column}), ``error`` (the probe itself failed),
    ``model_error`` (the model could not be exported to ``model``). Raises BrokerError only for infrastructure."""
    script = _script(py / "_bench_inspect.py", model, _INSPECT)
    result = run_script(script, broker, timeout_s)
    if not result.get("ok"):
        exc = result.get("exception") or result.get("error") or {}
        return {"summary": None, "solved": False, "ng": None, "tables": 0, "checks": {},
                "error": f"inspect: {exc.get('type', '')}: {exc.get('message', '')}"[:800]}
    data = json.loads(script.with_name("_bench_inspect.json").read_text(encoding="utf-8"))
    return {"summary": data["summary"], "solved": data["reactions"] > 0, "tables": len(data["checks"]),
            "ng": ng_items(data["checks"]) if data["checks"] else None, "checks": data["checks"],
            "error": data.get("reactions_error"), "model_error": data.get("model_error")}


def inspect_project(py: Path, broker: str, model: Path) -> dict[str, Any]:
    """``inspect`` plus ``code_after_solve``: ``py/prep/*.py`` newer than the last solve (``OSIS.out``), i.e. the
    delivered code differs from the solved model. Recorded only: scoring reads the model, not the code."""
    state = inspect(py, broker, model)
    solved_at = py.parent / "OSIS.out"  # written by each solve
    code = [p.stat().st_mtime for p in (py / "prep").glob("*.py")] if (py / "prep").is_dir() else []
    state["code_after_solve"] = bool(solved_at.is_file() and code and max(code) > solved_at.stat().st_mtime)
    return state


_REBUILD = """import runpy
import sys

from pyosis import OSISEngine
from pyosis.core import command

_execute = command.OSISFunctionRegistry._execute_command


def execute(self, cmd, timeout=None):  # the model is all that is read: skip the solve the code may ask for
    return (True, "") if cmd.split(",")[0].rstrip(";").lower() in ("solve", "combinesolve") else _execute(self, cmd, timeout)


command.OSISFunctionRegistry._execute_command = execute
main = Path(__file__).parent / "prep" / "main.py"
sys.path.insert(0, str(main.parent))
try:
    runpy.run_path(str(main), run_name="__main__")
except BaseException as exc:  # noqa: BLE001  (code after the build may need results it never gets; what got built counts)
    print(f"main.py: {type(exc).__name__}: {exc}"[:500])
export_model(OSISEngine(), MODEL)
"""


def rebuild_model(py: Path, model: Path, broker: str, timeout_s: int = SCRIPT_S) -> str | None:
    """For runs whose OSIS project is gone (pruned before ``inspect`` exported the model): rebuild a copy of ``py``
    without solving and export its model to ``model`` like ``inspect``. Returns an error string or None."""
    if not (py / "prep" / "main.py").is_file():
        return "py/prep/main.py missing"
    model.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="rebuild-", dir=model))
    try:
        copy = scratch / "project" / "py"
        shutil.copytree(py, copy, ignore=shutil.ignore_patterns("__pycache__", "_bench_*"))
        result = run_script(_script(copy / "_bench_rebuild.py", model, _REBUILD),
                            broker, timeout_s)
        if result.get("ok"):
            return None
        shutil.rmtree(model / "py", ignore_errors=True)  # a half-written model is not the model
        exc = result.get("exception") or result.get("error") or {}
        return f"rebuild: {exc.get('type', '')}: {exc.get('message', '')}"[:800]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def rebuild_and_solve(py: Path, broker: str, solve_timeout_s: int = SCRIPT_S
                      ) -> tuple[str | None, dict[str, dict[str, Any]]]:
    """Rebuild ``py/prep/main.py`` and solve, in solver mode (the project is ``py``'s parent).
    Returns (error string or None, raw Broker result per step that ran)."""
    main = py / "prep" / "main.py"
    if not main.is_file():
        return "py/prep/main.py missing", {}
    (py / "_bench_solve.py").write_text(_SOLVE.format(timeout=solve_timeout_s), encoding="utf-8")
    results: dict[str, dict[str, Any]] = {}
    for step, script, timeout in (("rebuild", main, SCRIPT_S), ("solve", py / "_bench_solve.py", solve_timeout_s)):
        results[step] = result = run_script(script, broker, timeout)
        if not result.get("ok"):
            exc = result.get("exception") or result.get("error") or {}
            return f"{step}: {exc.get('type', '')}: {exc.get('message', '')}"[:800], results
    return None, results
