"""The skill library under test: skills/ in this repository, or the directory given by parent_skills."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class Parent:
    def __init__(self, root: str | Path, skills: str | Path | None = None) -> None:
        self.root = Path(root).resolve()
        self._skills = Path(skills).resolve() if skills else None
        if not self.skills.is_dir():
            raise FileNotFoundError(f"skill library not found: {self.skills}")

    @property
    def python(self) -> Path:
        return self.root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    @property
    def skills(self) -> Path:
        return self._skills or self.root / "skills"

    def path(self, relative: str | Path) -> Path:
        return (self.root / relative).resolve()

    def commit(self) -> str:
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=self.root, text=True, encoding="utf-8",
                stderr=subprocess.DEVNULL,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            return "unknown"
