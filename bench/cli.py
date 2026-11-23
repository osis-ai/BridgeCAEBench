"""osis-bench command line. Every command prints one JSON document to stdout.

    osis-bench list
    osis-bench env    [--method T2 ...]
    osis-bench run    --task knowledge --label L [--methods T1 T2] [--items kn-faq-001] [--seeds 0 1] [--jobs 3] [--dry-run]
    osis-bench status --label L
    osis-bench report --label L [L2 ...] [--markdown]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import methods
import tasks
from bench import envs, runner, status
from bench.config import load_config, resolve


def _print(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="osis-bench")
    parser.add_argument("--config", help="default: configs/platform.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="registered methods and tasks")
    env = sub.add_parser("env", help="create per-method venvs with uv")
    env.add_argument("--method", nargs="+")
    env.add_argument("--dry-run", action="store_true")
    run = sub.add_parser("run", help="run (or resume) a campaign; finished runs are skipped")
    run.add_argument("--task", required=True, choices=sorted(tasks.TASKS))
    run.add_argument("--label", required=True)
    run.add_argument("--methods", nargs="+")
    run.add_argument("--items", nargs="+")
    run.add_argument("--seeds", nargs="+", type=int, default=[0])
    run.add_argument("--jobs", type=int, help="parallel cells (default: jobs in the config)")
    run.add_argument("--model", help="a gateway preset from models: in the config (default: model.name)")
    run.add_argument("--dry-run", action="store_true")
    sub.add_parser("status").add_argument("--label", required=True)
    rep = sub.add_parser("report", help="summary per method; several labels of one task line are put side by side")
    rep.add_argument("--label", required=True, nargs="+")
    rep.add_argument("--markdown", action="store_true", help="print a metric × method table instead of JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except Exception as exc:  # noqa: BLE001 - callers parse stdout; keep it JSON
        _print({"error": f"{type(exc).__name__}: {exc}"})
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.command == "list":
        _print({"methods": {k: {f: v.get(f) for f in ("name", "description", "python", "tasks", "serial")}
                            for k, v in methods.discover().items()},
                "tasks": sorted(tasks.TASKS)})
        return 0
    if args.command == "env":
        _print({"envs": envs.setup_envs(config, args.method, dry_run=args.dry_run)})
        return 0
    if args.command == "run":
        if args.model:
            config["model"] = {**config["model"], **config["models"][args.model], "name": args.model}
        jobs = args.jobs or int(config.get("jobs") or 1)
        result = runner.run_campaign(config, args.task, args.label, method_ids=args.methods,
                                     item_ids=args.items, seeds=args.seeds, jobs=jobs, dry_run=args.dry_run)
        _print(result)
        return 3 if result.get("gateway_down") else 1 if result.get("error") else 0  # 3: queue scripts stop
    if args.command == "status":
        _print(status.campaign_status(resolve(config["runs_root"]) / args.label))
        return 0
    result = runner.report(config, args.label[0]) if len(args.label) == 1 else runner.compare(config, args.label)
    if args.markdown:
        sys.stdout.write(runner.markdown(result) + "\n")
    else:
        _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
