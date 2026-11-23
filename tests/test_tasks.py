"""Task lines on OSIS projects: scoring reads what the agent left (pyosis inspection), never solves."""

import json
import os
import shutil
import subprocess
import types

import pytest

import tasks
from bench import osis
from bench.config import load_config, resolve
from scoring import answers, cases, judge
from scoring.checks import preserve
from scoring.extract import extract
from tasks import _project, analyze, build, diagnose, knowledge, project

PROTOCOL = ("SERIAL", "load_items", "item_id", "hidden", "prepare", "score", "summarize")


def test_every_line_has_the_protocol_and_cases():
    for name, module in tasks.TASKS.items():
        assert all(hasattr(module, f) for f in PROTOCOL), name
        items = module.load_items(None)
        ids = [module.item_id(i) for i in items]
        assert items and len(ids) == len(set(ids)), name
    for item in build.load_items(None):
        build.scored_checks(item)  # every must_ask term maps to check fields


def test_inspect_reads_results_and_check_tables(monkeypatch, tmp_path):
    def run(script, broker, timeout_s):  # what _INSPECT writes
        script.with_name("_bench_inspect.json").write_text(json.dumps(
            {"summary": {"nodes": 15}, "reactions": 40,
             "checks": {"混凝土_正截面抗弯验算_基本组合包络": ["OK"], "混凝土_PC正截面长期抗裂验算_准永久组合包络": ["NG", "OK"]}},
            ensure_ascii=False), encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr(osis, "run_script", run)
    state = osis.inspect(tmp_path, "broker", tmp_path / "model")
    assert state["solved"] and state["ng"] == ["PC正截面长期抗裂验算"] and state["tables"] == 2


def test_code_edited_after_the_last_solve_is_recorded_not_penalised(monkeypatch, tmp_path):
    py = tmp_path / "project" / "py"
    (py / "prep").mkdir(parents=True)
    (py.parent / "OSIS.out").write_text("solved", encoding="utf-8")
    (py / "prep" / "main.py").write_text("x = 1", encoding="utf-8")
    monkeypatch.setattr(osis, "inspect", lambda *a, **k: {"solved": True, "ng": None})
    ctx = types.SimpleNamespace(config={"osis": {"broker_url": "b"}})
    os.utime(py / "prep" / "main.py", (1, 1))
    assert not _project.inspect(ctx, tmp_path)["code_after_solve"]
    os.utime(py / "prep" / "main.py", (2e9, 2e9))
    state = _project.inspect(ctx, tmp_path)
    assert state["solved"] and state["code_after_solve"]


def test_behaviour_counts_a_script_that_raised_as_an_osis_error(tmp_path):
    calls = [{"server": "osis", "tool": "execute_python", "arguments": {"file": "py/prep/main.py"}, "result": '{"ok": true}'},
             {"server": "osis", "tool": "execute_python", "arguments": {"file": "a.py"}, "result": '{"ok": false, "stderr": "x"}'},
             {"server": "osis", "tool": "execute_python", "arguments": {"file": "b.py"}, "is_error": True, "result": "boom"},
             {"server": "osis", "tool": "execute_python", "arguments": {"file": r"D:\p\py\_t4_gate.py"}, "result": '{"ok": false}'}]
    (tmp_path / "mcp_calls.jsonl").write_text("\n".join(json.dumps(c) for c in calls), encoding="utf-8")
    b = _project.behaviour(tmp_path)
    assert b["osis_runs"] == 3 and b["osis_errors"] == 2 and b["ran_main"]


def test_last_json_takes_the_last_object_with_the_key():
    text = '先看 {"a": 1}，结论：```json\n{"localizations":[{"reason_code":"x"}]}\n``` 再改 {"localizations":[{"reason_code":"y"}]}'
    assert _project.last_json(text, "localizations") == {"localizations": [{"reason_code": "y"}]}
    assert _project.last_json("no json", "a") is None


def test_build_missing_case_drops_withheld_checks():
    item = {"id": "b", "must_ask": ["梁高", "沉降"], "checks": [
        {"field": "H_mid"}, {"field": "H_root"}, {"field": "settlement_mm"}, {"field": "concrete_grade"}]}
    assert [c["field"] for c in build.scored_checks(item)] == ["concrete_grade"]
    with pytest.raises(KeyError):
        build.scored_checks({"id": "b", "must_ask": ["桥宽"], "checks": []})
    item = {"brief": "建一个 150+300+150m 连续梁，跨径按此。", "pushback": {"keywords": ["300m", "跨径", "斜拉", "不合理"]}}
    assert build.pushback(item, "主跨 300m 对连续梁不合理，建议斜拉桥") and not build.pushback(item, "已建好 300m 模型")
    assert not build.pushback(item, "已按 150+300+150m 跨径建模并求解")  # restating the brief is no pushback
    # every infeasible case can still be passed: at least two of its keywords are not in its brief
    for case in build.load_items(None):
        if case.get("kind") == "infeasible":
            assert sum(k not in case["brief"] for k in case["pushback"]["keywords"]) >= 2, case["id"]


def test_build_missing_case_scores_named_assumptions():
    item = {"must_ask": ["梁高", "沉降"]}
    assert build.assume(item, "假设：根部梁高 7.5m、跨中梁高 3.2m；支座沉降按 10mm 计。") == 1.0
    assert build.assume(item, "跨中梁高取 3.2m。沉降按规范取值。") == 0.5  # 沉降 named without a value
    assert build.assume(item, "模型已建好。") == 0.0


def test_build_judge_decides_pushback_and_assumptions_first(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "VERDICTS", tmp_path / "verdicts.jsonl")
    monkeypatch.setattr(_project, "inspect", lambda ctx, run_dir: {"solved": True})
    monkeypatch.setattr(_project, "static", lambda *a: {"checks": {"score": 1.0}})
    prompts = []

    def fake_run(command, **kwargs):
        prompts.append(kwargs["input"])
        said = "参数：" in kwargs["input"] or "斜拉" in kwargs["input"].split("待评回复")[-1]
        return types.SimpleNamespace(stdout=json.dumps({"correct": said, "reason": "r"}))

    monkeypatch.setattr(judge.subprocess, "run", fake_run)
    ctx = types.SimpleNamespace(task_cfg={"judge": {"cli": "pi", "model": "m"}}, config={"model": {"name": "deepseek-v4.1-flash"}})
    infeasible = next(i for i in build.load_items(None) if i["kind"] == "infeasible")
    generation = {"status": "completed", "elapsed_s": 1}
    rec = build.score(ctx, infeasible, tmp_path, {**generation, "final_answer": "主跨这么大建议改斜拉桥"})
    assert rec["line_pushback"] == 1.0 and rec["judge"]["model"] == "m" and infeasible["pushback"]["issue"] in prompts[0]
    item = {"line": "build", "id": "b", "kind": "missing", "brief": "需求", "must_ask": ["梁高", "沉降"], "checks": []}
    reply = "主梁高度取两米，沉降取五毫米"  # values in words: the rule finds none
    assert build.assume(item, reply) == 0.0
    assert build.score(ctx, item, tmp_path, {**generation, "final_answer": reply})["line_assume"] == 1.0
    assert len(prompts) == 3

    monkeypatch.setattr(judge.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="no json"))
    rec = build.score(ctx, item, tmp_path, {**generation, "final_answer": "梁高取 2.2m"})  # judge fails: the rule
    assert rec["line_assume"] == 0.5 and rec["judge"] == {"梁高": None, "沉降": None}


def test_diagnose_localize_and_repair(tmp_path):
    item = {"kind": "check_ng", "localization": [{"reason_code": "rebar_num"}], "target_item": "正截面抗弯验算",
            "ng_seeded": ["正截面抗弯验算", "斜截面抗剪验算"]}
    assert diagnose.localize(item, '{"localizations":[{"check_item":"正截面抗弯验算","reason_code":"rebar_num"}]}') == 1.0
    assert diagnose.localize(item, "改好了") == 0.0
    for which, text in (("project", "post"), ("start", "post")):
        (tmp_path / which / "py" / "post").mkdir(parents=True)
        (tmp_path / which / "py" / "post" / "main.py").write_text(text, encoding="utf-8")
    target_ok = {"混凝土_正截面抗弯验算_基本组合包络": ["OK"], "混凝土_斜截面抗剪验算_基本组合包络": ["NG", "OK"]}
    assert diagnose.repair(item, tmp_path, {"ng": ["斜截面抗剪验算"], "checks": target_ok}, {}) == 1.0
    missing = {"混凝土_斜截面抗剪验算_基本组合包络": ["NG"]}  # the target's table was never produced
    assert diagnose.repair(item, tmp_path, {"ng": ["斜截面抗剪验算"], "checks": missing}, {}) == 0.0
    still_ng = {**target_ok, "钢筋_正截面抗弯验算_基本组合包络": ["NG"]}
    assert diagnose.repair(item, tmp_path, {"ng": ["正截面抗弯验算", "斜截面抗剪验算"], "checks": still_ng}, {}) == 0.0
    assert diagnose.repair(item, tmp_path, {"ng": None, "checks": {}}, {}) == 0.0  # post/main.py never run
    broke = {**target_ok, "混凝土_PC正截面短期抗裂验算_频遇组合包络": ["NG"]}
    assert diagnose.repair(item, tmp_path, {"ng": ["PC正截面短期抗裂验算"], "checks": broke}, {}) == 0.0
    (tmp_path / "project" / "py" / "post" / "main.py").write_text("edited", encoding="utf-8")
    assert diagnose.repair(item, tmp_path, {"ng": [], "checks": target_ok}, {}) == 0.0  # passed by editing the checks
    review = {"kind": "review", "localization": [{"item": "混凝土等级"}, {"item": "张拉控制应力"}]}
    reply = '{"inconsistencies":[{"item":"混凝土等级"},{"item":"跨径"}]}'
    assert diagnose.localize(review, reply) == 0.5


def test_preserve_compares_line_loads_per_case():
    case = cases.load(cases.DATASETS / "change" / "change-hollow-002.yaml")
    parent = resolve(load_config()["parent_repo"])
    if not (parent / "skills").is_dir():
        pytest.skip("parent repo not found")
    start = extract(cases.templates(parent / "skills")[case["base"]])
    exclude = {c["field"] for c in case["checks"]} | set(case["may_change"])
    paved = {**start, "line_loads": {**start["line_loads"], "铺装工况": [-11220.0]}}
    assert preserve(start, paved, exclude)["score"] == 1.0
    also = {**paved, "line_loads": {**paved["line_loads"], "防撞护栏工况": [-3000.0]}}  # an undeclared load moved too
    kept = preserve(start, also, exclude)
    assert kept["score"] < 1.0 and kept["changed"] == ["line_loads.防撞护栏工况"]
    added = {**paved, "line_loads": {**paved["line_loads"], "新增工况": [-1.0]}}
    assert preserve(start, added, exclude)["changed"] == ["line_loads.新增工况"]


def test_analyze_answer_by_tolerance_and_element():
    item = {"truth": {"value": 100.0, "element": [43, 44], "ok": "是"}, "tolerance": {"value": 0.02}}
    assert analyze.answer_score(item, {"value": 101.5, "element": 44, "ok": "是"}) == 1.0
    assert analyze.answer_score(item, {"value": 103, "element": "45", "ok": "否"}) == 0.0
    assert analyze.answer_score(item, None) == 0.0


def test_video_question_needs_the_video_link(tmp_path):
    item = next(i for i in knowledge.load_items(None) if i.get("video_url"))
    assert "VIDEO:" in knowledge.prepare(None, item, tmp_path)["instructions"]
    ctx = types.SimpleNamespace(task_cfg={}, config={"model": {"name": "deepseek-v4.1-flash"}})
    reply = f"FINAL ANSWER: {item['answer']}"
    generation = {"status": "completed", "elapsed_s": 1}
    assert knowledge.score(ctx, item, tmp_path, {**generation, "final_answer": reply})["correct"] == 0
    linked = f"VIDEO: {item['video_url']}?t=3\n{reply}"
    assert knowledge.score(ctx, item, tmp_path, {**generation, "final_answer": linked})["correct"] == 1


def test_grade_by_rubric(monkeypatch):
    rule = {"weights": {"quality": 0.7, "efficiency": 0.15, "cost": 0.15}, "gate": ["solved"],
            "parts": {"checks": 1, "line_assume": 1}, "by_kind": {"infeasible": {"gate": [], "parts": {"line_pushback": 1}}},
            "efficiency": {"scale": 100}, "cost": {"scale": 0.5}}
    monkeypatch.setattr(_project, "rubric", lambda line: rule)
    base = {"duration_s": 100, "cost_usd": 0.5}
    graded = _project.grade("build", {**base, "solved": 1.0, "checks": 0.5})  # no line_assume: left out
    assert graded == {"score": 0.5, "efficiency": 0.5, "cost": 0.5, "total": 0.5}
    assert _project.grade("build", {**base, "solved": 0.0, "checks": 1.0, "line_assume": 1.0})["score"] == 0.0
    refused = {**base, "kind": "infeasible", "solved": 0.0, "line_pushback": 1.0}
    assert _project.grade("build", refused)["score"] == 1.0  # the gate is off for this kind
    assert _project.grade("build", {**base, "error": "x", "solved": 1.0, "checks": 1.0})["total"] == 0.0


def test_duration_leaves_out_osis_time():
    ctx = types.SimpleNamespace(task_cfg={})
    generation = {"gateway": {"completion_tokens": 1000}, "tool_s": 70.0,
                  "mcp": {"osis": {"tool_s": 60.0}, "weknora": {"tool_s": 10.0}}}
    assert _project.normalized_duration(ctx, generation) == 20.0  # 1000 tokens at 100/s + weknora 10 s


def test_every_line_has_a_rubric():
    for line in ("knowledge", "build", "change", "handover", "diagnose", "analyze"):
        rule = _project.rubric(line)
        assert abs(sum(rule["weights"].values()) - 1) < 1e-9 and rule["parts"]


def test_answer_match_chinese_punctuation_and_lists():
    assert answers.match_answer("横向联系梁法。", "横向联系梁法")
    assert answers.match_answer("米,毫米,英寸", "米、毫米、英寸")
    assert not answers.match_answer("g", "Acel", ["Acel,g"])  # the command signature is one alias, not two


def test_judge_decides_first_caches_and_falls_back_to_the_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "VERDICTS", tmp_path / "verdicts.jsonl")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        verdict = json.dumps({"correct": "功能区" in kwargs["input"], "reason": "r"})
        return types.SimpleNamespace(stdout=f"some thinking {{not json}}\n{verdict}\n")

    monkeypatch.setattr(judge.subprocess, "run", fake_run)
    item = next(i for i in knowledge.load_items(None) if i["id"] == "kn-manual-009")
    ctx = types.SimpleNamespace(task_cfg={"judge": {"cli": "pi", "model": "m"}}, config={"model": {"name": "deepseek-v4.1-flash"}})
    generation = {"status": "completed", "elapsed_s": 1}
    reply = "FINAL ANSWER: 功能区菜单“特性”→“材料”→“收缩徐变”。"
    record = knowledge.score(ctx, item, tmp_path, {**generation, "final_answer": reply})
    assert record["correct"] == 1 and not record["rule_correct"] and record["judge"]["model"] == "m"
    knowledge.score(ctx, item, tmp_path, {**generation, "final_answer": reply})
    assert len(calls) == 1  # the second time comes from the cache
    exact = knowledge.score(ctx, item, tmp_path, {**generation, "final_answer": "FINAL ANSWER: 特性→材料→收缩徐变"})
    assert exact["rule_correct"] and exact["correct"] == 0 and len(calls) == 2  # the model decides even over the rule
    blank = knowledge.score(ctx, item, tmp_path, {**generation, "final_answer": "FINAL ANSWER: 查不到。"})
    assert blank["cannot_find"] and blank["correct"] == 0 and len(calls) == 2  # wrong by code, the model is not asked
    no_answer = next(i for i in knowledge.load_items(None) if i["type"] == "no_answer")
    found = knowledge.score(ctx, no_answer, tmp_path, {**generation, "final_answer": "FINAL ANSWER: 查不到"})
    assert not found["cannot_find"] and len(calls) == 3  # on a no-answer question it is the right reply

    def broken(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(judge.subprocess, "run", broken)
    for answer, expected in (("特性/材料/收缩徐变", 1), ("特性→截面", 0)):  # judge unavailable: the rule decides
        fallback = knowledge.score(ctx, item, tmp_path, {**generation, "final_answer": f"FINAL ANSWER: {answer}"})
        assert fallback["correct"] == expected and fallback["judge"] is None


def test_prepare_stages_start_without_answers(tmp_path):
    parent = resolve(load_config()["parent_repo"])
    if not (parent / "skills").is_dir():
        pytest.skip("parent repo not found")
    ctx = types.SimpleNamespace(parent=types.SimpleNamespace(skills=parent / "skills"), task_cfg={})
    item = next(i for i in diagnose.load_items(ctx) if i["kind"] == "check_ng")
    request = diagnose.prepare(ctx, item, tmp_path)
    assert (tmp_path / "project" / "py" / "post" / "main.py").is_file() and (tmp_path / "start" / "py").is_dir()
    assert item["localization"][0]["reason_code"] not in json.dumps(request, ensure_ascii=False)
    assert "post/main.py" in request["instructions"]
    run_error = next(i for i in diagnose.load_items(ctx) if i["kind"] == "run_error")
    assert "{symptom}" not in diagnose.prepare(ctx, run_error, tmp_path / "r")["instructions"]


def test_cost_is_the_list_price_of_what_the_gateway_counted():
    gateway = {"prompt_tokens": 1_000_000, "cached_tokens": 900_000, "completion_tokens": 100_000, "total_tokens": 1_100_000}
    generation = {"gateway": gateway, "tokens": {"total_tokens": 7}}  # the method's own count only covers its last turn
    assert _project.usage(generation) is gateway
    # deepseek-v4.1-flash at peak: 0.1M uncached x 0.30 + 0.9M cached x 0.006 + 0.1M out x 1.20
    assert _project.cost_usd("deepseek-v4.1-flash", gateway) == round(0.03 + 0.0054 + 0.12, 6)
    with pytest.raises(KeyError, match="no list price"):
        _project.cost_usd("unpriced-model", gateway)


def test_project_scores_each_turn_on_the_agents_own_previous_output(tmp_path, monkeypatch):
    parent = resolve(load_config()["parent_repo"])
    if not (parent / "skills").is_dir():
        pytest.skip("parent repo not found")
    skills = parent / "skills"
    ctx = types.SimpleNamespace(parent=types.SimpleNamespace(skills=skills), task_cfg={},
                                config={"model": {"name": "deepseek-v4.1-flash"}, "osis": {"broker_url": "b"}})
    item = next(i for i in project.load_items(ctx) if i["id"] == "project-hollow-001")
    request = project.prepare(ctx, item, tmp_path)
    assert len(request["turns"]) == 3 and "C55" in request["turns"][0] and request["broker_url"] == "b"
    # a perfect agent for turns 1-2 (the turn's reference is its output), then the session dies
    calls = []
    for k in (1, 2):
        cases.materialize({**item, **item["turns"][k - 1]}, "reference", skills, tmp_path / f"turn{k}" / "project" / "py")
        ended = f"2026-10-23T0{k}:00:00.000+00:00"
        (tmp_path / f"turn{k}" / "turn.json").write_text(json.dumps(
            {"turn": k, "ended_at": ended, "answer": "status: done", "state": {"solved": True}}), encoding="utf-8")
        calls.append({"started_at": f"2026-10-23T0{k - 1}:30:00.000+00:00",
                      "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000}})
    (tmp_path / "model_calls.jsonl").write_text("\n".join(json.dumps(c) for c in calls), encoding="utf-8")
    generation = {"status": "failed", "error": "TimeoutError", "gateway": {"total_tokens": 2_000_000}}
    monkeypatch.setattr(osis, "rebuild_model", lambda py, model, broker: shutil.copytree(py, model / "py") and None)
    record = project.score(ctx, item, tmp_path, generation)
    first, second = record["turns"][:2]
    assert first["quality"] == second["quality"] == 1.0 and second["preserve"] == 1.0
    assert first["cost_usd"] == second["cost_usd"] == 0.3  # one call each, 1M uncached input at $0.30
    assert record["turns_done"] == 2 and record["first_failed_turn"] == 3 and record["turn_quality"] == 0.5
    assert record["error"] is None and record["generation_error"] == "TimeoutError" and record["score"] == 0.5
