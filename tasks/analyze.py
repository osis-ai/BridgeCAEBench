"""analyze: rebuild and solve a template, then answer a question about its results.

The agent solves it itself (the brief says so). Scored by ``line_answer``: each key of the reply JSON against
``truth``, 0/1, averaged. Numbers by relative ``tolerance``; an element is right if it is any of the truth's
(an extreme on a node shared by two elements counts for both).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tasks import _project as p

SERIAL = False


def load_items(ctx: Any) -> list[dict[str, Any]]:
    return [c for c in p.load_items("analyze") if c.get("truth") is not None]


item_id = p.item_id


def hidden(ctx: Any) -> list[str]:
    return []  # the start is the template itself: nothing to hide


def prepare(ctx: Any, item: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    return p.prepare(ctx, item, run_dir)


def answer_score(item: dict[str, Any], reply: dict[str, Any] | None) -> float:
    if not reply:
        return 0.0
    tolerance = item.get("tolerance") or {}
    marks = []
    for key, want in item["truth"].items():
        got = reply.get(key)
        try:
            if isinstance(want, list):
                marks.append(int(float(got)) in want)
            elif isinstance(want, (int, float)):
                marks.append(abs(float(got) - want) <= float(tolerance.get(key, 0.02)) * abs(want))
            else:
                marks.append(str(got).strip() == str(want))
        except (TypeError, ValueError):
            marks.append(False)
    return round(sum(marks) / len(marks), 4)


def score(ctx: Any, item: dict[str, Any], run_dir: Path, generation: dict[str, Any]) -> dict[str, Any]:
    record = p.base_record(ctx, item, run_dir, generation)
    if record["error"]:
        return record
    reply = p.last_json(str(generation.get("final_answer") or ""), next(iter(item["truth"])))
    record.update(reply=reply, truth=item["truth"], line_answer=answer_score(item, reply))
    record.update(p.grade("analyze", record))
    return record


def summarize(ctx: Any, records: list[dict[str, Any]]) -> dict[str, Any]:
    return p.summarize("analyze", records)
