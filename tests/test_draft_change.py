"""Geometry change cases keep the start's design conditions in the reference."""

import pytest

from bench.config import load_config, resolve
from scoring import cases
from tools.draft_build import SHORT
from tools.draft_change import CONDITIONS, PLAN, geometry


def test_geometry_reference_keeps_the_start_design_conditions(tmp_path):
    skills = resolve(load_config()["parent_repo"]) / "skills"
    if not skills.is_dir():
        pytest.skip("parent repo not found")
    names = cases.templates(skills)
    for bridge in ("osis-bridge-hollow-slab", "osis-bridge-rigid-frame-box"):
        body = geometry(bridge, *PLAN[bridge][0], names, tmp_path / SHORT[bridge])  # asserts the pinned facts
        assert body["reference"] == "overlay:reference" and not set(body["may_change"]) & set(CONDITIONS)
