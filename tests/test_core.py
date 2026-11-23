from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bench.skills import SkillAdapter, create_snapshot
from bench.status import pid_alive
from bench.tools import Toolbox


def test_snapshot_excludes_answers_and_is_read_only(parent: Path, tmp_path: Path) -> None:
    dest = tmp_path / "snap"
    manifest = create_snapshot(parent / "skills", dest, ["templates"])
    files = SkillAdapter(dest).list_files()
    assert "osis-bridge-x/references/notes.md" in files
    assert not any("templates" in f for f in files)
    assert manifest["sha256"] == SkillAdapter(dest).bundle_hash()
    with pytest.raises(PermissionError):
        (dest / "osis-engine" / "SKILL.md").write_text("tampered", encoding="utf-8")


def test_skill_adapter_rejects_escape(parent: Path) -> None:
    skills = SkillAdapter(parent / "skills")
    with pytest.raises(ValueError):
        skills.read_file("../outside.txt")
    assert skills.find_skill("osis-engine").name == "osis-engine"
    assert skills.skill_index()[1] == {"skill_id": "osis-engine", "description": "engine rules"}


def test_toolbox_policy(parent: Path, tmp_path: Path) -> None:
    request = {"skills_dir": str(parent / "skills"), "work_dir": str(tmp_path / "cand"),
               "writable": False}
    box = Toolbox(request)
    assert "TOOL_ERROR" in box.write_candidate_file("py/a.py", "x")
    assert "TOOL_ERROR" in box.read_knowledge_file("../../x")
    assert json.loads(box.search_skills("create_conc"))[0]["path"] == "osis-engine/SKILL.md"
    box.writable = True
    assert "TOOL_ERROR" in box.write_candidate_file("../escape.py", "x")
    assert box.write_candidate_file("py/a.py", "x").startswith("wrote")
    names = ["read_skill", "write_candidate_file"]
    assert [f.__name__ for f in box.functions(names)] == names
    with pytest.raises(KeyError):
        box.functions(["functions"])
    with pytest.raises(KeyError):  # WeKnora is an MCP server now, not a harness tool
        box.functions(["hybrid_search"])


def test_pid_alive_does_not_kill() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(child.pid)
        assert child.poll() is None, "pid_alive must not terminate the process"
    finally:
        child.kill()
        child.wait()
    assert not pid_alive(child.pid)
    assert pid_alive(os.getpid())


def test_parallel_writes_into_new_dirs(tmp_path: Path) -> None:
    """Path.resolve() returned \\?\ paths under concurrent mkdir on Windows and refused valid writes."""
    from concurrent.futures import ThreadPoolExecutor

    from bench.workspace import CandidateWorkspace

    for trial in range(20):
        workspace = CandidateWorkspace(tmp_path / f"边梁-{trial}" / "project")
        with ThreadPoolExecutor(6) as pool:
            list(pool.map(lambda i: workspace.write_text(f"py/prep/_{i}.py", "x = 1\n"), range(6)))
        assert len(workspace.list_files()) == 6


def test_finish_reports_harness_tool_time(tmp_path: Path) -> None:
    from bench import tools
    from methods._common import finish

    tools.TOOL_SECONDS[:] = []
    tools.logged(lambda: "x")()
    assert finish({"run_dir": str(tmp_path)}, {}, 0.0)["tool_s"] >= 0.0
    assert len(tools.TOOL_SECONDS) == 1
