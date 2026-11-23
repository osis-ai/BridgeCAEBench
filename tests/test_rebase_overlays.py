"""tools/rebase_overlays.py keeps planted changes and drops out-of-date pyosis spellings."""

from tools.rebase_overlays import rebase

TEMPLATE = [
    'engine.prop.creep_shrink.create(1, "收缩徐变", fcuk=55e6, avg_humidity=75.0, type_coeff=5.0, birth_by_shrinking=3)',
    'engine.prop.creep_shrink.create(2, "收缩徐变", fcuk=45e6, avg_humidity=75.0, type_coeff=5.0, birth_by_shrinking=3)',
    'engine.material.create(1, "C55", "CONC", "JTG3362_2018", "C55", 1, 0.05)',
    'engine.material.create(2, "C45", "CONC", "JTG3362_2018", "C45", 2, 0.05)',
    'engine.section.get(1).add_rebar_s("ShearStirrup", 3, 0.1, 0.00125664)',
    'engine.tendon.shape.create("N1", 2, "15-4", "g", "ARC3D", "c")',
]
OVERLAY = [
    'engine.prop.creep_shrink.create(1, "收缩徐变", 75.0, 7, 5.0, 3)',            # old spelling, one line for two
    'engine.material.create(1, "C30", "CONC", "JTG3362_2018", "C30", 1, 0.05)',  # planted
    'engine.material.create(2, "C45", "CONC", "JTG3362_2018", "C45", 1, 0.05)',  # old creep/shrinkage number
    'engine.section.get(1).add_rebar_s("SHEARSTIRRUP", 3, 0.1, 0.00125664)',    # old upper case
    'engine.tendon.prop.create("15-2", "IN", 3, 1, "GBT5224_2014", 15.2, 2)',    # planted insert
    'engine.tendon.shape.create("N1", 2, "15-2", "g", "ARC3D", "c")',            # planted
]


def test_rebase_keeps_only_planted_lines():
    lines, kept, problems = rebase(TEMPLATE, OVERLAY)
    assert problems == []
    assert lines == [TEMPLATE[0], TEMPLATE[1], OVERLAY[1], TEMPLATE[3], TEMPLATE[4], OVERLAY[4], OVERLAY[5]]
    assert len(kept) == 3


def test_rebase_reports_lost_template_lines():
    _, _, problems = rebase(TEMPLATE, OVERLAY[:3])
    assert problems
