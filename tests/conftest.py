"""A minimal parent repo (skills only) and a config whose one task line is tests/fake_task.py."""

from __future__ import annotations

from pathlib import Path

import pytest

import tasks
from bench.config import load_config
from tests import fake_task


@pytest.fixture
def parent(tmp_path: Path) -> Path:
    root = tmp_path / "parent"
    skills = root / "skills"
    (skills / "osis-engine").mkdir(parents=True)
    (skills / "osis-engine" / "SKILL.md").write_text(
        "---\nname: osis-engine\ndescription: engine rules\n---\nengine.material.create_conc(no, ...)\n",
        encoding="utf-8")
    (skills / "osis-bridge-x" / "references" / "templates" / "t1").mkdir(parents=True)
    (skills / "osis-bridge-x" / "SKILL.md").write_text("---\nname: osis-bridge-x\n---\nbridge\n", encoding="utf-8")
    (skills / "osis-bridge-x" / "references" / "notes.md").write_text("notes\n", encoding="utf-8")
    (skills / "osis-bridge-x" / "references" / "templates" / "t1" / "answer.py").write_text("ANSWER\n",
                                                                                           encoding="utf-8")
    return root


@pytest.fixture
def config(parent: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    cfg = load_config()
    cfg["parent_repo"] = str(parent)
    cfg["runs_root"] = str(tmp_path / "runs")
    cfg["model"]["name"] = "fake-model"
    cfg["tasks"] = {"fake": {
        "items": [{"id": "q-001", "question": "create_conc first arg?", "answer": "no"},
                  {"id": "q-002", "question": "2+2?", "answer": "4"}],
        "timeout_s": 60, "skills_exclude": [], "mcp": [],
        "tools": ["list_skills", "read_skill", "read_skill_reference", "list_reference_files", "search_skills",
                  "read_knowledge_file"]}}
    monkeypatch.setitem(tasks.TASKS, "fake", fake_task)
    monkeypatch.setenv(cfg["model"]["api_key_env"], "test-key")
    return cfg
