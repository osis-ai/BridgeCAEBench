"""Fixtures for the evaluator: the real templates are in skills/."""

from __future__ import annotations

from pathlib import Path

import pytest



ROOT = Path(__file__).resolve().parents[2]
REAL_PARENT = ROOT  # skills/ holds the frozen skill library with its templates


@pytest.fixture
def repo_root() -> Path:
    return ROOT


@pytest.fixture
def parent_root() -> Path:
    return REAL_PARENT
