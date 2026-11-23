"""T2: OpenCode with no vendor configuration: the same binary as T5 (OSIS-AI), started bare (bench.osisai).

Same skill snapshot (native skill loading), MCP servers, gateway model and time limit as T5; no AGENTS.md,
commands, prompts, plugins (so no long-term memory), sub-agents or permission rules. Questions get the same
default answer as T5.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from bench import osisai
from methods._common import base_metadata, fail, finish, main, normalize_tokens
from methods.t5_osisai.adapter import chat


def run(request: dict[str, Any], *, chat_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    metadata = base_metadata(request, framework="opencode", interaction_mode="session", skill_loading="opencode_native")
    try:
        if chat_fn is None:
            with osisai.Server(request, bare=True) as server:
                output = chat({**request, "opencode_url": server.url})
        else:
            output = chat_fn(request)
        answer = str(output.get("raw") or "").strip()
        if not answer:
            raise RuntimeError("OpenCode returned no final answer")
        metadata.update(final_answer=answer, tokens=normalize_tokens(output.get("usage")),
                        latency_s=output.get("latency_s"), status="completed", stop_reason="completed")
    except Exception as exc:  # noqa: BLE001
        fail(metadata, exc)
    return finish(request, metadata, started)


if __name__ == "__main__":
    raise SystemExit(main(run))
