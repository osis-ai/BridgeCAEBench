"""CONVENTIONALBOX 截面面积估算:含筋量(kg/m³)的分母不再缺失。"""
from __future__ import annotations

import ast

from scoring.evaluation.levels.model_conformance.common import (
    _box_section_area,
    _extract_section,
)

_CALL = (
    'engine.section.create(1, "跨中截面", "CONVENTIONALBOX", '
    "2.4, 6.375, 6.375, 3.5, 3.5, 0.5, 0.28, 0.32, 0.5, 0.5, "
    "1, 5.05, 4.5, 5.05, 5.05, "
    "0.0, 0.7, 1.5, 0.7, 1.0, 0.5, 0.5, 0.35, 0.6, 0.3, 1.0, 0.5, 0.6, 0.3, "
    "2.875, 0.2, 1.325, 0.7, 0.4, 1, 2.875, 0.2, 1.325, 0.7, 0.4, "
    '"Integral", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)'
)


def _parse_sec() -> dict:
    node = ast.parse(_CALL, mode="eval").body
    sec = _extract_section(node)
    assert sec is not None
    return sec


def test_box_fields_extracted() -> None:
    sec = _parse_sec()
    assert sec["type"] == "CONVENTIONALBOX"
    assert (sec["bt_l"], sec["bt_r"]) == (6.375, 6.375)
    assert (sec["bb_l"], sec["bb_r"]) == (3.5, 3.5)
    assert sec["tw2"] == 0.5
    assert (sec["bc_l"], sec["bc_r"]) == (2.875, 2.875)
    assert (sec["tc_l"], sec["tc_r"]) == (0.2, 0.2)
    assert (1.5, 0.7) in sec["box_chamfers"]


def test_box_section_area_value() -> None:
    a = _box_section_area(_parse_sec())
    # 顶板 7·0.28 + 翼缘 5.75·0.24 + 底板 7·0.32 + 腹板 1.0·1.8 + 倒角 ≈ 8.67
    assert a is not None
    assert abs(a - 8.67) < 0.1


def test_box_section_area_rejects_other_types() -> None:
    assert _box_section_area({"type": "TGIRDER", "h": 2.0}) is None
    assert _box_section_area(None) is None
