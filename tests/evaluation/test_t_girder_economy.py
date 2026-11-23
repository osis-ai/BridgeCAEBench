"""T 梁经济性与标准化规则。"""
from __future__ import annotations

import pytest

from scoring.evaluation.levels.model_conformance.bridges.t_girder import (
    _score_t_girder_d4_economy,
    _score_t_girder_d5_standardization,
)
from scoring.evaluation.levels.model_conformance.report import _report_suggestion


@pytest.mark.parametrize(
    ("steel_ratio", "steel_score"),
    [
        (50.0, 1.0),
        (50.185, 0.9815),
        (55.0, 0.5),
        (60.0, 0.0),
        (61.0, 0.0),
    ],
)
def test_standard_t_girder_high_steel_ratio_is_linear(
    steel_ratio: float,
    steel_score: float,
) -> None:
    _, details = _score_t_girder_d4_economy(
        concrete_ratio=0.5,
        pst_steel_ratio=steel_ratio,
        variant="standard",
    )

    assert details["sub_scores"]["steel"] == pytest.approx(steel_score)


def test_standard_t_girder_high_steel_ratio_note() -> None:
    score, details = _score_t_girder_d4_economy(
        concrete_ratio=0.639,
        pst_steel_ratio=50.185,
        variant="standard",
    )

    assert score == pytest.approx(0.99075)
    assert details["issue"] == "钢绞线用量 50.2 kg/m³ 偏高(50~60),扣 0.02"


_COMPLETE_STAGES = ["主梁预制", "存梁", "二期恒载", "徐变十年"]


@pytest.mark.parametrize(
    ("beam_width", "expected_score"),
    [
        (1.0, 1.0),
        (1.2, 1.0),
        (1.55, 1.0),
        (0.99, 0.5),
        (1.56, 0.5),
    ],
)
def test_low_t_girder_width_band(
    beam_width: float,
    expected_score: float,
) -> None:
    score, _ = _score_t_girder_d5_standardization(
        beam_width=beam_width,
        stage_count=4,
        stage_names=_COMPLETE_STAGES,
        variant="low",
    )

    assert score == pytest.approx(expected_score)


def test_low_t_girder_missing_stages_deducts_half_point() -> None:
    score, details = _score_t_girder_d5_standardization(
        beam_width=1.2,
        stage_count=3,
        stage_names=["主梁预制", "存梁", "二期恒载"],
        variant="low",
    )

    assert score == pytest.approx(0.5)
    assert details["sub_scores"] == {"width": 1.0, "stage": 0.0}


def test_low_t_girder_report_suggests_low_width_band() -> None:
    suggestion = _report_suggestion(
        "D5_standardization",
        {"t_variant": "low"},
        "t_girder",
    )
    assert "1.0~1.55 m" in suggestion
