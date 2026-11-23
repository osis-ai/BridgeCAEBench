"""D5 纵向钢束含筋量：超分跨上限 10 kg/m³ 内线性扣分。"""
from __future__ import annotations

import pytest

from scoring.evaluation.levels.model_conformance.bridges.cantilever import (
    _d5_steel_ratio_deduction,
    _score_md_d5 as score_cantilever_d5,
)
from scoring.evaluation.levels.model_conformance.bridges.rigid_frame import (
    _score_md_d5 as score_rigid_d5,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (60.0, 0.0),
        (64.4, 0.44),
        (70.0, 1.0),
        (75.0, 1.0),
    ],
)
def test_steel_ratio_linear_deduction(value: float, expected: float) -> None:
    assert _d5_steel_ratio_deduction(value, 60.0) == pytest.approx(expected)


@pytest.mark.parametrize("score_d5", [score_cantilever_d5, score_rigid_d5])
def test_steel_ratio_linear_deduction_used_by_bridge_scorers(score_d5) -> None:
    _, details = score_d5(
        L=90.0,
        H_mid=None,
        total_length=None,
        has_vertical_tendon=True,
        pst_steel_ratio=64.4,
    )

    item = details["items"]["steel_ratio"]
    assert item["deduction"] == pytest.approx(0.44)
    assert item["score"] == pytest.approx(0.56)
    assert "60~70 线性扣分" in item["note"]
