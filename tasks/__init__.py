"""Task lines. A task line is a module here exposing:

    SERIAL: bool                                   # every run needs exclusive OSIS/OpenCode
    load_items(ctx) -> list[dict]                  # cases, private fields included (datasets/<line>/)
    item_id(item) -> str                           # unique, usable as a directory name
    hidden(ctx) -> list[str]                       # skill path parts that hold answers; never mounted
    prepare(ctx, item, run_dir) -> dict            # instructions, work_dir, writable, deliverable, native
    score(ctx, item, run_dir, generation) -> dict  # one record
    summarize(ctx, records) -> dict

Only ``prepare`` output crosses into method processes; it must not carry answers.
The old qa / check_repair / modeling lines live on the ``legacy`` branch.
The lines on an OSIS project share ``tasks/_project.py`` (staging, pyosis inspection, static scoring).
"""

from __future__ import annotations

from types import ModuleType

from tasks import analyze, build, change, diagnose, handover, knowledge, project  # noqa: E402

TASKS: dict[str, ModuleType] = {m.__name__.split(".")[-1]: m
                                for m in (analyze, build, change, diagnose, handover, knowledge, project)}


def get(name: str) -> ModuleType:
    try:
        return TASKS[name]
    except KeyError as exc:
        raise KeyError(f"unknown task {name!r}; known: {sorted(TASKS)}") from exc
