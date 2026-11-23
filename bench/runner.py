"""Campaign orchestration: methods x items x seeds, resumable, one state file per run."""

from __future__ import annotations

import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import methods
import tasks
from bench import osis, status
from bench.config import ROOT, resolve
from bench.dispatch import dispatch
from bench.parent import Parent
from bench.recorder import network_failure
from bench.skills import SkillAdapter, create_snapshot


@dataclass
class Context:
    config: dict[str, Any]
    task_name: str
    parent: Parent
    campaign_dir: Path

    @property
    def task_cfg(self) -> dict[str, Any]:
        return self.config["tasks"][self.task_name]


def make_context(config: dict[str, Any], task_name: str, label: str) -> Context:
    tasks.get(task_name)
    if task_name not in config.get("tasks", {}):
        raise KeyError(f"configs/platform.yaml has no tasks.{task_name} section")
    parent = Parent(resolve(config["parent_repo"]), config.get("parent_skills") and resolve(config["parent_skills"]))
    return Context(config, task_name, parent, resolve(config["runs_root"]) / label)


def select_methods(task_name: str, wanted: list[str] | None) -> list[dict[str, Any]]:
    registry = methods.discover()
    ids = wanted or [m for m, spec in registry.items() if methods.supports(spec, task_name) and not spec["legacy"]]
    unknown = [m for m in ids if m not in registry]
    if unknown:
        raise KeyError(f"unknown methods {unknown}; known: {sorted(registry)}")
    unsupported = [m for m in ids if not methods.supports(registry[m], task_name)]
    if unsupported:
        raise ValueError(f"methods {unsupported} do not support task {task_name}")
    return [registry[m] for m in ids]


def _check_model(config: dict[str, Any], specs: list[dict[str, Any]]) -> None:
    model = config["model"]
    if not str(model.get("name") or "").strip():
        raise ValueError("model.name is empty in configs/platform.yaml")
    gateway = [s["id"] for s in specs if s["python"] != "parent"]
    if gateway and not os.environ.get(model["api_key_env"], "").strip():
        raise ValueError(f"{gateway} need the API key in env var {model['api_key_env']}")


def _snapshot(ctx: Context, task: ModuleType) -> tuple[Path, str]:
    skills = ctx.campaign_dir / "skills"
    if not skills.exists():
        create_snapshot(ctx.parent.skills, skills, [*(ctx.task_cfg.get("skills_exclude") or []), *task.hidden(ctx)])
    return skills, SkillAdapter(skills).bundle_hash()


def prune(run_dir: Path) -> None:
    """After scoring: drop what is big and never read again (OSIS model and results ~100 MB, OpenCode's
    node_modules); the code (``project/py``), the record and every log stay, so new cases never force reruns."""
    project = run_dir / "project"
    big = [*run_dir.glob("project.sis*"), *run_dir.glob("osisai/*/node_modules")]
    if project.is_dir():
        big += [p for p in project.iterdir() if p.name != "py"]
    for path in big:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # still open (an OSIS solver holding project.sis): leave it, the cell is scored
                pass


def _archive(ctx: Context, run_dir: Path) -> None:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = ctx.campaign_dir / "_archive" / stamp / run_dir.relative_to(ctx.campaign_dir)
    n = 1
    while target.exists():  # two retries of one cell within a second
        n += 1
        target = ctx.campaign_dir / "_archive" / f"{stamp}-{n}" / run_dir.relative_to(ctx.campaign_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    run_dir.rename(target)  # same drive: all or nothing, unlike shutil.move's copy-then-delete fallback
    prune(target)  # a failed attempt keeps its logs, not its OSIS results


def same_model(model: dict[str, Any]) -> str:
    """The model behind a gateway name: one model under another gateway's name (``same_model``) may share a label."""
    return str(model.get("same_model") or model.get("name"))


GATEWAY_DOWN = 3  # this many cells in a row failing on the platform (gateway, MCP servers, machine) stop the campaign


class NetworkFailure(RuntimeError):
    """The method failed because the LLM gateway did (rate limit, server error, connection)."""


def reset_dirs(spec: dict[str, Any], run_dir: Path) -> None:
    """Every run starts from clean method state (e.g. OSIS-AI long-term memory); what was there is logged as evidence."""
    log = []
    for path in spec.get("reset_dirs") or []:
        target = Path(os.path.expanduser(path))
        entries = sum(1 for _ in target.rglob("*")) if target.exists() else 0
        if target.exists():
            shutil.rmtree(target)
        log.append({"path": str(target), "entries_before": entries, "exists_after": target.exists()})
    if log:
        status.write_json(run_dir / "reset_dirs.json", {"at": status.now(), "dirs": log})


def run_campaign(config: dict[str, Any], task_name: str, label: str, *, method_ids: list[str] | None = None,
                 item_ids: list[str] | None = None, seeds: list[int] | None = None, jobs: int = 1,
                 dry_run: bool = False) -> dict[str, Any]:
    ctx = make_context(config, task_name, label)
    task = tasks.get(task_name)
    specs = select_methods(task_name, method_ids)
    items = {task.item_id(item): item for item in task.load_items(ctx)}
    if item_ids:
        missing = [i for i in item_ids if i not in items]
        if missing:
            raise KeyError(f"unknown items {missing}")
        items = {i: items[i] for i in item_ids}
    seeds = seeds or [0]
    cells = [(spec["id"], iid, seed) for spec in specs for iid in items for seed in seeds]
    if dry_run:
        return {"label": label, "task": task_name, "methods": [s["id"] for s in specs],
                "items": len(items), "seeds": seeds, "cells": len(cells)}

    _check_model(config, specs)
    campaign_path = ctx.campaign_dir / "campaign.json"
    previous = status.read_json(campaign_path)
    if previous and previous.get("task") != task_name:
        raise ValueError(f"label {label} already holds task {previous.get('task')}")
    if previous and same_model(previous.get("model") or {}) != same_model(config["model"]):
        raise ValueError(f"label {label} ran model {previous['model'].get('name')}; use a new label for "
                         f"{config['model']['name']}")
    if previous and not previous.get("finished_at") and status.pid_alive(int(previous.get("pid") or 0)):
        raise RuntimeError(f"campaign {label} is already running (pid {previous['pid']})")
    skills_dir, skills_hash = _snapshot(ctx, task)
    commit = ctx.parent.commit()
    mcp = {name: config["mcp"][name] for name in ctx.task_cfg.get("mcp") or []}
    known = [tuple(c) for c in previous.get("cells", [])]
    seen = set(known)
    all_cells = known + [c for c in cells if c not in seen]
    status.write_json(campaign_path, {
        "label": label, "task": task_name, "cells": [list(c) for c in all_cells],
        "pid": os.getpid(), "started_at": status.now(), "finished_at": None,
        "parent_commits": sorted(set(previous.get("parent_commits", [])) | {commit}),
        "skills_sha256": skills_hash, "model": config["model"], "osisai": config["osisai"], "mcp": mcp,
        "task_config": ctx.task_cfg, "environment": _environment(ctx),
    })

    by_id = {spec["id"]: spec for spec in specs}
    serial_lock = threading.Lock()
    method_slots: dict[str, Any] = {}
    gateway = {"failed_in_a_row": 0}  # cells that just failed on the platform, across threads

    def heavy(cell: tuple[str, str, int]) -> Any:
        return osis.heavy_slots(config.get("osis") or {}, str(items[cell[1]].get("bridge") or ""))

    def serial(cell: tuple[str, str, int]) -> Any:
        # serial methods share state across processes too (T5: ~/.osisai/memory): one run at a time per machine
        if not by_id[cell[0]]["serial"]:
            return None
        return method_slots.setdefault(cell[0], osis.MachineSlots(1, Path(tempfile.gettempdir()), f"method-{cell[0]}"))

    def one(cell: tuple[str, str, int]) -> str:
        """``attempt`` holding, for the whole run, a big bridge's slot (agents solve big models too) and a serial
        method's; "deferred" without waiting when one is taken. All or nothing, so two campaigns never hold one
        each and wait for the other."""
        locks = [lock for lock in (heavy(cell), serial(cell)) if lock is not None]
        if not locks or status.read_json(by_cell_dir(cell) / "status.json").get("state") in ("done", "abandoned"):
            return attempt(cell)
        held = []
        for lock in locks:
            if not lock.acquire(blocking=False):
                break
            held.append(lock)
        if len(held) < len(locks):
            for lock in held:
                lock.release()
            return "deferred"
        try:
            return attempt(cell)
        finally:
            for lock in held:
                lock.release()

    def by_cell_dir(cell: tuple[str, str, int]) -> Path:
        return ctx.campaign_dir / cell[0] / cell[1] / f"seed{cell[2]}"

    def attempt(cell: tuple[str, str, int]) -> str:
        method, iid, seed = cell
        spec, item = by_id[method], items[iid]
        run_dir = ctx.campaign_dir / method / iid / f"seed{seed}"
        prior = status.read_json(run_dir / "status.json")
        if prior.get("state") in ("done", "abandoned"):
            return "skipped" if prior["state"] == "done" else "abandoned"
        if gateway["failed_in_a_row"] >= GATEWAY_DOWN:  # gateway out of quota, machine out of memory: leave the rest for a resume
            return "not_run"
        failures = list(prior.get("failures") or [])  # platform failures so far, carried across retries
        if run_dir.exists():
            try:
                _archive(ctx, run_dir)
            except OSError:  # a file still held (e.g. a server orphaned by a killed run): the cell waits for a resume
                return "error"
        run_dir.mkdir(parents=True)
        with serial_lock if task.SERIAL else nullcontext():
            stage = "prepare"
            try:
                status.set_state(run_dir, "preparing", method=method, item=iid, seed=seed, parent_commit=commit,
                                 failures=failures)
                prepared = task.prepare(ctx, item, run_dir)
                request = {
                    "method": method, "task": task_name, "item_id": iid, "seed": seed,
                    "run_dir": str(run_dir), "skills_dir": str(skills_dir),
                    "tools": list(ctx.task_cfg.get("tools", [])), "timeout_s": float(ctx.task_cfg["timeout_s"]),
                    "model": config["model"], "osisai": config["osisai"],
                    "parent_repo": str(ctx.parent.root), **prepared,
                }
                notes = [cfg["instructions"] for cfg in mcp.values() if cfg.get("instructions")]
                if notes and request.get("instructions"):
                    request["instructions"] = "\n\n".join([request["instructions"], *notes])
                stage = "generate"
                reset_dirs(spec, run_dir)  # inside the serial lock: methods with shared state are serial
                status.set_state(run_dir, "generating")
                generation = dispatch(spec, request, ctx.parent, mcp)
                status.write_json(run_dir / "generation.json", generation)
                if generation.get("status") != "completed" and (why := network_failure(generation.get("gateway") or {})):
                    raise NetworkFailure(why)  # not the method's failure: retried on resume, not scored
                stage = "score"
                status.set_state(run_dir, "scoring", generation_status=generation.get("status"))
                record = task.score(ctx, item, run_dir, generation)
                record.update(method=method, seed=seed, generation_status=generation.get("status"))
                status.write_json(run_dir / "record.json", record)
                status.set_state(run_dir, "done", scored_error=record.get("error"))
                prune(run_dir)
                gateway["failed_in_a_row"] = 0
                return "done"
            except Exception as exc:  # noqa: BLE001 - infrastructure failure: keep it retryable
                (run_dir / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
                error = f"{type(exc).__name__}: {exc}"[:800]
                failures.append({"at": status.now(), "stage": stage, "error": error,
                                 "network": isinstance(exc, NetworkFailure)})
                # a gateway that is down or out of quota says nothing about the cell: it never gets it abandoned
                counted = [f for f in failures if not f.get("network")]
                state = "abandoned" if len(counted) >= status.MAX_FAILURES else "error"
                status.set_state(run_dir, state, stage=stage, error=error, failures=failures)
                gateway["failed_in_a_row"] += 1  # a method failing on its own is scored, never lands here
                return state

    # order mixed (fixed seed): cells of one bridge type sit together. A worker that draws a cell whose slots are
    # taken puts it back and takes another, so one waiting cell never blocks a runnable one behind it
    queue = deque(random.Random(0).sample(cells, len(cells)))
    outcomes: list[str] = []
    queue_lock = threading.Lock()

    def worker() -> None:
        while True:
            with queue_lock:
                if not queue:
                    return
                cell = queue.popleft()
                locked_only = all(heavy(c) is not None or serial(c) is not None for c in queue)
            outcome = one(cell)
            with queue_lock:
                if outcome == "deferred":
                    queue.append(cell)
                else:
                    outcomes.append(outcome)
            if outcome == "deferred":
                time.sleep(2 if locked_only else 0.2)

    try:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            for future in [pool.submit(worker) for _ in range(max(1, jobs))]:
                future.result()
    finally:
        final = status.read_json(campaign_path)
        final["finished_at"] = status.now()
        status.write_json(campaign_path, final)
    result = {"label": label, "task": task_name, "cells": len(cells),
              **{k: outcomes.count(k) for k in ("done", "skipped", "error", "abandoned")}}
    if outcomes.count("not_run"):
        result.update(not_run=outcomes.count("not_run"), gateway_down=True)
    return result


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=root, text=True, encoding="utf-8",
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _environment(ctx: Context) -> dict[str, Any]:
    """What a paper needs to reproduce the campaign: exact code on both sides, uncommitted edits, host."""
    return {"mission_commit": _git(ROOT, "rev-parse", "HEAD"),
            "mission_dirty": _git(ROOT, "status", "--porcelain").splitlines(),
            "parent_dirty": _git(ctx.parent.root, "status", "--porcelain").splitlines(),
            "host": platform.node(), "platform": platform.platform(), "processor": platform.processor(),
            "cpu_count": os.cpu_count(), "python": sys.version.split()[0]}


def report(config: dict[str, Any], label: str) -> dict[str, Any]:
    campaign_dir = resolve(config["runs_root"]) / label
    campaign = status.read_json(campaign_dir / "campaign.json")
    if not campaign:
        raise FileNotFoundError(f"no campaign {label}")
    ctx = make_context(config, campaign["task"], label)
    task = tasks.get(campaign["task"])
    records: dict[str, list[dict[str, Any]]] = {}
    incomplete: dict[str, int] = {}
    abandoned: list[dict[str, Any]] = []
    for method, iid, seed in campaign["cells"]:
        run_dir = campaign_dir / method / iid / f"seed{seed}"
        record = status.read_json(run_dir / "record.json")
        if record:
            records.setdefault(method, []).append(record)
        else:
            incomplete[method] = incomplete.get(method, 0) + 1
            state = status.read_json(run_dir / "status.json")
            if state.get("state") == "abandoned":
                abandoned.append({"method": method, "item": iid, "seed": seed, "failures": state.get("failures")})
    result = {"label": label, "task": campaign["task"], "parent_commits": campaign.get("parent_commits"),
              "incomplete": incomplete, "abandoned": abandoned,
              "methods": {m: task.summarize(ctx, recs) for m, recs in sorted(records.items())}}
    status.write_json(campaign_dir / "report.json", result)
    return result


def compare(config: dict[str, Any], labels: list[str]) -> dict[str, Any]:
    """The reports of several campaigns of one task line side by side. ``label:T1,T2`` takes only those methods."""
    picks = [(label.split(":")[0], label.split(":")[1].split(",") if ":" in label else None) for label in labels]
    reports = [(report(config, label), only) for label, only in picks]
    if len({r["task"] for r, _ in reports}) != 1:
        raise ValueError(f"labels mix task lines: { {r['label']: r['task'] for r, _ in reports} }")
    merged: dict[str, Any] = {}
    for r, only in reports:
        for method, summary in r["methods"].items():
            if only is not None and method not in only:
                continue
            if method in merged:
                raise ValueError(f"method {method} is in more than one of {labels}")
            merged[method] = {**summary, "label": r["label"]}
    return {"task": reports[0][0]["task"], "labels": labels, "methods": dict(sorted(merged.items()))}


def markdown(result: dict[str, Any]) -> str:
    """One row per overall metric (weighted total first), one column per method; per kind stays in the JSON."""
    stats = result["methods"]
    lines = ["| 指标 | " + " | ".join(stats) + " |", "|---|" + "---|" * len(stats)]
    for k in dict.fromkeys(k for s in stats.values() for k in s):
        cells = [s.get(k) for s in stats.values()]
        if all(c is None or isinstance(c, (int, float)) for c in cells):
            lines.append(f"| {k} | " + " | ".join("—" if c is None else f"{c:g}" for c in cells) + " |")
    return "\n".join(lines)
