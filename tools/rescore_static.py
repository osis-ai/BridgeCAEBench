"""按现在的 scoring/extract.py 离线重算已完成格子的静态分项（C、P、M，Diagnose 的 R，Project 各轮），不重跑、不求解。
原因：OSIS 导出的模型里梁单元线荷载有的写成 BLINE、湿度按位置写在规范名之后，旧提取器读不到，线荷载检查读空、P 误判改动。
用法：uv run python tools/rescore_static.py <task> <label> [--dry-run]
原 record.json 备份为 record.pre_bline.json（已有则不覆盖）。"""
import argparse, json, shutil, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench import runner, status  # noqa: E402
from bench.config import load_config  # noqa: E402
import tasks  # noqa: E402
from tasks import _project as p  # noqa: E402

MODEL = {"deepseek": "deepseek-v4.1-flash", "gpt": "gpt-6.1-sol"}
PARTS = ("checks", "preserve", "conformance", "line_repair", "turn_quality", "first_failed_turn", "score", "total")


def rescore(task_name, task, ctx, item, run_dir, record):
    if task_name == "project":  # turns are scored from turn<k>/ copies only, no OSIS needed
        generation = status.read_json(run_dir / "generation.json")
        keep = {k: record[k] for k in ("method", "seed", "generation_status") if k in record}
        return {**record, **task.score(ctx, item, run_dir, generation), **keep}
    if record.get("error") or item.get("kind") == "infeasible":
        return record
    new = dict(record)
    if task_name == "build":
        found = p.static(ctx, item, run_dir, task.scored_checks(item))
        new.update(checks=p.part(found["checks"]), conformance=p.part(found.get("conformance")))
    else:
        found = p.static(ctx, item, run_dir)
        new["preserve"] = p.part(found.get("preserve"))
        if task_name == "diagnose":
            new["line_repair"] = task.repair(item, run_dir, record["state"], found)
        else:
            new.update(checks=p.part(found["checks"]), conformance=p.part(found.get("conformance")))
    new["detail"] = found
    new.update(p.grade(task_name, new))
    return new


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task"), ap.add_argument("label"), ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    config = load_config(None)
    config["model"] = {**config["model"], "name": MODEL[a.label.rsplit("-", 1)[-1]]}
    ctx, task = runner.make_context(config, a.task, a.label), tasks.get(a.task)
    items = {task.item_id(i): i for i in task.load_items(ctx)}
    changed = 0
    for rec_path in sorted(ctx.campaign_dir.glob("T*/*/seed0/record.json")):
        run_dir = rec_path.parent
        if status.read_json(run_dir / "status.json").get("state") != "done":
            continue
        record = status.read_json(rec_path)
        if not (run_dir / "model" / "py" / "prep").is_dir() and a.task != "project" and not record.get("error"):
            print(run_dir.parent.parent.name, run_dir.parent.name, "skip: no exported model"); continue
        new = rescore(a.task, task, ctx, items[record["item_id"]], run_dir, record)
        diff = {k: (record.get(k), new.get(k)) for k in PARTS if record.get(k) != new.get(k)}
        if not diff:
            continue
        changed += 1
        print(run_dir.parent.parent.name, run_dir.parent.name, json.dumps(diff, ensure_ascii=False))
        if not a.dry_run:
            backup = run_dir / "record.pre_bline.json"
            if not backup.exists():
                shutil.copy2(rec_path, backup)
            status.write_json(rec_path, new)
    print("changed", changed)


if __name__ == "__main__":
    main()
