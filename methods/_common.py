"""Helpers used inside method processes. Stdlib only: T2 and T5 run in the parent repo's Python."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Libraries install a small default loop bound when the argument is omitted.
# The experiment stops on wall time, not steps; this only disables that default.
LIBRARY_LOOP_BOUND = 1_000_000


def load_request(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    return json.loads(Path(args.request).read_text(encoding="utf-8"))


def package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def model_settings(request: dict[str, Any]) -> dict[str, Any]:
    model = request["model"]
    max_tokens = int(model.get("max_output_tokens") or 0)
    return {
        "model": model["name"],
        "base_url": model["base_url"],
        "api_key": os.environ.get(model.get("api_key_env") or "OSIS_MODEL_API_KEY", ""),
        "temperature": None if model.get("temperature", 0.0) is None else float(model.get("temperature", 0.0)),  # null: not sent
        "seed": int(request.get("seed", 0)),
        "reasoning_effort": str(model.get("reasoning_effort") or "low"),
        "timeout": float(model.get("request_timeout_s", 300)),
        "max_tokens": max_tokens or None,
    }


def normalize_tokens(value: Any) -> dict[str, int]:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        return {}
    aliases = {
        "prompt_tokens": ("prompt_tokens", "input_tokens"),
        "completion_tokens": ("completion_tokens", "output_tokens"),
        "total_tokens": ("total_tokens",),
    }
    result: dict[str, int] = {}
    for target, keys in aliases.items():
        for key in keys:
            if isinstance(value.get(key), (int, float)):
                result[target] = int(value[key])
                break
    if "total_tokens" not in result and result:
        result["total_tokens"] = result.get("prompt_tokens", 0) + result.get("completion_tokens", 0)
    return result


def base_metadata(request: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"method": request["method"], "task": request["task"], "model_calls": 0,
            "tool_calls": 0, "status": "failed", **extra}


def fail(metadata: dict[str, Any], exc: BaseException) -> None:
    metadata.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:800], stop_reason="error")


def finish(request: dict[str, Any], metadata: dict[str, Any], started: float) -> dict[str, Any]:
    result = dict(metadata)
    result["elapsed_s"] = round(max(0.0, time.monotonic() - started), 3)
    tools = sys.modules.get("bench.tools")  # not imported = no harness tools ran
    result["tool_s"] = round(sum(tools.TOOL_SECONDS), 3) if tools else 0.0
    path = Path(request["run_dir"]) / "generation.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def end_turn(request: dict[str, Any], answer: str) -> None:
    """project line: record the turn that just ended in ``run_dir/turn<k>/``: the reply, when it ended, what the
    agent left in OSIS (read now, before the next turn changes it) and a copy of its code. No-op on other lines."""
    if "turns" not in request:
        return
    from bench import osis  # httpx: present in every method interpreter, unlike the stdlib-only rest of this file

    run_dir, py = Path(request["run_dir"]), Path(request["work_dir"]) / "py"
    k = len(list(run_dir.glob("turn*/turn.json"))) + 1
    state = osis.inspect_project(py, request["broker_url"], run_dir / f"turn{k}" / "model")
    for name in ("_bench_inspect.py", "_bench_inspect.json"):  # the agent's next turn should not see them
        (py / name).unlink(missing_ok=True)
    shutil.copytree(py, run_dir / f"turn{k}" / "project" / "py", ignore=shutil.ignore_patterns("__pycache__"))
    (run_dir / f"turn{k}" / "turn.json").write_text(json.dumps(
        {"turn": k, "ended_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "answer": answer,
         "state": state}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(run: Callable[[dict[str, Any]], dict[str, Any]]) -> int:
    run(load_request())
    return 0
