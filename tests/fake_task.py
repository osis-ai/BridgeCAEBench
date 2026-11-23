"""Stand-in task line for runner tests: one short-answer question per item, exact-match scoring."""

from __future__ import annotations

from pathlib import Path
from typing import Any

SERIAL = False


def load_items(ctx: Any) -> list[dict[str, Any]]:
    return ctx.task_cfg["items"]


def item_id(item: dict[str, Any]) -> str:
    return item["id"]


def hidden(ctx: Any) -> list[str]:
    return ["templates"]


def prepare(ctx: Any, item: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    if item.get("broken"):
        raise ValueError("item is broken")
    return {"instructions": item["question"], "work_dir": "", "writable": False, "deliverable": "answer",
            "native": {"item_id": item["id"]}}


def score(ctx: Any, item: dict[str, Any], run_dir: Path, generation: dict[str, Any]) -> dict[str, Any]:
    failed = generation.get("status") != "completed"
    return {"item": item["id"], "correct": not failed and generation.get("final_answer") == item["answer"],
            "latency_s": float(generation.get("elapsed_s") or 0.0), "error": generation.get("error") if failed else None}


def summarize(ctx: Any, records: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(records)
    return {"n": n, "accuracy": sum(r["correct"] for r in records) / n if n else 0.0,
            "mean_latency_s": sum(r["latency_s"] for r in records) / n if n else 0.0}
