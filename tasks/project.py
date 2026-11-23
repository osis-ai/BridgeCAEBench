"""project: one session, one project, ordered turns (build -> change A -> change B -> full review), each turn on the
agent's own output of the previous one: what errors carry over.

The method sends ``instructions`` then each of ``turns`` in the same session (T3 has none: it reruns on the project
with the earlier replies); after every turn ``methods._common.end_turn`` reads the OSIS state and copies the code to
``run_dir/turn<k>/``. Each turn is scored like change: solved x mean(checks, preserve against the previous turn's
code, conformance against that turn's reference). ``turn_quality`` is the mean over all turns, a turn the session
never reached scoring 0; ``first_failed_turn`` is the first turn not solved or with a check failing.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from tasks import _project as p

SERIAL = False


def load_items(ctx: Any) -> list[dict[str, Any]]:
    return p.load_items("project")


item_id = p.item_id


def hidden(ctx: Any) -> list[str]:
    return p.hidden(ctx, load_items(ctx))


def prepare(ctx: Any, item: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    out = p.prepare(ctx, item, run_dir, brief=item["turns"][0]["brief"])
    # every project starts empty, so no turn has a post/main.py to mention up front (p.prepare's post is "")
    later = [p.INSTRUCTIONS.format(brief=t["brief"].strip(), project=out["work_dir"], post="")
             for t in item["turns"][1:]]
    return {**out, "turns": later, "broker_url": ctx.config["osis"]["broker_url"]}


def calls(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "model_calls.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def turn_usage(model_calls: list[dict[str, Any]], after: str, until: str) -> dict[str, int]:
    """Gateway tokens of the calls that started in (after, until]: one turn (timestamps are the same UTC format)."""
    total: dict[str, int] = {}
    for call in model_calls:
        if after < str(call.get("started_at") or "") <= until:
            for key, value in (call.get("usage") or {}).items():
                total[key] = total.get(key, 0) + int(value or 0)
    return total


def score(ctx: Any, item: dict[str, Any], run_dir: Path, generation: dict[str, Any]) -> dict[str, Any]:
    record = p.base_record(ctx, item, run_dir, generation)
    model, log, ended = ctx.config["model"]["name"], calls(run_dir), ""
    turns = []
    for k, spec in enumerate(item["turns"], start=1):
        here = run_dir / f"turn{k}"
        info = json.loads((here / "turn.json").read_text(encoding="utf-8")) if (here / "turn.json").is_file() else None
        if info is None:  # the session ended (timeout, no reply) before this turn did
            turns.append({"turn": k, "done": False, "quality": 0.0, "passed": False})
            continue
        if k > 1:
            shutil.copytree(run_dir / f"turn{k - 1}" / "project" / "py", here / "start" / "py", dirs_exist_ok=True)
        found = p.static(ctx, {**item, **spec, "start": "empty" if k == 1 else "previous"}, here)
        parts = {"solved": float(info["state"]["solved"]), "checks": p.part(found["checks"]),
                 "preserve": p.part(found.get("preserve")), "conformance": p.part(found.get("conformance"))}
        used = turn_usage(log, ended, info["ended_at"])
        ended = info["ended_at"]
        turns.append({"turn": k, "done": True, **parts,
                      "quality": p.grade("change", {**parts, "duration_s": 0, "cost_usd": 0})["score"],
                      "passed": bool(parts["solved"] and parts["checks"] == 1.0),
                      "usage": used, "cost_usd": p.cost_usd(model, used), "detail": found, "state": info["state"]})
    record["turns"] = turns
    record["turns_done"] = sum(t["done"] for t in turns)
    record["first_failed_turn"] = next((t["turn"] for t in turns if not t["passed"]), None)
    record["turn_quality"] = round(sum(t["quality"] for t in turns) / len(turns), 4)
    if record["turns_done"]:  # finished turns count even if the session failed later
        record["generation_error"], record["error"] = record["error"], None
    return {**record, **p.grade("project", record)}


def summarize(ctx: Any, records: list[dict[str, Any]]) -> dict[str, Any]:
    out = p.summarize("project", records)
    n = max((len(r.get("turns") or []) for r in records), default=0)
    out["by_turn"] = [p.mean([(r.get("turns") or [])[k]["quality"] for r in records if len(r.get("turns") or []) > k])
                      for k in range(n)]
    out["first_failed_turn"] = {str(t): sum(r.get("first_failed_turn") == t for r in records)
                                for t in sorted({r.get("first_failed_turn") for r in records}, key=str)}
    return out
