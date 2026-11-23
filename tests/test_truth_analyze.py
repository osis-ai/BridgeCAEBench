"""Probe evaluation of tools/truth_analyze.py on a hand-made result dump (two spans of 10 m and a pier)."""

import pytest

from tools.truth_analyze import truth, truth_check

NODES = [[1, 0.0, 0, 0], [2, 5.0, 0, 0], [3, 10.0, 0, 0], [4, 15.0, 0, 0], [5, 20.0, 0, 0], [6, 10.0, 0, -8.0]]
ELEMENTS = [[1, "BEAM3D", 1, 2], [2, "BEAM3D", 2, 3], [3, "BEAM3D", 3, 4], [4, "BEAM3D", 4, 5], [5, "BEAM3D", 6, 3]]
FORCE = [{"ele": 1, "my": 1000.0}, {"ele": 1, "my": 4000.0}, {"ele": 2, "my": 4000.0}, {"ele": 2, "my": -9000.0},
         {"ele": 5, "my": 50000.0}]  # the pier (element 5) is not girder
DATA = {"nodes": NODES, "elements": ELEMENTS, "results": {
    "element_force||铺装": {"names": ["铺装_CS3"], "rows": FORCE},
    "support_reaction||铺装": {"names": ["铺装_CS3"], "rows": [{"no": 1, "fz": 2000.0}, {"no": 6, "fz": 9000.0}]},
    "envelope|displacement|活载": {"names": ["活载"], "rows": [
        {"component": "Uz-Min", "no": 4, "uz": -0.012}, {"component": "Uz-Max", "no": 4, "uz": 0.001},
        {"component": "Uz-Min", "no": 2, "uz": -0.020}]},
    "node_displacement||正梯度": {"names": ["正梯度"], "rows": [{"no": 4, "uz": 0.003}]},
    "node_displacement||负梯度": {"names": ["负梯度"], "rows": [{"no": 4, "uz": -0.001}]},
}}


def _case(probe):
    return {"probe": probe}


def test_extreme_over_girder_with_tied_elements():
    probe = {"call": "element_force", "load_case": "铺装", "column": "my", "agg": "max", "where": {"girder": True},
             "answer": {"value": "kN·m", "element": "ele"}}
    assert truth(_case(probe), DATA, [10, 10]) == {"value": 4.0, "element": [1, 2]}


def test_first_support_reaction_and_span_mid_envelope():
    reaction = {"call": "support_reaction", "load_case": "铺装", "column": "fz", "where": {"support": "first"},
                "answer": {"value": "kN"}}
    assert truth(_case(reaction), DATA, [10, 10]) == {"value": 2.0}
    live = {"call": "envelope", "kind": "displacement", "load_case": "活载", "component": "Uz-Min", "column": "uz",
            "agg": "min", "where": {"span_mid": 1}, "answer": {"value": "mm_down", "ok": "abs(value) <= 10 * 1000 / 600"}}
    assert truth(_case(live), DATA, [10, 10]) == {"value": 12.0, "ok": "是"}


def test_minus_and_ambiguous_name():
    probe = {"call": "node_displacement", "load_case": "正梯度", "column": "uz", "where": {"span_mid": 1},
             "minus": {"call": "node_displacement", "load_case": "负梯度", "column": "uz", "where": {"span_mid": 1}},
             "answer": {"value": "mm"}}
    assert truth(_case(probe), DATA, [10, 10]) == {"value": 4.0}
    data = {**DATA, "results": {**DATA["results"], "node_displacement||正梯度": {"names": ["a", "b"], "rows": []}}}
    with pytest.raises(ValueError):
        truth(_case(probe), data, [10, 10])


def test_check_truth_takes_the_extreme_over_the_item_tables():
    probe = {"item": "PC正截面压应力验算", "column": "安全系数", "agg": "min",
             "answer": {"ok": "value >= 1.0", "value": "", "element": "单元"}}
    data = {"tables": {"混凝土_PC正截面压应力验算_标准组合包络": [
        {"单元": 3, "安全系数": 1.42, "结果": "OK"}, {"单元": 7, "安全系数": 1.18, "结果": "OK"}],
        "混凝土_PC正截面压应力验算_短暂状况": [{"单元": 9, "安全系数": 1.18, "结果": "OK"}]}}
    assert truth_check(probe, data) == {"ok": "是", "value": 1.18, "element": [7, 9]}
    data["tables"]["混凝土_PC正截面压应力验算_短暂状况"][0].update(安全系数=0.9, 结果="NG")
    assert truth_check(probe, data) == {"ok": "否", "value": 0.9, "element": [9]}
    data["tables"]["混凝土_PC正截面压应力验算_短暂状况"][0]["结果"] = "OK"  # the formula and 结果 disagree
    with pytest.raises(ValueError):
        truth_check(probe, data)
    with pytest.raises(ValueError):
        truth_check(probe, {"tables": {}})
