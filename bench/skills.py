"""Read-only access to a frozen skill snapshot, and snapshot creation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path

_FRONT_NAME = re.compile(r"(?m)^name:\s*(?P<v>.+?)\s*$")
_FRONT_DESC = re.compile(r"(?m)^description:\s*(?P<v>.+?)\s*$")


class SkillAdapter:
    def __init__(self, skills_dir: str | Path, *, max_read_bytes: int = 200_000) -> None:
        self.root = Path(skills_dir).resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(self.root)
        self.max_read_bytes = max_read_bytes

    def _inside(self, relative: str) -> Path:
        raw = Path(str(relative))
        if not str(relative) or raw.is_absolute() or ".." in raw.parts:
            raise ValueError("path must be relative and stay inside the skills directory")
        path = (self.root / raw).resolve()
        path.relative_to(self.root)
        return path

    def list_files(self) -> list[str]:
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())

    def read_file(self, relative: str) -> str:
        path = self._inside(relative)
        if not path.is_file():
            raise FileNotFoundError(relative)
        return path.read_bytes()[: self.max_read_bytes].decode("utf-8", errors="replace")

    def skill_index(self) -> list[dict[str, str]]:
        index = []
        for child in sorted(self.root.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.is_file():
                head = skill_md.read_text(encoding="utf-8", errors="replace")[:4000]
                desc = _FRONT_DESC.search(head)
                index.append({"skill_id": child.name, "description": desc.group("v") if desc else ""})
        return index

    def find_skill(self, name: str) -> Path | None:
        """Match a skill by directory name or by its frontmatter ``name``."""
        for child in sorted(self.root.iterdir()):
            skill_md = child / "SKILL.md"
            if not child.is_dir() or not skill_md.is_file():
                continue
            if child.name == name:
                return child
            match = _FRONT_NAME.search(skill_md.read_text(encoding="utf-8", errors="replace")[:4000])
            if match and match.group("v") == name:
                return child
        return None

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, object]]:
        terms = [t.casefold() for t in query.split() if t.strip()]
        if not terms:
            return []
        hits = []
        for relative in self.list_files():
            try:
                text = self.read_file(relative).casefold()
            except (OSError, ValueError):
                continue
            score = sum(text.count(t) for t in terms)
            if score:
                hits.append({"path": relative, "score": score})
        return sorted(hits, key=lambda h: (-int(h["score"]), str(h["path"])))[:limit]

    def bundle_hash(self) -> str:
        digest = hashlib.sha256()
        for relative in self.list_files():
            digest.update(relative.encode("utf-8") + b"\0")
            digest.update((self.root / relative).read_bytes() + b"\0")
        return digest.hexdigest()

    def fixed_bundle_text(self) -> str:
        parts = [f"# Skill snapshot sha256={self.bundle_hash()}"]
        for relative in self.list_files():
            parts.extend((f"\n## {relative}", self.read_file(relative)))
        return "\n".join(parts)


def create_snapshot(source: str | Path, destination: str | Path, exclude: list[str] | tuple[str, ...] = ()) -> dict:
    """Copy ``source`` skills, drop any path containing an excluded part, make it read-only.

    Read-only matters: T3/T4 get a real interpreter or shell, and every run of a
    campaign shares this snapshot.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_dir():
        raise NotADirectoryError(source)
    if destination.exists():
        raise FileExistsError(destination)
    banned = {part.casefold() for part in exclude}
    files = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if banned & {part.casefold() for part in relative.parts} or not path.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        os.chmod(target, stat.S_IREAD)
        files += 1
    if not files or not any(destination.rglob("SKILL.md")):
        raise RuntimeError(f"snapshot from {source} contains no SKILL.md")
    leaked = [p for p in destination.rglob("*") if banned & {x.casefold() for x in p.relative_to(destination).parts}]
    if leaked:
        raise RuntimeError(f"excluded paths survived: {leaked[:3]}")
    manifest = {"source": str(source), "exclude": list(exclude), "files": files,
                "sha256": SkillAdapter(destination).bundle_hash()}
    (destination.parent / f"{destination.name}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
