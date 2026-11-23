from scoring.extract import load_facts

LOAD = 'engine.load.get("铺装工况").create("LINE", 1, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, {}, 0.0)\n'


def test_line_load_through_a_module_constant():
    assert load_facts({"a.py": LOAD.format("-11220.0")})["line_load_kn"] == [11.22]
    assert load_facts({"a.py": "PAVEMENT = -11220.0\n" + LOAD.format("PAVEMENT")})["line_load_kn"] == [11.22]
    twice = "P = -1.0\nP = -11220.0\n" + LOAD.format("P")  # reassigned: not a constant
    assert load_facts({"a.py": twice})["line_load_kn"] == []


def test_exported_beam_line_load_and_positional_humidity():
    bline = 'engine.load.get("铺装工况").create("BLINE", 1, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, -9350.0, 0.0)\n'
    assert load_facts({"a.py": bline})["line_load_kn"] == [9.35]
    exported = 'engine.prop.creep_shrink.create(1, "收缩徐变", "JTG3362_2018", 50000000, 75, 1, 5, 3, 0)\n'
    assert load_facts({"a.py": exported})["humidity"] == 75


def test_an_integer_girder_height_is_the_height_not_a_section_number():
    import ast

    from scoring.evaluation.levels.model_conformance.common import _extract_section

    code = 'engine.section.create(17, "等高段", "CONVENTIONALBOX", {}, 6.25, 6.25, 3.75, 3.75, 0.5, 0.3, 0.32)'
    for h in ("3", "3.0"):
        section = _extract_section(ast.parse(code.format(h)).body[0].value)
        assert (section["no"], section["h"], section["tt"], section["tb"]) == (17, 3.0, 0.3, 0.32)
