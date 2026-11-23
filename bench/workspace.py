"""Path policy for candidate edits made through harness tools."""

from __future__ import annotations

import os
from pathlib import Path

ALLOWED_SUFFIXES = frozenset({".py", ".md", ".json", ".yaml", ".yml", ".txt"})


class PathPolicyError(ValueError):
    pass


def inside(root: Path, relative: str) -> Path:
    """``root / relative`` if it stays under root, checked lexically.

    Not ``Path.resolve()``: on Windows it can return an extended-length (``\\\\?\\``) path while another thread
    creates a parent directory, which then fails the containment check (seen with parallel tool calls).
    """
    raw = Path(str(relative))
    if raw.is_absolute() or ".." in raw.parts or not raw.parts:
        raise PathPolicyError("path must be relative and cannot contain '..'")
    path = Path(os.path.normpath(root / raw))
    if not path.is_relative_to(root):
        raise PathPolicyError("path escapes the project")
    return path


class CandidateWorkspace:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str, *, for_write: bool = False) -> Path:
        path = inside(self.root, relative)
        if for_write and path.suffix.lower() not in ALLOWED_SUFFIXES:
            raise PathPolicyError(f"file type is not writable: {path.suffix}")
        return path

    def read_text(self, relative: str) -> str:
        return self.resolve(relative).read_text(encoding="utf-8", errors="replace")

    def write_text(self, relative: str, content: str) -> Path:
        target = self.resolve(relative, for_write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        return target

    def list_files(self) -> list[str]:
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
