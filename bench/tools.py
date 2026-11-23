"""Method-neutral tool surface. Tasks pick tool names in config; methods wrap them in their framework API."""

from __future__ import annotations

import functools
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bench.skills import SkillAdapter
from bench.workspace import CandidateWorkspace

_EVENTS_LOCK = threading.Lock()
TOOL_SECONDS: list[float] = []  # every harness tool call's wall time; finish() reports the sum as tool_s


def logged(function: Callable[..., str]) -> Callable[..., str]:
    """One stdout line per call; dispatch sends stdout to method.log, which is what monitors read."""

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        started, started_at = time.monotonic(), datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        result, error = "", None
        try:
            result = function(*args, **kwargs)
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            elapsed = time.monotonic() - started
            TOOL_SECONDS.append(elapsed)
            if os.environ.get("BENCH_RUN_DIR"):
                with _EVENTS_LOCK, (Path(os.environ["BENCH_RUN_DIR"]) / "tool_calls.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"started_at": started_at, "tool": function.__name__,
                                        "args": list(args), "kwargs": kwargs, "duration_s": round(elapsed, 3),
                                        "result_chars": len(str(result)), "tool_error": str(result).startswith("TOOL_ERROR"),
                                        "exception": error, "result": str(result)},
                                       ensure_ascii=False, default=str) + "\n")
        shown = json.dumps([*args, kwargs] if kwargs else list(args), ensure_ascii=False, default=str)
        print(f"[tool] {function.__name__} {shown[:200]} -> {len(str(result))} chars"
              f"{' ERROR' if str(result).startswith('TOOL_ERROR') else ''} {time.monotonic() - started:.1f}s",
              flush=True)
        return result

    return wrapper


def _error(exc: BaseException) -> str:
    return f"TOOL_ERROR: {type(exc).__name__}: {exc}"


class Toolbox:
    def __init__(self, request: dict[str, Any]) -> None:
        self.skills = SkillAdapter(request["skills_dir"])
        work_dir = request.get("work_dir") or ""
        self.candidate = CandidateWorkspace(work_dir) if work_dir else None
        self.writable = bool(request.get("writable"))

    def list_skills(self) -> str:
        """List mounted skills (id and description) as JSON."""
        return json.dumps(self.skills.skill_index(), ensure_ascii=False)

    def read_skill(self, skill_id: str) -> str:
        """Read one mounted SKILL.md.

        Args:
            skill_id: Exact skill directory name.
        """
        return self.read_knowledge_file(f"{skill_id}/SKILL.md")

    def read_skill_reference(self, skill_id: str, relative_path: str) -> str:
        """Read one file inside a mounted skill directory.

        Args:
            skill_id: Exact skill directory name.
            relative_path: Path relative to that skill directory.
        """
        return self.read_knowledge_file(f"{skill_id}/{relative_path}")

    def list_reference_files(self, skill_id: str, subdir: str = "") -> str:
        """List files under one skill, optionally under one subdirectory of it.

        Args:
            skill_id: Exact skill directory name.
            subdir: Optional subdirectory inside the skill.
        """
        prefix = f"{skill_id}/{subdir}".rstrip("/")
        return json.dumps([f for f in self.skills.list_files() if f == prefix or f.startswith(prefix + "/")],
                          ensure_ascii=False)

    def search_skills(self, query: str) -> str:
        """Search every mounted skill file for space-separated terms; returns ranked paths.

        Args:
            query: Space-separated API names or concepts.
        """
        try:
            return json.dumps(self.skills.search(query), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    def read_knowledge_file(self, relative_path: str) -> str:
        """Read one mounted skill file by the path returned from list/search.

        Args:
            relative_path: Path relative to the skills root.
        """
        try:
            return self.skills.read_file(relative_path)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    def list_candidate_files(self) -> str:
        """List files in the candidate project as JSON."""
        return json.dumps(self.candidate.list_files() if self.candidate else [], ensure_ascii=False)

    def read_candidate_file(self, relative_path: str) -> str:
        """Read one candidate file.

        Args:
            relative_path: Path relative to the candidate project.
        """
        try:
            return self._candidate().read_text(relative_path)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    def write_candidate_file(self, relative_path: str, content: str) -> str:
        """Write one complete text file inside the candidate project.

        Args:
            relative_path: Path relative to the candidate project.
            content: Complete replacement file content.
        """
        if not self.writable:
            return "TOOL_ERROR: candidate is read-only for this task"
        try:
            self._candidate().write_text(relative_path, content)
            return f"wrote {relative_path} ({len(content)} chars)"
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    def _candidate(self) -> CandidateWorkspace:
        if self.candidate is None:
            raise RuntimeError("this task has no candidate project")
        return self.candidate

    def functions(self, names: list[str]) -> list[Callable[..., str]]:
        result = []
        for name in names:
            if not name.startswith("_") and callable(getattr(self, name, None)) and name != "functions":
                result.append(logged(getattr(self, name)))
            else:
                raise KeyError(f"unknown tool: {name}")
        return result
