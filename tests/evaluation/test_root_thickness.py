"""D4 根部底板：区分附近渐变拐点与支点处横梁。"""
from __future__ import annotations

from pathlib import Path

from scoring.evaluation.levels.model_conformance import collect_python_files
from scoring.evaluation.levels.model_conformance.common import _extract_params


def test_rigid_frame_root_thickness_distinguishes_breakpoint_and_diaphragm(
    parent_root: Path,
) -> None:
    template = (
        parent_root
        / "skills"
        / "osis-bridge-rigid-frame-box"
        / "references"
        / "templates"
        / "变截面连续刚构-95+180+95-双肢"
    )

    params = _extract_params(collect_python_files(template))

    assert params["H_root"] == 10.5
    assert params["T_root"] == 0.993828
    assert params["T_dia_root"] == 1.7
