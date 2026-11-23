"""小箱梁 D4：钢绞线用量 56~66 kg/m³ 延续线性扣分至满分。"""
from __future__ import annotations

import pytest

from scoring.evaluation.levels.model_conformance.bridges.small_box import (
    _score_small_box_d4_economy,
)


@pytest.mark.parametrize(
    ("steel_ratio", "steel_score"),
    [
        (46.0, 1.0),
        (56.0, 0.7),
        (57.1, 0.623),
        (61.0, 0.35),
        (66.0, 0.0),
        (67.0, 0.0),
    ],
)
def test_small_box_high_steel_ratio_continues_linear_deduction(
    steel_ratio: float,
    steel_score: float,
) -> None:
    _, details = _score_small_box_d4_economy(
        concrete_ratio=0.5,
        pst_steel_ratio=steel_ratio,
    )

    assert details["sub_scores"]["steel"] == pytest.approx(steel_score)


def test_small_box_high_steel_ratio_note() -> None:
    score, details = _score_small_box_d4_economy(
        concrete_ratio=0.5427,
        pst_steel_ratio=57.1,
    )

    assert score == pytest.approx(0.8115)
    assert details["issues"] == ["钢绞线用量 57.1 kg/m³ 偏高(56~66),扣 0.38"]
