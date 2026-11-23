"""handover: finish a colleague's half-built model (a template cut after some module).

Scored like change: solved × mean(checks, preserve, conformance). preserve compares only the facts the start
already has, so it covers the kept modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tasks import _project as p

SERIAL = False


def load_items(ctx: Any) -> list[dict[str, Any]]:
    return p.load_items("handover")


item_id = p.item_id
score = p.score_edit


def hidden(ctx: Any) -> list[str]:
    return p.hidden(ctx, load_items(ctx))


def prepare(ctx: Any, item: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    return p.prepare(ctx, item, run_dir)


def summarize(ctx: Any, records: list[dict[str, Any]]) -> dict[str, Any]:
    return p.summarize("handover", records)
