"""竖向预应力:WEBVERTICALREBAR(支点±L/3) 或 loadcase 竖向 PST。"""

from __future__ import annotations

from pathlib import Path

from scoring.evaluation.levels.model_conformance import _has_vertical_tendon
from scoring.evaluation.levels.model_conformance.common import (
    _has_vertical_from_loadcase,
    _scan_web_vertical_section_nos,
)
import ast


def test_vertical_from_loadcase_name():
    files = {
        "prep/_8_loadcase.py": '''
def build(engine):
    engine.load.create("竖向预应力", "CS", 1.0)
    engine.load.get("竖向预应力").create("PST", "SV-1", "BOTH", "ST", 1.0, 1.0)
'''
    }
    assert _has_vertical_from_loadcase(files) is True
    assert _has_vertical_tendon(files) is True


def test_vertical_from_tendon_name():
    files = {
        "prep/_8_loadcase.py": '''
def build(engine):
    engine.load.create("0号块预应力", "CS", 1.0)
    engine.load.get("0号块预应力").create("PST", "竖向束1", "BOTH", "ST", 1.0, 1.0)
'''
    }
    assert _has_vertical_tendon(files) is True


def test_longitudinal_bbz_not_vertical():
    files = {
        "prep/_8_loadcase.py": '''
def build(engine):
    engine.tendon.shape.create("BB2-z", 2, "15-19", "BB2-zGp", "ARC2D", 1,
                               "BB2-z_VerCurve", "BB2-z_HorCurve")
    engine.load.create("左边跨预应力", "CS", 1.0)
    engine.load.get("左边跨预应力").create("PST", "BB2-z", "BOTH", "ST", 1.0, 1.0)
'''
    }
    assert _has_vertical_tendon(files) is False


def test_shape_without_pst_not_vertical():
    files = {
        "prep/_8_loadcase.py": '''
def build(engine):
    engine.tendon.shape.create("竖向束1", 2, "15-15", "g", "ARC3D", "c")
'''
    }
    assert _has_vertical_tendon(files) is False


def test_missing_files_is_false():
    assert _has_vertical_tendon({}) is False
    assert _has_vertical_tendon({"prep/_4_section.py": "x = 1"}) is False


def test_web_vertical_rebar_scanned():
    tree = ast.parse('''
def build(engine):
    engine.section.get(16).add_rebar_s("WEBVERTICALREBAR", 4, 0.5, 0.0024, 90.0, 567400.0, 0.8)
    engine.section.get(1).add_rebar_s("SHEARSTIRRUP", 3, 0.1, 0.001)
''')
    assert _scan_web_vertical_section_nos(tree) == {16}


def test_web_vertical_near_support_passes():
    """支点旁梁段挂 WEBVERTICALREBAR → True。"""
    files = {
        "prep/_4_section.py": '''
def build(engine):
    engine.section.create(1, "跨中", "CONVENTIONALBOX", 2.2, 6.0, 6.0, 3.0, 3.0, 0.5, 0.28, 0.3, 0.5, 0.5)
    engine.section.create(10, "墩顶", "CONVENTIONALBOX", 5.0, 6.0, 6.0, 3.0, 3.0, 0.9, 0.5, 0.8, 0.9, 0.9)
    engine.section.get(10).add_rebar_s("WEBVERTICALREBAR", 4, 0.5, 0.0024, 90.0, 567400.0, 0.8)
''',
        "prep/_5_node.py": '''
def build(engine):
    engine.node.create(1, 0.0, 0.0, 0.0)
    engine.node.create(2, 40.0, 0.0, 0.0)
    engine.node.create(3, 50.0, 0.0, 0.0)
    engine.node.create(4, 60.0, 0.0, 0.0)
    engine.node.create(5, 100.0, 0.0, 0.0)
    engine.node.create(99, 50.0, 2.0, -5.0)  # 墩外节点 → support_xs
''',
        "prep/_6_element.py": '''
def build(engine):
    engine.element.create(1, "BEAM3D", 1, 2, 1, 1, 1, 1, 1, 0.0, 0, 0.0, 0)
    engine.element.create(2, "BEAM3D", 2, 3, 1, 10, 10, 1, 1, 0.0, 0, 0.0, 0)
    engine.element.create(3, "BEAM3D", 3, 4, 1, 10, 10, 1, 1, 0.0, 0, 0.0, 0)
    engine.element.create(4, "BEAM3D", 4, 5, 1, 1, 1, 1, 1, 0.0, 0, 0.0, 0)
''',
    }
    # L=100 → L/3≈33.3; support at 50; beams 2/3 near pier with sec 10
    assert _has_vertical_tendon(files, L=100.0, support_xs=[50.0]) is True


def test_web_vertical_only_far_from_support_fails():
    """仅跨中有 WEBVERTICALREBAR、远离支点 → False。"""
    files = {
        "prep/_4_section.py": '''
def build(engine):
    engine.section.create(1, "跨中", "CONVENTIONALBOX", 2.2, 6.0, 6.0, 3.0, 3.0, 0.5, 0.28, 0.3, 0.5, 0.5)
    engine.section.get(1).add_rebar_s("WEBVERTICALREBAR", 4, 0.5, 0.0024, 90.0, 567400.0, 0.8)
    engine.section.create(10, "墩顶", "CONVENTIONALBOX", 5.0, 6.0, 6.0, 3.0, 3.0, 0.9, 0.5, 0.8, 0.9, 0.9)
''',
        "prep/_5_node.py": '''
def build(engine):
    engine.node.create(1, 0.0, 0.0, 0.0)
    engine.node.create(2, 49.0, 0.0, 0.0)
    engine.node.create(3, 51.0, 0.0, 0.0)
    engine.node.create(4, 100.0, 0.0, 0.0)
    engine.node.create(99, 0.0, 2.0, -2.0)
    engine.node.create(98, 100.0, 2.0, -2.0)
''',
        "prep/_6_element.py": '''
def build(engine):
    # 仅跨中短单元挂 sec1(有竖筋);墩旁无
    engine.element.create(1, "BEAM3D", 1, 2, 1, 10, 10, 1, 1, 0.0, 0, 0.0, 0)
    engine.element.create(2, "BEAM3D", 2, 3, 1, 1, 1, 1, 1, 0.0, 0, 0.0, 0)
    engine.element.create(3, "BEAM3D", 3, 4, 1, 10, 10, 1, 1, 0.0, 0, 0.0, 0)
''',
    }
    # supports at ends 0 and 100; L=100 → L/3≈33; mid at 50 is outside both
    assert _has_vertical_tendon(files, L=100.0, support_xs=[0.0, 100.0]) is False


def test_real_template_45_80_has_web_vertical(parent_root: Path):
    prep = (
        parent_root
        / "skills/osis-bridge-cantilever-box/references/templates"
        / "变截面悬浇连续梁-45+80+45/prep"
    )
    if not prep.is_dir():
        return
    files = {f"prep/{p.name}": p.read_text(encoding="utf-8") for p in prep.glob("*.py")}
    # loadcase  alone 仍可能 False
    assert _has_vertical_from_loadcase(files) is False
    # 但截面腹板竖筋 + 支点范围 → True
    from scoring.evaluation.levels.model_conformance.common import _support_xs, _parse

    sx = _support_xs(_parse(files, "prep/_5_node.py"))
    assert _has_vertical_tendon(files, L=80.0, support_xs=sx) is True


def test_real_template_76_140_has_web_vertical(parent_root: Path):
    """重生成后的 76+140+76 模板含 WEBVERTICALREBAR(L=140>75,应有竖向预应力)。"""
    prep = (
        parent_root
        / "skills/osis-bridge-cantilever-box/references/templates"
        / "变截面悬浇连续梁-76+140+76/prep"
    )
    if not prep.is_dir():
        return
    files = {f"prep/{p.name}": p.read_text(encoding="utf-8") for p in prep.glob("*.py")}
    from scoring.evaluation.levels.model_conformance.common import _support_xs, _parse

    sx = _support_xs(_parse(files, "prep/_5_node.py"))
    assert _has_vertical_tendon(files, L=140.0, support_xs=sx) is True
