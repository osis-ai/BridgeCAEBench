"""File-based run state. Everything an agent needs to monitor a campaign is on disk.

Per run:      runs/<label>/<method>/<item>/seed<k>/status.json
Per campaign: runs/<label>/campaign.json (expected cells, pid, start/finish)
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATES = ("preparing", "generating", "scoring", "done", "error", "abandoned")
MAX_FAILURES = 3  # platform failures of one cell before it is abandoned instead of retried
ACTIVE = {"preparing", "generating", "scoring"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def set_state(run_dir: Path, state: str, **extra: Any) -> None:
    assert state in STATES, state
    current = read_json(run_dir / "status.json")
    current.update(extra, state=state, updated_at=now(), updated_ts=time.time())
    current.setdefault("started_at", current["updated_at"])
    write_json(run_dir / "status.json", current)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) terminates the process on Windows; query it instead.
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def campaign_status(campaign_dir: Path) -> dict[str, Any]:
    campaign = read_json(campaign_dir / "campaign.json")
    if not campaign:
        raise FileNotFoundError(f"no campaign.json in {campaign_dir}")
    counts = {state: 0 for state in ("pending", *STATES)}
    by_method: dict[str, dict[str, int]] = {}
    errors, active, abandoned, scored_with_error = [], [], [], 0
    last_update = 0.0
    for method, item, seed in campaign["cells"]:
        run_dir = campaign_dir / method / item / f"seed{seed}"
        status = read_json(run_dir / "status.json")
        state = status.get("state", "pending")
        counts[state] += 1
        by_method.setdefault(method, {}).setdefault(state, 0)
        by_method[method][state] += 1
        last_update = max(last_update, float(status.get("updated_ts") or 0))
        cell = {"method": method, "item": item, "seed": seed}
        if state == "error":
            errors.append({**cell, "stage": status.get("stage"), "error": str(status.get("error", ""))[:300]})
        elif state == "abandoned":
            abandoned.append({**cell, "failures": status.get("failures") or []})
        elif state in ACTIVE:
            active.append({**cell, "state": state, "since": status.get("updated_at")})
        elif state == "done" and status.get("scored_error"):
            scored_with_error += 1
    alive = pid_alive(int(campaign.get("pid") or 0))
    total = len(campaign["cells"])
    return {
        "label": campaign["label"],
        "task": campaign["task"],
        "process_alive": alive,
        "finished": bool(campaign.get("finished_at")),
        "total": total,
        "counts": counts,
        "progress": round(counts["done"] / total, 4) if total else 1.0,
        "by_method": by_method,
        "active": active,
        "errors": errors,
        "abandoned": abandoned,
        "scored_with_error": scored_with_error,
        "last_update": datetime.fromtimestamp(last_update, timezone.utc).isoformat(timespec="seconds")
        if last_update else None,
        # Runs left in an active state by a dead process are retried on the next `run`.
        "stalled": bool(active) and not alive,
    }
