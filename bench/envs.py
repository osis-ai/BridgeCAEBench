"""Create one isolated venv per framework: .venvs/<extra> (methods on the same framework share it)."""

from __future__ import annotations

import os
import subprocess
from typing import Any

import methods
from bench.config import ROOT


def setup_envs(config: dict[str, Any], method_ids: list[str] | None = None, *, dry_run: bool = False) -> list[str]:
    registry = methods.discover()
    targets = [registry[m] for m in (method_ids or registry)]
    uv, python = config["env"]["uv"], config["env"]["python"]
    done = []
    for spec in targets:
        if spec["python"] != "venv":
            continue
        venv = ROOT / ".venvs" / spec["extra"]
        create = [uv, "venv", "--allow-existing", "--python", python, str(venv)]
        sync = [uv, "sync", "--frozen", "--active", "--extra", spec["extra"]]
        if not dry_run:
            subprocess.run(create, cwd=ROOT, check=True)
            subprocess.run(sync, cwd=ROOT, check=True, env={**os.environ, "VIRTUAL_ENV": str(venv)})
        done.append(f"{spec['id']}: {subprocess.list2cmdline(create)} && {subprocess.list2cmdline(sync)}")
    return done
