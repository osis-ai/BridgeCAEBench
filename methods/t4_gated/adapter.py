"""T4: verification gate around T1 (AIDA-BRIDGE accept / redo / defer; Luo et al.'s violation-feedback loop).

The inner agent is T1 unchanged (same prompt, same session). When it submits, the orchestrator checks gates
with what the agent itself can read over MCP (a read-only pyosis probe through the run's osis proxy, and
file times); a failing gate goes back as one more user message. After ``MAX_REOPEN`` reopenings the run is
marked ``deferred`` (handed to a human). A reply with ``status: declined`` skips G1–G3.

G1 solved: support reactions exist. G2 no prep/*.py newer than OSIS.out. G3 the check items the instructions
name (「…验算」) are OK. G4 the reply's JSON parses (analyze: the keys the brief asks for; diagnose: its
localizations / inconsistencies, reason codes from py/reason_codes.yaml). Never uses the scorer's hidden data.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from bench.osis import _INSPECT, ng_items
from methods._common import base_metadata, end_turn, fail, finish, main, package_version
from methods.mcp_cli import Session
from methods.t1_miniswe import adapter as t1

MAX_REOPEN = 5
SOLVE_LINES = {"build", "change", "handover", "diagnose", "analyze", "project"}
PROBE = "_t4_gate.py"
Probe = Callable[[dict[str, Any]], dict[str, Any]]


def probe(request: dict[str, Any]) -> dict[str, Any]:
    """Read-only pyosis probe of the agent's project through the run's osis MCP proxy (as the agent could)."""
    py = Path(request["work_dir"]) / "py"
    script = py / PROBE
    script.write_text(_INSPECT.replace("_bench_inspect.json", "_t4_gate.json"), encoding="utf-8")
    text, is_error = Session(request["mcp_servers"]["osis"]["url"], float(request["timeout_s"])).call(
        "execute_python", {"instance_id": "solver", "file": str(script), "cwd": str(py), "timeout": 600})
    out = py / "_t4_gate.json"
    if is_error or not out.is_file():
        return {"error": text[:500]}
    data = json.loads(out.read_text(encoding="utf-8"))
    out.unlink()
    return {"solved": data["reactions"] > 0, "checks": data["checks"]}


def declined(answer: str) -> bool:
    first = next((line for line in answer.splitlines() if line.strip()), "")
    return bool(re.match(r"\s*`?status:\s*declined", first, re.IGNORECASE))


def _json_with(answer: str, key: str) -> dict[str, Any] | None:
    decoder, found = json.JSONDecoder(), None
    for i, ch in enumerate(answer):
        if ch == "{":
            try:
                value, _ = decoder.raw_decode(answer, i)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and key in value:
                found = value
    return found


def format_gate(request: dict[str, Any], answer: str) -> list[str]:
    """G4: the reply format the task line asks for."""
    task, brief = request["task"], request["instructions"]
    if task == "analyze":
        keys = re.findall(r'"([A-Za-z_]+)"\s*:', brief)
        if keys and _json_with(answer, keys[0]) is None:
            return [f"G4 格式：回复里没有可解析的 JSON（要有键 {keys}）"]
    if task == "diagnose":
        key = "inconsistencies" if "inconsistencies" in brief else "localizations"
        reply = _json_with(answer, key)
        if reply is None:
            return [f"G4 格式：回复里没有可解析的 JSON（要有键 {key}）"]
        codes_file = Path(request["work_dir"]) / "py" / "reason_codes.yaml"
        if key == "localizations" and codes_file.is_file():
            import yaml

            known = set((yaml.safe_load(codes_file.read_text(encoding="utf-8")) or {}).get("codes") or {})
            bad = [x.get("reason_code") for x in reply.get(key) or [] if isinstance(x, dict)
                   and x.get("reason_code") not in known]
            if bad:
                return [f"G4 格式：reason_code {bad} 不在 py/reason_codes.yaml 里"]
    return []


def gates(request: dict[str, Any], answer: str, probe_fn: Probe = probe) -> list[str]:
    """Violations (empty = accept)."""
    violations = format_gate(request, answer)
    if request["task"] not in SOLVE_LINES or not request.get("work_dir") or declined(answer):
        return violations
    project = Path(request["work_dir"])
    state = probe_fn(request)
    if state.get("error"):
        return [f"G1 有解：探查失败，读不到模型（{state['error']}）", *violations]
    if not state["solved"]:
        violations.insert(0, "G1 有解：OSIS 里没有求解结果（支座反力为空）。改完要运行 py/prep/main.py 重建并求解")
    out = project / "OSIS.out"
    newer = [p.name for p in (project / "py" / "prep").glob("*.py")
             if out.is_file() and p.stat().st_mtime > out.stat().st_mtime]
    if newer:
        violations.append(f"G2 算后未改：{newer} 比最近一次求解（OSIS.out）新，代码改了没重算")
    named = set(re.findall(r"「([^」]*验算)」", request["instructions"]))
    if named:
        tables = state.get("checks") or {}
        if not tables:
            violations.append(f"G3 验算：没有验算表，求解后要运行 py/post/main.py（点名的项：{sorted(named)}）")
        else:
            ng = [item for item in sorted(named) if item in ng_items(tables)]
            if ng:
                violations.append(f"G3 验算：点名的验算项仍为 NG：{ng}")
    return violations


def feedback(violations: list[str]) -> str:
    return ("外部检查没有通过，任务还没完成。不通过的项：\n" + "\n".join(f"- {v}" for v in violations)
            + "\n请接着处理，完成后照原格式重写最终回复并提交。")


def _runtime(request: dict[str, Any], prompt: str, probe_fn: Probe = probe) -> dict[str, Any]:
    agent = t1.make_agent(request)
    exit_info = agent.run(prompt)
    turns: list[dict[str, Any]] = []  # project: one gate loop per turn; other lines have one turn
    current = request["instructions"]
    for text in [*(request.get("turns") or []), None]:
        exit_info, gate = gated({**request, "instructions": current}, agent, exit_info, probe_fn)  # G3: this turn's
        turns.append(gate)
        if exit_info.get("exit_status") != "Submitted":
            break
        end_turn(request, str(exit_info.get("submission") or ""))
        if text is None:
            break
        current = text
        exit_info = agent.resume(t1.next_turn(text))
    return {**t1.summarize(agent, exit_info), **turns[-1], **({"turn_gates": turns} if "turns" in request else {})}


def gated(request: dict[str, Any], agent: Any, exit_info: dict[str, Any], probe_fn: Probe
          ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Gate a submission, reopening the session with the violations until it passes or ``MAX_REOPEN`` runs out."""
    rounds: list[list[str]] = []
    while True:
        answer = str(exit_info.get("submission") or "")
        if exit_info.get("exit_status") != "Submitted":  # out of time or format errors: nothing to gate
            return exit_info, {"gate_rounds": rounds, "reopened": len(rounds), "deferred": False}
        violations = gates(request, answer, probe_fn)
        rounds.append(violations)
        print(f"[gate] round {len(rounds)}: {violations or 'accept'}", flush=True)
        if not violations or len(rounds) > MAX_REOPEN:
            return exit_info, {"gate_rounds": rounds, "reopened": len(rounds) - 1, "deferred": bool(violations)}
        exit_info = agent.resume(feedback(violations))


def run(request: dict[str, Any], *, runtime: Callable[[dict[str, Any], str], dict[str, Any]] | None = None
        ) -> dict[str, Any]:
    started = time.monotonic()
    metadata = base_metadata(request, framework="mini-swe-agent+gate", framework_version=package_version("mini-swe-agent"),
                             interaction_mode="gated_bash_loop", tools=["bash"], max_reopen=MAX_REOPEN)
    try:
        metadata.update((runtime or _runtime)(request, t1.task_text(request)))
        if not str(metadata.get("final_answer") or "").strip():
            raise RuntimeError(f"mini-SWE-agent ended without a submission ({metadata.get('exit_status')})")
        metadata.update(status="completed", stop_reason="deferred" if metadata.get("deferred") else "completed")
    except Exception as exc:  # noqa: BLE001
        fail(metadata, exc)
    return finish(request, metadata, started)


if __name__ == "__main__":
    raise SystemExit(main(run))
