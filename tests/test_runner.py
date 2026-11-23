"""End-to-end campaigns on the fake task line, with method processes replaced."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench import cli, runner, status
from bench.dispatch import DispatchError

ANSWERS = {"q-001": "no", "q-002": "5"}


def _fake_dispatch(calls: list, fail: set | None = None):
    def fake(spec, request, parent, mcp=None):
        calls.append((spec["id"], request["item_id"]))
        assert '"answer": ' not in json.dumps(request, ensure_ascii=False), "gold leaked into request"
        if fail and (spec["id"], request["item_id"]) in fail:
            raise DispatchError("boom")
        return {"status": "completed", "final_answer": ANSWERS.get(request["item_id"], "x"), "elapsed_s": 24.0,
                "tokens": {"total_tokens": 1000}}
    return fake


def test_campaign_resume_and_retry(config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(runner, "dispatch", _fake_dispatch(calls, fail={("T2", "q-002")}))
    result = runner.run_campaign(config, "fake", "c1", method_ids=["T1", "T2"], jobs=2)
    assert result == {"label": "c1", "task": "fake", "cells": 4, "done": 3, "skipped": 0, "error": 1, "abandoned": 0}

    campaign_dir = Path(config["runs_root"]) / "c1"
    report = status.campaign_status(campaign_dir)
    assert report["counts"]["done"] == 3 and report["errors"][0]["item"] == "q-002"
    assert report["finished"] and not report["stalled"]
    assert not any("templates" in str(p) for p in (campaign_dir / "skills").rglob("*")), "hidden() must apply"

    calls.clear()
    monkeypatch.setattr(runner, "dispatch", _fake_dispatch(calls))
    again = runner.run_campaign(config, "fake", "c1", method_ids=["T1", "T2"])
    assert calls == [("T2", "q-002")] and again["skipped"] == 3 and again["done"] == 1
    assert any((campaign_dir / "_archive").rglob("error.log"))

    summary = runner.report(config, "c1")
    assert summary["methods"]["T1"]["accuracy"] == 0.5 and summary["methods"]["T1"]["n"] == 2
    assert summary["methods"]["T1"]["mean_latency_s"] == 24.0 and not summary["incomplete"]


def test_compare_puts_labels_side_by_side(config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "dispatch", _fake_dispatch([]))
    runner.run_campaign(config, "fake", "a", method_ids=["T1", "T2"])
    runner.run_campaign(config, "fake", "b", method_ids=["T2"])
    with pytest.raises(ValueError, match="more than one"):
        runner.compare(config, ["a", "b"])
    both = runner.compare(config, ["a:T1", "b"])
    assert list(both["methods"]) == ["T1", "T2"] and both["methods"]["T2"]["label"] == "b"
    table = runner.markdown(both).splitlines()
    assert table[0] == "| 指标 | T1 | T2 |" and any(row.startswith("| accuracy | 0.5 | 0.5 |") for row in table)


def test_failed_generation_keeps_its_time(config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "dispatch", lambda spec, request, parent, mcp=None: {
        "status": "failed", "stop_reason": "timeout", "error": "TimeoutError", "elapsed_s": 1800.0})
    runner.run_campaign(config, "fake", "c2", method_ids=["T1"], item_ids=["q-001"])
    record = status.read_json(Path(config["runs_root"]) / "c2" / "T1" / "q-001" / "seed0" / "record.json")
    assert record["correct"] is False and record["latency_s"] == 1800.0 and record["error"]


def test_prepare_failure_is_retryable_infrastructure(config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    config["tasks"]["fake"]["items"][0]["broken"] = True
    monkeypatch.setattr(runner, "dispatch", _fake_dispatch([]))
    result = runner.run_campaign(config, "fake", "c3", method_ids=["T1"], item_ids=["q-001"])
    assert result["error"] == 1
    state = status.read_json(Path(config["runs_root"]) / "c3" / "T1" / "q-001" / "seed0" / "status.json")
    assert "broken" in state["error"] and state["stage"] == "prepare"


def test_a_cell_failing_three_times_is_abandoned(config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(runner, "dispatch", _fake_dispatch(calls, fail={("T1", "q-001")}))
    outcomes = [runner.run_campaign(config, "fake", "c4", method_ids=["T1"], item_ids=["q-001"]) for _ in range(4)]
    assert [o["error"] for o in outcomes] == [1, 1, 0, 0] and [o["abandoned"] for o in outcomes] == [0, 0, 1, 1]
    assert len(calls) == 3  # not run again once abandoned
    state = status.campaign_status(Path(config["runs_root"]) / "c4")
    assert state["counts"]["abandoned"] == 1 and [f["stage"] for f in state["abandoned"][0]["failures"]] == ["generate"] * 3
    assert runner.report(config, "c4")["abandoned"][0]["item"] == "q-001"


def test_cli_reports_errors_as_json(config: dict, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    import yaml

    config["model"]["name"] = ""
    path = tmp_path / "platform.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    assert cli.main(["--config", str(path), "run", "--task", "fake", "--label", "x"]) == 2
    assert "model.name is empty" in json.loads(capsys.readouterr().out)["error"]
    assert cli.main(["--config", str(path), "run", "--task", "fake", "--label", "x", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["cells"] == 8  # T1 T2 T3 T5 x 2 items (T4 skips this line, legacy only by name)


def test_reset_dirs_empties_shared_state_before_every_run(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    (memory / "projects").mkdir(parents=True)
    (memory / "PROFILE.md").write_text("learned", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    runner.reset_dirs({"reset_dirs": [str(memory)]}, run_dir)
    log = status.read_json(run_dir / "reset_dirs.json")["dirs"]
    assert not memory.exists() and log == [{"path": str(memory), "entries_before": 2, "exists_after": False}]
    runner.reset_dirs({}, run_dir / "none")  # methods without shared state leave no log
    assert not (run_dir / "none").exists()


def test_a_gateway_failure_is_retried_and_never_abandons(config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from bench.recorder import network_failure

    assert network_failure({"last_status": 429}) and network_failure({"last_status": 0, "last_error": "ConnectError: x"})
    assert not network_failure({"last_status": 200, "last_error": "client disconnected: reset"})
    assert network_failure({"last_status": 200, "last_error": "client disconnected: reset", "last_ttfb_s": 320.9})
    assert network_failure({"calls": 0, "waiting_s": 301.0}) and not network_failure({"calls": 3, "waiting_s": 12.0})
    assert not network_failure({"last_status": 400}) and not network_failure({})
    assert not network_failure({"last_status": 400, "last_error": "prompt is too long: 1872370 tokens > 1048576"})
    assert network_failure({"last_status": 402, "last_error": "Insufficient Balance"})
    monkeypatch.setattr(runner, "dispatch", lambda spec, request, parent, mcp=None: {
        "status": "failed", "error": "no final answer", "elapsed_s": 5.0, "gateway": {"last_status": 429}})
    outcomes = [runner.run_campaign(config, "fake", "c5", method_ids=["T1"], item_ids=["q-001"]) for _ in range(4)]
    assert [o["error"] for o in outcomes] == [1, 1, 1, 1]  # not scored, not abandoned
    many = runner.run_campaign(config, "fake", "c7", method_ids=["T1"], seeds=[0, 1, 2, 3], jobs=1)
    assert many["error"] == runner.GATEWAY_DOWN and many["not_run"] == many["cells"] - 3 and many["gateway_down"]

    def mcp_dies(spec, request, parent, mcp=None):
        raise RuntimeError("WeKnora MCP server exited 3221225794")  # machine out of memory

    monkeypatch.setattr(runner, "dispatch", mcp_dies)
    starved = runner.run_campaign(config, "fake", "c8", method_ids=["T1"], seeds=[0, 1, 2, 3], jobs=1)
    assert starved["error"] == runner.GATEWAY_DOWN and starved["gateway_down"]
    state = status.read_json(Path(config["runs_root"]) / "c5" / "T1" / "q-001" / "seed0" / "status.json")
    assert all(f["network"] for f in state["failures"]) and "gateway returned 429" in state["error"]
    monkeypatch.setattr(runner, "dispatch", _fake_dispatch([]))

    def held(ctx, run_dir):
        raise PermissionError("WinError 32: weknora_server.log is used by another process")

    with monkeypatch.context() as m:  # a prior run still held open: that cell waits, the campaign goes on
        m.setattr(runner, "_archive", held)
        assert runner.run_campaign(config, "fake", "c5", method_ids=["T1"], item_ids=["q-001", "q-002"])["error"] == 1
    assert runner.run_campaign(config, "fake", "c5", method_ids=["T1"], item_ids=["q-001"])["done"] == 1


def test_prune_keeps_code_records_and_logs(tmp_path: Path) -> None:
    run = tmp_path / "run"
    for rel in ("project/py/prep/main.py", "project/Result/big.bin", "project/OSIS.out", "project.sis", "record.json",
                "osisai/agents/node_modules/x.js", "osisai/data/opencode/session.db", "model_calls.jsonl"):
        (run / rel).parent.mkdir(parents=True, exist_ok=True)
        (run / rel).write_text("x", encoding="utf-8")
    runner.prune(run)
    left = sorted(p.relative_to(run).as_posix() for p in run.rglob("*") if p.is_file())
    assert left == ["model_calls.jsonl", "osisai/data/opencode/session.db", "project/py/prep/main.py", "record.json"]


def test_a_label_keeps_its_model(config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "dispatch", lambda spec, request, parent, mcp=None: {
        "status": "failed", "error": "x", "elapsed_s": 1.0, "gateway": {"last_status": 429}})
    runner.run_campaign(config, "fake", "c6", method_ids=["T1"], item_ids=["q-001"])
    other = {**config, "model": {**config["model"], "name": "another-model"}}
    with pytest.raises(ValueError, match="use a new label"):
        runner.run_campaign(other, "fake", "c6", method_ids=["T1"], item_ids=["q-001"])
    alias = {**config, "model": {**config["model"], "name": "remote-name", "same_model": config["model"]["name"]}}
    runner.run_campaign(alias, "fake", "c6", method_ids=["T1"], item_ids=["q-001"])  # same model, other gateway



def test_a_big_bridge_waits_for_its_slot_without_holding_a_worker(config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    slots = threading.BoundedSemaphore(1)
    slots.acquire()  # another run is solving a big bridge
    monkeypatch.setattr(runner.osis, "heavy_slots", lambda cfg, bridge: slots if bridge == "big" else None)
    task_cfg = config["tasks"]["fake"]
    task_cfg["items"] = [{**task_cfg["items"][0], "bridge": "big"}, *task_cfg["items"][1:]]
    calls: list = []
    fake = _fake_dispatch(calls)

    def dispatch(spec, request, parent, mcp=None):
        if request["item_id"] == "q-002":  # the light cell ran while q-001 waited: now the slot frees
            slots.release()
        return fake(spec, request, parent, mcp)

    monkeypatch.setattr(runner, "dispatch", dispatch)
    result = runner.run_campaign(config, "fake", "c9", method_ids=["T1"], jobs=1)  # one worker: no deadlock
    assert result["done"] == 2 and [c[1] for c in calls] == ["q-002", "q-001"]
