from __future__ import annotations

from pathlib import Path

import pytest

from bench.dispatch import DispatchError, dispatch
from bench.parent import Parent

SPEC = {"id": "TX", "python": "main", "module": "tests.fake_method"}


def _request(tmp_path: Path, mode: str, timeout: float = 60) -> dict:
    run_dir = tmp_path / mode
    run_dir.mkdir()
    return {"method": "TX", "task": "fake", "run_dir": str(run_dir), "timeout_s": timeout, "native": {"mode": mode},
            "model": {"base_url": "http://127.0.0.1:9/v1"}}


def test_dispatch_runs_method_in_subprocess(parent: Path, tmp_path: Path) -> None:
    out = dispatch(SPEC, _request(tmp_path, "ok"), Parent(parent))
    assert out["status"] == "completed" and out["final_answer"] == "FINAL ANSWER: no"


def test_dispatch_timeout_is_scored_not_retried(parent: Path, tmp_path: Path) -> None:
    out = dispatch(SPEC, _request(tmp_path, "sleep", timeout=2), Parent(parent))
    assert out["status"] == "failed" and out["stop_reason"] == "timeout"


def test_dispatch_crash_is_infrastructure(parent: Path, tmp_path: Path) -> None:
    with pytest.raises(DispatchError):
        dispatch(SPEC, _request(tmp_path, "crash"), Parent(parent))


def test_missing_venv_names_the_fix(parent: Path, tmp_path: Path) -> None:
    with pytest.raises(DispatchError, match="osis-bench env --method TZ"):
        dispatch({"id": "TZ", "python": "venv", "extra": "tz", "module": "x"}, _request(tmp_path, "ok"), Parent(parent))
