"""bench.osis helpers that need no Broker: machine-trouble detection and the heavy-model solve slots."""

import threading
import time

from bench import osis


def test_transient_errors_are_machine_trouble_only():
    assert osis.transient('solve: RuntimeError: 求解失败: HTTP 500: {"error":"bad allocation"}')
    assert osis.transient("solve: : 执行超时 (3600s)")
    assert not osis.transient("rebuild: TypeError: Section.set_stress_point() missing 1 required positional argument")
    assert not osis.transient(None)


def test_heavy_bridges_share_limited_slots(monkeypatch, tmp_path):
    monkeypatch.setattr(osis, "_HEAVY", None)
    cfg = {"heavy": {"bridges": ["big"], "jobs": 2, "lock_dir": str(tmp_path)}}
    active, peak, lock = [], [0], threading.Lock()

    def solve(bridge):
        with osis.slot(cfg, bridge):
            with lock:
                active.append(bridge)
                peak[0] = max(peak[0], active.count("big"))
            time.sleep(0.05)
            with lock:
                active.remove(bridge)

    threads = [threading.Thread(target=solve, args=(b,)) for b in ["big"] * 6 + ["small"] * 3]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 2


def test_check_reads_ng_items(monkeypatch, tmp_path):
    def run(script, broker, timeout_s):  # what _CHECK writes after post/main.py
        script.with_name("_bench_check.json").write_text(
            '{"混凝土_PC正截面长期抗裂验算_准永久组合包络": ["NG", "OK"], "混凝土_PC正截面长期抗裂验算_准永久组合包络_直接作用": ["NG"],'
            ' "施工阶段荷载包络_PC施工阶段正截面压应力验算_MinMax": ["OK"], "混凝土_裂缝宽度验算_频遇组合包络": []}', encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr(osis, "run_script", run)
    assert osis.check(tmp_path, "broker")[:2] == (None, ["PC正截面长期抗裂验算"])


def test_heavy_slots_are_shared_across_processes(tmp_path):
    import subprocess
    import sys

    slots = osis.MachineSlots(1, tmp_path)
    assert slots.acquire(blocking=False)
    other = "from pathlib import Path; from bench import osis; print(osis.MachineSlots(1, Path(r'%s')).acquire(False))"
    assert subprocess.run([sys.executable, "-c", other % tmp_path], capture_output=True, text=True).stdout.strip() == "False"
    slots.release()
    assert subprocess.run([sys.executable, "-c", other % tmp_path], capture_output=True, text=True).stdout.strip() == "True"


def test_runner_yields_heavy_slot_to_a_blocking_waiter(tmp_path):
    import threading

    runner, tool = osis.MachineSlots(1, tmp_path), osis.MachineSlots(1, tmp_path)
    assert runner.acquire(blocking=False)
    got, done = threading.Event(), threading.Event()

    def wait_then_hold():  # acquire and release on one thread, like the tools
        tool.acquire()
        got.set()
        done.wait(5)
        tool.release()

    waiter = threading.Thread(target=wait_then_hold)
    waiter.start()
    time.sleep(0.5)  # the tool is waiting and has left its mark
    runner.release()
    assert not runner.acquire(blocking=False)  # the runner takes the freed slot no longer
    assert got.wait(5) and not (tmp_path / "osis-bench-heavy-0.wait").exists()
    done.set()
    waiter.join(5)
    assert not runner.acquire(blocking=False)  # the tool may want it back for its next solve
    (tmp_path / "osis-bench-heavy-0.wait").unlink()
    assert runner.acquire(blocking=False)
    runner.release()
