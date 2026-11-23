"""Adapters with their framework runtime replaced; checks the request contract, not the frameworks."""

from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

import methods
from bench import osisai
from methods import mcp_cli
from methods.t1_miniswe import adapter as t1
from methods.t2_opencode import adapter as t2
from methods.t3_pipeline import adapter as t3
from methods.t4_gated import adapter as t4
from methods.t5_osisai import adapter as t5


@pytest.fixture
def request_for(parent: Path, tmp_path: Path, config: dict):
    def make(method: str, *, files: bool = False) -> dict:
        run_dir = tmp_path / f"{method}-{len(list(tmp_path.iterdir()))}"
        run_dir.mkdir()
        work = ""
        if files:
            work_path = run_dir / "candidate_project"
            (work_path / "py").mkdir(parents=True)
            (work_path / "py" / "main.py").write_text("x = 1\n", encoding="utf-8")
            work = str(work_path)
        return {"method": method, "task": "fake", "item_id": "i", "seed": 0,
                "run_dir": str(run_dir), "skills_dir": str(parent / "skills"), "work_dir": work,
                "writable": files, "deliverable": "files" if files else "answer",
                "instructions": "TASK TEXT", "tools": config["tasks"]["fake"]["tools"], "timeout_s": 60,
                "model": config["model"], "osisai": config["osisai"], "parent_repo": "",
                "native": {"item_id": "i"}}
    return make


def _record(request: dict) -> dict:
    return json.loads((Path(request["run_dir"]) / "generation.json").read_text(encoding="utf-8"))


def test_registry_has_the_five_designs() -> None:
    registry = methods.discover()
    assert sorted(registry) == ["T1", "T2", "T3", "T4", "T5"]
    assert registry["T5"]["serial"] and registry["T5"]["reset_dirs"] and methods.supports(registry["T5"], "build")
    assert registry["T1"]["extra"] == registry["T4"]["extra"] == "miniswe"  # T4 wraps T1 unchanged
    assert not methods.supports(registry["T4"], "knowledge") and methods.supports(registry["T4"], "analyze")


def test_t5_uses_parent_latency_and_tokens(request_for) -> None:
    request = request_for("T5")
    out = t5.run(request, chat_fn=lambda r: {"raw": "FINAL ANSWER: no", "latency_s": 12.5,
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                                   "total_tokens": 15}})
    assert out["latency_s"] == 12.5 and out["tokens"]["total_tokens"] == 15
    assert t5.run(request, chat_fn=lambda r: {"raw": ""})["status"] == "failed"


def test_t1_task_text_and_osis_mcp_shim(request_for) -> None:
    request = {**request_for("T1", files=True), "mcp_servers": {"osis": {"url": "http://127.0.0.1:1/mcp"}}}
    text = t1.task_text(request)
    assert text.startswith("TASK TEXT") and request["skills_dir"] in text and "osis-mcp call" in text
    assert "status: declined" in text and t1.SUBMIT in text
    env = t1._osis_mcp(request)
    assert (Path(request["run_dir"]) / "bin" / "osis-mcp").is_file()
    assert json.loads(env["OSIS_MCP_SERVERS"]) == {"osis": "http://127.0.0.1:1/mcp"}
    out = t1.run(request, runtime=lambda r, p: {"final_answer": "status: done\nok", "exit_status": "Submitted"})
    assert out["status"] == "completed"
    assert t1.run(request, runtime=lambda r, p: {"final_answer": "", "exit_status": "TimeExceeded"})["status"] == "failed"
    # a submit marker that was not the first output line: the answer file still counts
    agent = types.SimpleNamespace(messages=[], n_calls=1, model=types.SimpleNamespace(cuts=0),
                                  config=types.SimpleNamespace(output_path=Path(request["run_dir"]) / "trajectory.json"))
    (Path(request["run_dir"]) / t1.FINAL).write_text("status: done\nFINAL ANSWER: 1", encoding="utf-8")
    assert t1.summarize(agent, {"submission": "", "exit_status": "Submitted"})["final_answer"].endswith("ANSWER: 1")


def test_mcp_cli_rejects_unknown_server(monkeypatch, capsys) -> None:
    monkeypatch.setenv("OSIS_MCP_SERVERS", json.dumps({"osis": "http://x"}))
    assert mcp_cli.main(["call", "nope", "t", "{}"]) == 2 and "unknown server" in capsys.readouterr().err
    assert mcp_cli.main(["call", "osis", "t", "{bad"]) == 2


def test_t2_bare_pack_has_no_vendor_config(request_for, tmp_path) -> None:
    install = tmp_path / "install"
    (install / ".agents" / "plugins").mkdir(parents=True)
    (install / ".agents" / "AGENTS.md").write_text("vendor", encoding="utf-8")
    (install / ".agents" / "plugins" / "y-osis-memory.ts").write_text("", encoding="utf-8")
    (install / ".agents" / "opencode.json").write_text(json.dumps(
        {"$schema": "s", "share": "disabled", "agent": {"x": {}}, "plugin": ["p"],
         "permission": {"*": "allow", "bash": "ask"}}), encoding="utf-8")
    request = {**request_for("T2"), "mcp_servers": {"osis": {"url": "http://p/mcp"}}}
    vendor = osisai.build_pack(request, install, tmp_path / "vendor")
    bare = osisai.build_pack(request, install, tmp_path / "bare", bare=True)
    assert (vendor / "AGENTS.md").is_file() and not (bare / "AGENTS.md").exists() and not (bare / "plugins").exists()
    config = json.loads((bare / "opencode.json").read_text(encoding="utf-8"))
    assert set(config) == {"$schema", "share", "model", "small_model", "provider", "mcp", "permission"}
    assert config["permission"] == {"*": "allow", "bash": osisai.KILL_GUARD} and (bare / "skills").is_dir()
    limit = config["provider"][osisai.PROVIDER]["models"][request["model"]["name"]]["limit"]
    assert limit["context"] == request["model"]["context_tokens"] == 256000  # what OpenCode compacts against
    out = t2.run(request, chat_fn=lambda r: {"raw": "FINAL ANSWER: no", "usage": {"total_tokens": 3}})
    assert out["status"] == "completed" and out["tokens"]["total_tokens"] == 3


def test_t1_drops_the_oldest_turns_near_the_context_cap(monkeypatch) -> None:
    pytest.importorskip("minisweagent")  # only in .venvs/miniswe
    from minisweagent.models.litellm_model import LitellmModel

    model = t1._model_class()(model_name="openai/m", cost_tracking="ignore_errors")
    model.cap, model.per_char = 100_000, 1.0
    turn = [{"role": "assistant", "content": "a" * 10_000}, {"role": "tool", "content": "t" * 10_000}]
    history = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}, *turn * 3]
    assert model._prepare_messages_for_api(history) == history and model.cuts == 0  # 60k: under 100k - 32k
    sent = []
    monkeypatch.setattr(LitellmModel, "query", lambda self, messages, **kw: sent.append(
        self._prepare_messages_for_api(messages)) or {"extra": {"response": {"usage": {"prompt_tokens": 1}}}})
    model.query(history + turn * 2)  # 100k: cut to about half the cap, at a turn start
    kept = sent[0]
    assert model.cuts == 1 and kept[2]["role"] == "assistant" and len(kept) == 6 and "省略" in kept[1]["content"]
    assert kept[0] == history[0] and kept[-4:] == (turn * 2)[-4:]
    assert model.per_char == 1 / model.sent_chars  # calibrated from the reply
    model.per_char = 1.0
    assert model._prepare_messages_for_api(history + turn * 2) == kept and model.cuts == 1  # same cut until full again


def _project(request: dict) -> Path:
    project = Path(request["work_dir"])
    (project / "py" / "prep").mkdir(parents=True, exist_ok=True)
    (project / "py" / "prep" / "main.py").write_text('"""entry"""\n', encoding="utf-8")
    (project / "py" / "prep" / "_8_loadcase.py").write_text('"""loads"""\nLOAD = 1\n', encoding="utf-8")
    return project


def test_t3_pipeline_repairs_until_the_runner_passes(request_for) -> None:
    request = {**request_for("T3", files=True), "task": "change", "instructions": "改荷载为 2",
               "mcp_servers": {"osis": {"url": "u"}}}
    project = _project(request)
    replies = {"interpreter": '{"intent": "改荷载", "parameters": {"LOAD": 2}, "decline": false}',
               "architect": '{"skill_files": [], "edits": [{"path": "py/prep/_8_loadcase.py", "change": "LOAD=2"}]}',
               "writer": '```python\n"""loads"""\nLOAD = 2\n```',
               "reviewer": '{"root_cause": "x", "edits": [{"path": "py/prep/_8_loadcase.py", "change": "again"}]}',
               "reporter": '{"final": "改好了"}'}
    roles = {v: k for k, v in t3.ROLES.items()}
    runs = []

    def mcp(server, tool, args):
        runs.append(Path(args.get("file", "")).name)
        ok = len(runs) != 1  # the first rebuild fails once
        return json.dumps({"ok": ok}), not ok

    out = t3.run(request, llm=lambda system, user: (replies[roles[system]], {"total_tokens": 1}), mcp=mcp)
    assert out["status"] == "completed" and out["final_answer"] == "改好了" and out["repair_rounds"] == 1
    assert "LOAD = 2" in (project / "py" / "prep" / "_8_loadcase.py").read_text(encoding="utf-8")
    assert runs == ["main.py", "main.py", "_t3_solve.py"]

    replies["interpreter"] = '{"decline": true, "concerns": ["空心板 40m 不合理"]}'
    replies["reporter"] = '{"final": "status: declined 跨径不合理"}'
    out = t3.run(request, llm=lambda system, user: (replies[roles[system]], {}), mcp=mcp)
    assert out["declined"] and out["final_answer"].startswith("status: declined")

    replies["reporter"] = '{"value": 3.5}'  # the answer without the "final" wrapper still counts
    out = t3.run(request, llm=lambda system, user: (replies[roles[system]], {}), mcp=mcp)
    assert out["status"] == "completed" and json.loads(out["final_answer"]) == {"value": 3.5}


def test_t3_starts_an_empty_project_from_a_template(request_for, tmp_path) -> None:
    skills = tmp_path / "skills"
    tpl = skills / "osis-bridge-hollow-slab" / "references" / "templates" / "空心板-13m"
    (tpl / "prep").mkdir(parents=True)
    (tpl / "prep" / "main.py").write_text('"""entry"""\n', encoding="utf-8")
    (tpl / "prep" / "_8_loadcase.py").write_text('"""loads"""\nLOAD = 1\n', encoding="utf-8")
    (tpl / "prep" / "_8_loadcase.py").chmod(0o444)  # snapshots are read-only
    request = {**request_for("T3"), "task": "build", "instructions": "建 16m 空心板", "skills_dir": str(skills),
               "work_dir": str(tmp_path / "project"), "mcp_servers": {"osis": {"url": "u"}}}
    (tmp_path / "project" / "py").mkdir(parents=True)
    rel = "osis-bridge-hollow-slab/references/templates/空心板-13m"
    prompts = []
    replies = {"interpreter": '{"intent": "建模", "decline": false}',
               "architect": json.dumps({"skill_files": [], "template": rel,
                                        "edits": [{"path": "py/prep/_8_loadcase.py", "change": "LOAD=2"}]}),
               "writer": '```python\n"""loads"""\nLOAD = 2\n```', "reporter": '{"final": "建好了"}'}
    roles = {v: k for k, v in t3.ROLES.items()}
    out = t3.run(request, llm=lambda system, user: prompts.append(user) or (replies[roles[system]], {}),
                 mcp=lambda server, tool, args: (json.dumps({"ok": True}), False))
    py = tmp_path / "project" / "py" / "prep"
    assert out["status"] == "completed" and (py / "main.py").is_file()
    assert "LOAD = 2" in (py / "_8_loadcase.py").read_text(encoding="utf-8")
    assert rel in prompts[1]  # the architect is shown the template list
    pipe = t3.Pipeline({**request, "work_dir": str(tmp_path / "other")})
    assert not pipe.copy_template("../../etc")  # only a listed template


def test_t4_gates(request_for) -> None:
    request = {**request_for("T4", files=True), "task": "diagnose",
               "instructions": '本项目「正截面抗弯验算」不通过。定位结论请输出 JSON：{"localizations":[{"reason_code":"短码"}]}'}
    project = _project(request)
    (project / "py" / "reason_codes.yaml").write_text("codes:\n  rebar_num: {label: x}\n", encoding="utf-8")
    good = '{"localizations":[{"reason_code":"rebar_num"}]}'
    ok_state = {"solved": True, "checks": {"混凝土_正截面抗弯验算_基本组合包络": ["OK"]}}
    assert t4.gates(request, "status: done\n" + good, lambda r: ok_state) == []
    ng_state = {"solved": False, "checks": {"混凝土_正截面抗弯验算_基本组合包络": ["NG"]}}
    found = t4.gates(request, '{"localizations":[{"reason_code":"bogus"}]}', lambda r: ng_state)
    assert [v[:2] for v in found] == ["G1", "G4", "G3"]
    assert t4.gates(request, "status: declined\n" + good, lambda r: ng_state) == []  # declined skips G1-G3
    (project / "OSIS.out").write_text("", encoding="utf-8")
    os.utime(project / "OSIS.out", (1, 1))
    assert any(v.startswith("G2") for v in t4.gates(request, good, lambda r: ok_state))
    analyze = {**request, "task": "analyze", "instructions": '回复 JSON：{"value": 数值, "element": 单元号}'}
    assert t4.format_gate(analyze, '{"value": 1, "element": 2}') == [] and t4.format_gate(analyze, "100 kN")
    out = t4.run(request, runtime=lambda r, p: {"final_answer": "x", "deferred": True, "gate_rounds": [["G1"]] * 6})
    assert out["status"] == "completed" and out["stop_reason"] == "deferred"


def _fake_turn_state(monkeypatch) -> list[str]:
    """``end_turn`` reads OSIS through the Broker; here it records which project it was asked about."""
    from bench import osis

    seen: list[str] = []
    monkeypatch.setattr(osis, "inspect_project", lambda py, broker, model: seen.append(broker) or {"solved": True})
    return seen


def test_t3_runs_one_pipeline_per_project_turn(request_for, monkeypatch) -> None:
    seen = _fake_turn_state(monkeypatch)
    request = {**request_for("T3", files=True), "task": "project", "instructions": "第一轮：改荷载为 2",
               "turns": ["第二轮：改荷载为 3"], "broker_url": "b", "mcp_servers": {"osis": {"url": "u"}}}
    project = _project(request)
    prompts: list[str] = []
    replies = {"interpreter": '{"intent": "改荷载", "decline": false}',
               "architect": '{"skill_files": [], "edits": [{"path": "py/prep/_8_loadcase.py", "change": "x"}]}',
               "writer": '```python\n"""loads"""\nLOAD = 2\n```', "reporter": '{"final": "改好了"}'}
    roles = {v: k for k, v in t3.ROLES.items()}

    def llm(system, user):
        prompts.append(user)
        return replies[roles[system]], {"total_tokens": 1}

    out = t3.run(request, llm=llm, mcp=lambda server, tool, args: (json.dumps({"ok": True}), False))
    run_dir = Path(request["run_dir"])
    assert out["status"] == "completed" and seen == ["b", "b"]
    turns = [json.loads((run_dir / f"turn{k}" / "turn.json").read_text(encoding="utf-8")) for k in (1, 2)]
    assert [t["answer"] for t in turns] == ["改好了", "改好了"] and turns[0]["ended_at"] <= turns[1]["ended_at"]
    assert (run_dir / "turn2" / "project" / "py" / "prep" / "_8_loadcase.py").is_file()
    second = [p for p in prompts if p.startswith("第二轮") or "第二轮" in p[:200]]
    assert second and "前几轮（同一工程）的最终回复" in second[0] and "第 1 轮：\n改好了" in second[0]
    assert not (project / "py" / "_bench_inspect.py").exists()


def test_t4_gates_and_records_every_project_turn(request_for, monkeypatch) -> None:
    seen = _fake_turn_state(monkeypatch)
    request = {**request_for("T4", files=True), "task": "project", "instructions": "第一轮", "broker_url": "b",
               "turns": ["第二轮：「正截面抗弯验算」要通过"]}
    _project(request)
    sent: list[str] = []

    class Agent:
        messages, n_calls = [], 1
        model = types.SimpleNamespace(cuts=0)
        config = types.SimpleNamespace(output_path=Path(request["run_dir"]) / "trajectory.json")

        def run(self, prompt):
            sent.append(prompt)
            return {"exit_status": "Submitted", "submission": "status: done\n一"}

        def resume(self, text):
            sent.append(text)
            return {"exit_status": "Submitted", "submission": f"status: done\n{len(sent)}"}

    monkeypatch.setattr(t1, "make_agent", lambda r: Agent())
    states = iter([{"solved": True, "checks": {}},  # turn 1 passes
                   {"solved": True, "checks": {"混凝土_正截面抗弯验算_基本组合包络": ["NG"]}},  # turn 2 reopened once
                   {"solved": True, "checks": {"混凝土_正截面抗弯验算_基本组合包络": ["OK"]}}])
    out = t4._runtime(request, "第一轮", probe_fn=lambda r: next(states))
    assert [g["reopened"] for g in out["turn_gates"]] == [0, 1] and seen == ["b", "b"]
    assert sent[1].startswith("第二轮") and sent[2].startswith("外部检查没有通过")
    answers = [json.loads((Path(request["run_dir"]) / f"turn{k}" / "turn.json").read_text(encoding="utf-8"))["answer"]
               for k in (1, 2)]
    assert answers == ["status: done\n一", "status: done\n3"]


def test_null_temperature_is_not_sent(request_for, tmp_path) -> None:
    request = {**request_for("T2"), "model": {**request_for("T2")["model"], "temperature": None}}
    from methods._common import model_settings
    assert model_settings(request)["temperature"] is None
    install = tmp_path / "install"
    (install / ".agents").mkdir(parents=True)
    (install / ".agents" / "opencode.json").write_text("{}", encoding="utf-8")
    config = json.loads((osisai.build_pack(request, install, tmp_path / "bare", bare=True) / "opencode.json").read_text(encoding="utf-8"))
    assert "temperature" not in config["provider"][osisai.PROVIDER]["models"][request["model"]["name"]]["options"]
