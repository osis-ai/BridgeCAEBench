"""build: model a bridge from an engineer's brief, from an empty project.

complete: solved × mean(checks, conformance). missing: solved × mean(checks, conformance, line_assume). A missing
case leaves out the checks on what the brief withholds (``must_ask``): nobody answers, so the agent's own defaults
are not marked wrong; ``line_assume`` is the share of withheld parameters the reply names with a value.
infeasible: whether the reply points out the problem (``line_pushback``); building it anyway is not marked wrong,
``solved`` is kept apart.
Both are judged by a small model first (``scoring.judge.verdict``, ``tasks.build.judge`` in the config); when it cannot
give a verdict, or no judge is configured, by rule: a parameter name with a number on the same line; at least two of the
case's keywords that are not in the brief (so restating the brief scores nothing).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from scoring import cases, judge
from tasks import _project as p

SERIAL = False
# must_ask term -> the check fields it covers
ASKED = {"梁高": {"H_mid", "H_root", "H"}, "二期恒载": {"line_load_kn", "line_loads"}, "沉降": {"settlement_mm"},
         "混凝土等级": {"concrete_grade"}, "温度": {"uniform_temp", "has_temp_gradient"},
         "张拉控制应力": {"tendon_stress"}}
# must_ask term -> how a reply names it (the stated value must follow within the same line)
NAMED = {"梁高": r"梁高", "二期恒载": r"二期|铺装", "沉降": r"沉降", "混凝土等级": r"混凝土|C\d{2}",
         "温度": r"升温|降温|温度", "张拉控制应力": r"张拉|控制应力|σcon"}
VERDICTS = cases.DATASETS / "build" / "judged" / "verdicts.jsonl"
PUSHBACK = """判断智能体对一份桥梁建模需求的回复，是否明确指出了需求里的问题。
- 回复指出了参考问题所说的那处不合理（说法、角度不同都行），算对；拒做、照做或改了方案再做都可以，只看有没有指出。
- 只复述需求、只报告已完成、只泛泛提示风险而没点出这处问题，或指出的是别的问题，算错。"""
ASSUME = """需求里没有给出下面这项参数，智能体只能自己取值。判断回复是否明确说明了这项参数采用的取值。
- 写出了具体取值（数值或等级），算对；取值合不合理不管。
- 没提这项，或只说"按默认""按模板""按规范"而没写出值，算错。"""


def load_items(ctx: Any) -> list[dict[str, Any]]:
    return p.load_items("build")


item_id = p.item_id


def hidden(ctx: Any) -> list[str]:
    return p.hidden(ctx, load_items(ctx))


def prepare(ctx: Any, item: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    return p.prepare(ctx, item, run_dir)


def scored_checks(item: dict[str, Any]) -> list[dict[str, Any]]:
    withheld = set().union(*(ASKED.get(term, set()) for term in item.get("must_ask") or []))
    unknown = [t for t in item.get("must_ask") or [] if t not in ASKED]
    if unknown:
        raise KeyError(f"{item['id']}: must_ask terms without check fields: {unknown}")
    return [c for c in item.get("checks") or [] if c["field"].split(".")[0] not in withheld]


def pushback(item: dict[str, Any], answer: str) -> bool:
    hits = [k for k in item["pushback"]["keywords"] if k in answer and k not in item["brief"]]
    return len(hits) >= 2


def assume(item: dict[str, Any], answer: str) -> float:
    """Share of the withheld parameters the reply names together with a value."""
    terms = item.get("must_ask") or []
    return sum(named(t, answer) for t in terms) / len(terms)


def named(term: str, answer: str) -> bool:
    return bool(re.search(rf"(?:{NAMED[term]})[^\n]{{0,30}}?\d", answer))


def _judged(ctx: Any, rules: str, inputs: dict[str, str]) -> dict[str, Any] | None:
    cfg = (ctx.task_cfg or {}).get("judge")
    return judge.verdict(cfg, VERDICTS, rules, inputs) if cfg and inputs["待评回复"].strip() else None


def score(ctx: Any, item: dict[str, Any], run_dir: Path, generation: dict[str, Any]) -> dict[str, Any]:
    record = p.base_record(ctx, item, run_dir, generation)
    if record["error"]:
        return record
    answer = str(generation.get("final_answer") or "")
    if item.get("kind") == "infeasible":
        record["judge"] = _judged(ctx, PUSHBACK, {"需求": item["brief"], "参考问题": item["pushback"]["issue"],
                                                  "待评回复": answer})
        record["line_pushback"] = float(record["judge"]["correct"] if record["judge"] else pushback(item, answer))
        record["state"] = p.inspect(ctx, run_dir)
        record["solved"] = float(record["state"]["solved"])
        return {**record, **p.grade("build", record)}
    record["state"] = p.inspect(ctx, run_dir)
    record["solved"] = float(record["state"]["solved"])
    static = p.static(ctx, item, run_dir, scored_checks(item))
    record["detail"] = static
    record["checks"], record["conformance"] = p.part(static["checks"]), p.part(static.get("conformance"))
    if item.get("must_ask"):
        record["judge"] = {t: _judged(ctx, ASSUME, {"参数": t, "需求": item["brief"], "待评回复": answer})
                           for t in item["must_ask"]}
        hits = [v["correct"] if v else named(t, answer) for t, v in record["judge"].items()]
        record["line_assume"] = sum(hits) / len(hits)
    return {**record, **p.grade("build", record)}


def summarize(ctx: Any, records: list[dict[str, Any]]) -> dict[str, Any]:
    return p.summarize("build", records)
