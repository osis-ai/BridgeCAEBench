"""diagnose: from a symptom, locate and fix a seeded fault.

Kinds: ``check_ng`` (a code check is NG), ``run_error`` (the rebuild fails, the brief quotes the error),
``review`` (the model disagrees with a design table). Scored solved × mean(line_localize, line_repair, preserve):
- line_localize: check_ng / run_error, the last ``reason_code`` of the reply's ``localizations`` hits the seed;
  review, F1 of the reply's ``inconsistencies`` items against the seeded ones.
- line_repair: check_ng, the code-check tables the agent left (it must run py/post/main.py after its last solve)
  have the target item (at least one table, none NG) OK and no NG outside ``ng_seeded``, with py/post/main.py unchanged (passing a check by
  editing the check settings is not a repair); run_error, solved; review, the checks on the seeded values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tasks import _project as p

SERIAL = False


def load_items(ctx: Any) -> list[dict[str, Any]]:
    return p.load_items("diagnose")


item_id = p.item_id


def hidden(ctx: Any) -> list[str]:
    return p.hidden(ctx, load_items(ctx))


def prepare(ctx: Any, item: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    brief = item["brief"].replace("{symptom}", str(item.get("symptom") or ""))  # the brief also holds JSON braces
    return p.prepare(ctx, item, run_dir, brief)


def localize(item: dict[str, Any], answer: str) -> float:
    if item["kind"] == "review":
        reply = p.last_json(answer, "inconsistencies") or {}
        got = {str(x.get("item")) for x in reply.get("inconsistencies") or [] if isinstance(x, dict)}
        want = {x["item"] for x in item["localization"]}
        hit = len(got & want)
        return round(2 * hit / (len(got) + len(want)), 4) if hit else 0.0
    reply = p.last_json(answer, "localizations") or {}
    found = [x for x in reply.get("localizations") or [] if isinstance(x, dict) and x.get("reason_code")]
    return float(bool(found) and found[-1]["reason_code"] == item["localization"][0]["reason_code"])


def repair(item: dict[str, Any], run_dir: Path, state: dict[str, Any], found: dict[str, Any]) -> float:
    if item["kind"] == "run_error":
        return float(state["solved"])
    if item["kind"] == "review":
        return p.part(found["checks"]) or 0.0
    post = [run_dir / which / "py" / "post" / "main.py" for which in ("project", "start")]
    same_post = post[0].is_file() and post[0].read_bytes() == post[1].read_bytes()
    ng = state["ng"]
    # the target must be checked and OK, not merely absent from the NG list
    target = [v for name, v in (state.get("checks") or {}).items() if name.split("_")[1:2] == [item["target_item"]]]
    return float(same_post and ng is not None and bool(target) and not any("NG" in v for v in target)
                 and set(ng) <= set(item["ng_seeded"]))


def score(ctx: Any, item: dict[str, Any], run_dir: Path, generation: dict[str, Any]) -> dict[str, Any]:
    record = p.base_record(ctx, item, run_dir, generation)
    if record["error"]:
        return record
    record["state"] = state = p.inspect(ctx, run_dir)
    record["solved"] = float(state["solved"])
    record["detail"] = found = p.static(ctx, item, run_dir)
    record["line_localize"] = localize(item, str(generation.get("final_answer") or ""))
    record["line_repair"] = repair(item, run_dir, state, found)
    record["preserve"] = p.part(found.get("preserve"))
    record.update(p.grade("diagnose", record))
    return record


def summarize(ctx: Any, records: list[dict[str, Any]]) -> dict[str, Any]:
    return p.summarize("diagnose", records)
