"""Shared by the lines where an agent works on an OSIS project (build, change, handover, diagnose, analyze).

The platform never rebuilds or solves for scoring. The agent's own pyosis work is what is judged: after the run,
``bench.osis.inspect`` opens the project it left and reads with pyosis what is there (model summary, results,
code-check tables). Rebuilding clears results and check tables, so results present mean the saved model was solved
after its last rebuild. The checks read the model it left, not how the code is written: OSIS exports its command
stream and pyosis.transfer writes it back as template-layout code (``static``), so code edited after the last solve
(``py/prep/*.py`` newer than ``OSIS.out``) is only recorded (``code_after_solve``), not penalised.
No results = the agent did not get the task done: the run is scored, not retried.
"""

from __future__ import annotations

import functools
import json
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from bench import osis
from bench.config import ROOT
from scoring import cases
from scoring.checks import preserve, score_checks
from scoring.evaluation.levels.model_conformance import evaluate as conformance
from scoring.extract import extract

SERIAL = False
BRIDGE_TYPES = {"osis-bridge-cantilever-box": "cantilever_box", "osis-bridge-rigid-frame-box": "rigid_frame",
                "osis-bridge-precast-t-girder": "t_girder", "osis-bridge-precast-small-box": "precast_small_box",
                "osis-bridge-conventional-box": "cast_in_place_box", "osis-bridge-hollow-slab": "hollow_slab"}
INSTRUCTIONS = """{brief}

工程目录：{project}。Python 模型写在 py/ 下，入口 py/prep/main.py，各模块为 py/prep/_N_*.py{post}。
OSIS 只能通过 execute_python 运行 py/ 下的脚本（file 填脚本的绝对路径）。改了代码要重新运行 py/prep/main.py 重建并求解：
评分时读的是你最后留在 OSIS 里的模型和结果，以及 py/ 下的代码。
没有工程师可以确认参数，问了也只会回答“按默认参数，你自己判断”：简报里没给的按常规取值，并在回复里写明你的假设。
skill 只通过提供的工具读取，不要在磁盘上搜索文件，不要查找数据集、评测文件、其他运行目录或模板答案。
不要结束不是你自己启动的进程（评测平台、OSIS、求解器等），它们是评测环境的一部分。"""


def load_items(line: str) -> list[dict[str, Any]]:
    return [c for c in cases.load_line(line) if c["status"] in ("verified", "frozen")]


def item_id(item: dict[str, Any]) -> str:
    return str(item["id"])


def hidden(ctx: Any, items: list[dict[str, Any]]) -> list[str]:
    """``templates: with`` keeps every template in the skills; the default hides the cases' own templates."""
    if ctx.task_cfg.get("templates") == "with":
        return []
    return sorted({t for item in items for t in item.get("hide_templates") or []})


def prepare(ctx: Any, item: dict[str, Any], run_dir: Path, brief: str | None = None) -> dict[str, Any]:
    """Stage the start project in ``run_dir/project`` (and a pristine copy in ``run_dir/start`` to compare with)."""
    project = run_dir / "project"
    py = cases.materialize(item, "start", ctx.parent.skills, project / "py")
    shutil.copytree(py, run_dir / "start" / "py")
    post = "；组合与验算在 py/post/main.py（求解后运行）" if (py / "post" / "main.py").is_file() else ""
    return {"instructions": INSTRUCTIONS.format(brief=(brief or item["brief"]).strip(), project=project, post=post),
            "work_dir": str(project), "writable": True, "deliverable": "files",
            "native": {"item_id": item_id(item), "line": item["line"], "kind": item.get("kind")}}


def inspect(ctx: Any, run_dir: Path) -> dict[str, Any]:
    """What the agent left in OSIS (``bench.osis.inspect``, its model exported to ``run_dir/model``) plus
    ``code_after_solve`` from file times."""
    return osis.inspect_project(run_dir / "project" / "py", ctx.config["osis"]["broker_url"], run_dir / "model")


def static(ctx: Any, item: dict[str, Any], run_dir: Path, checks: list[dict[str, Any]] | None = None
           ) -> dict[str, Any]:
    """Checks, preserve and conformance on the model the agent left, as code (``inspect`` exported it to
    ``run_dir/model/py``); a run whose project was pruned before that is rebuilt from its code without solving.
    A model that cannot be had scores as an empty one (``model_error``)."""
    model = run_dir / "model"
    error = None
    if not (model / "py" / "prep").is_dir():
        error = osis.rebuild_model(run_dir / "project" / "py", model, ctx.config["osis"]["broker_url"])
    py = model / "py"
    facts = extract(py)
    specs = (item.get("checks") or []) if checks is None else checks
    out: dict[str, Any] = {"checks": score_checks(facts, specs) if specs else None, "model_error": error}
    if item.get("preserve") and item["start"] != "empty":
        exclude = {c["field"] for c in specs} | set(item.get("may_change") or [])  # a.b leaves out one key
        out["preserve"] = preserve(extract(run_dir / "start" / "py"), facts, exclude)
    if item.get("reference"):
        reference = cases.materialize(item, "reference", ctx.parent.skills, run_dir / "reference" / "py")
        report = conformance(reference, py, system_config={}, resources={"bridge_type": BRIDGE_TYPES[item["bridge"]]})
        out["conformance"] = {"score": float(report.get("overall_score") or 0.0),
                              "candidate": report.get("candidate_score"), "reference": report.get("reference_score")}
    return out


def failed(call: dict[str, Any]) -> bool:
    """A script that raised comes back as a normal MCP result with ``"ok": false`` in its text, not as an MCP error."""
    if call.get("is_error") or call.get("error"):
        return True
    try:
        return json.loads(str(call.get("result") or "")).get("ok") is False
    except (json.JSONDecodeError, AttributeError):
        return False


def behaviour(run_dir: Path) -> dict[str, Any]:
    """How the agent drove OSIS, from the MCP proxy log: what it ran, what failed."""
    calls = []
    path = run_dir / "mcp_calls.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
        call = json.loads(line)
        script = str((call.get("arguments") or {}).get("file") or "").replace("\\", "/").rsplit("/", 1)[-1]
        if (call.get("server") == "osis" and call.get("tool") in ("execute_python", "execute_apdl")
                and script != "_t4_gate.py"):  # T4's gate probe is the orchestrator's, not the agent's
            calls.append(call)
    files = [str((c.get("arguments") or {}).get("file") or "") for c in calls]
    return {"osis_runs": len(calls), "osis_errors": sum(failed(c) for c in calls),
            "refused": sum(bool(c.get("refused")) for c in calls),
            "ran_main": any(f.replace("\\", "/").endswith("prep/main.py") for f in files),
            "transient": any(osis.transient(c.get("result")) for c in calls if c.get("is_error"))}


def last_json(text: str, key: str) -> dict[str, Any] | None:
    """The last JSON object in ``text`` that has ``key`` (replies often wrap it in prose or a code fence)."""
    decoder, found = json.JSONDecoder(), None
    for match in re.finditer(r"\{", text or ""):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and key in value:
            found = value
    return found


def normalized_duration(ctx: Any, generation: dict[str, Any]) -> float:
    """Output tokens at a fixed rate + harness tool time, so gateway speed does not count; wall time otherwise.
    Time in the osis MCP server (rebuilds, solves, code checks) is left out: it measures OSIS, not the agent."""
    output = ((generation.get("gateway") or {}).get("completion_tokens")
              or (generation.get("tokens") or {}).get("completion_tokens"))
    if not output:
        return float(generation.get("elapsed_s") or 0.0)
    osis_s = float(((generation.get("mcp") or {}).get("osis") or {}).get("tool_s") or 0.0)
    tool_s = float(generation.get("tool_s") or 0.0) - osis_s
    return round(output / float(ctx.task_cfg.get("tokens_per_s") or 100) + tool_s, 3)


@functools.cache
def prices() -> dict[str, Any]:
    """Official list prices per model, USD per million tokens: configs/pricing.yaml."""
    return yaml.safe_load((ROOT / "configs" / "pricing.yaml").read_text(encoding="utf-8"))["models"]


def usage(generation: dict[str, Any]) -> dict[str, Any]:
    """Tokens as the run's gateway recorder counted them (every call, the same way for every method); what the
    method reports itself is only a fallback for runs from before the recorder (it can cover only the last turn)."""
    return generation.get("gateway") or generation.get("tokens") or {}


def cost_usd(model: str, used: dict[str, Any]) -> float:
    """List price of the tokens: uncached input, cache reads, cache writes and output (reasoning included)."""
    if model not in prices():
        raise KeyError(f"no list price for model {model!r} in configs/pricing.yaml")
    p = prices()[model]  # shortcut: the top-level (peak) price for every call; DeepSeek halves off-peak hours
    cached = int(used.get("cached_tokens") or 0)
    tokens = {"input": int(used.get("prompt_tokens") or 0) - cached, "cache_read": cached,
              "cache_write": int(used.get("cache_write_tokens") or 0), "output": int(used.get("completion_tokens") or 0)}
    return round(sum(n * float(p[k]) for k, n in tokens.items()) / 1e6, 6)


def base_record(ctx: Any, item: dict[str, Any], run_dir: Path, generation: dict[str, Any]) -> dict[str, Any]:
    used = usage(generation)
    return {"item_id": item_id(item), "line": item["line"], "kind": item.get("kind"), "bridge": item.get("bridge"),
            "score": 0.0, "error": None if generation.get("status") == "completed"
            else str(generation.get("error") or generation.get("stop_reason") or "generation failed"),
            "latency_s": float(generation.get("elapsed_s") or 0.0), "duration_s": normalized_duration(ctx, generation),
            "usage": used, "cost_usd": cost_usd(ctx.config["model"]["name"], used), "behaviour": behaviour(run_dir)}


def score_edit(ctx: Any, item: dict[str, Any], run_dir: Path, generation: dict[str, Any]) -> dict[str, Any]:
    """change / handover: graded by the line's rubric (configs/evaluation.yaml)."""
    record = base_record(ctx, item, run_dir, generation)
    if record["error"]:
        return record
    record["state"] = inspect(ctx, run_dir)
    record["solved"] = float(record["state"]["solved"])
    record["detail"] = found = static(ctx, item, run_dir)
    record.update(checks=part(found["checks"]), preserve=part(found.get("preserve")),
                  conformance=part(found.get("conformance")))
    record.update(grade(record["line"], record))
    return record


def mean(values: list[float | None]) -> float:
    present = [float(v) for v in values if v is not None]
    return round(sum(present) / len(present), 4) if present else 0.0


def part(result: dict[str, Any] | None) -> float | None:
    return None if result is None else float(result["score"])


@functools.cache
def rubric(line: str) -> dict[str, Any]:
    """The line's scoring rule: configs/evaluation.yaml ``tasks.<line>``."""
    return yaml.safe_load((ROOT / "configs" / "evaluation.yaml").read_text(encoding="utf-8"))["tasks"][line]


def grade(line: str, record: dict[str, Any]) -> dict[str, Any]:
    """Score one record by its line's rubric: quality, efficiency, cost and the weighted total, all in [0, 1]."""
    rule = rubric(line)
    kind = {**rule, **(rule.get("by_kind") or {}).get(str(record.get("kind")), {})}
    if record.get("error"):
        return {"score": 0.0, "efficiency": 0.0, "cost": 0.0, "total": 0.0}
    gate = 1.0
    for name in kind["gate"]:
        gate *= float(record.get(name) or 0.0)
    present = {k: w for k, w in kind["parts"].items() if record.get(k) is not None}
    quality = gate * sum(w * float(record[k]) for k, w in present.items()) / sum(present.values()) if present else 0.0
    efficiency = rule["efficiency"]["scale"] / (rule["efficiency"]["scale"] + float(record.get("duration_s") or 0))
    cost = rule["cost"]["scale"] / (rule["cost"]["scale"] + float(record["cost_usd"]))
    weights = rule["weights"]
    total = weights["quality"] * quality + weights["efficiency"] * efficiency + weights["cost"] * cost
    return {"score": round(quality, 4), "efficiency": round(efficiency, 4), "cost": round(cost, 4),
            "total": round(total, 4)}


def summarize(line: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Means of the weighted total, quality (score), efficiency, cost and each rubric part, overall and per kind.

    Graded from the stored parts, so a rubric change applies to finished runs too. A part missing from a record
    is left out of its mean."""
    rule = rubric(line)
    parts = list(dict.fromkeys([*rule["gate"], *rule["parts"],
                                *(p for k in (rule.get("by_kind") or {}).values() for p in [*k["gate"], *k["parts"]])]))

    def stats(rs: list[dict[str, Any]]) -> dict[str, Any]:
        graded = [grade(line, r) for r in rs]
        out = {"n": len(rs), **{k: mean([g[k] for g in graded]) for k in ("total", "score", "efficiency", "cost")},
               "errors": sum(bool(r.get("error")) for r in rs),
               "mean_duration_s": mean([r.get("duration_s") for r in rs]),
               "mean_tokens": mean([(r.get("usage") or {}).get("total_tokens") for r in rs]),
               "mean_cost_usd": mean([r.get("cost_usd") for r in rs])}
        out.update({p: mean([r.get(p) for r in rs]) for p in parts if any(r.get(p) is not None for r in rs)})
        return out

    kinds = sorted({str(r.get("kind")) for r in records})
    return {**stats(records), "by_kind": {k: stats([r for r in records if str(r.get("kind")) == k]) for k in kinds}}
