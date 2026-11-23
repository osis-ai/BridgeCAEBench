"""Draft diagnose/review cases: a model with planted mismatches against its design parameter table.

    uv run python tools/draft_review.py           # writes datasets/diagnose/diagnose-review-*.yaml (skips existing)

The start is the template with ``draft_change`` edits applied (the planted errors); the parameter table is
the template's build brief (correct values); the reference is the template. The agent lists the mismatches
by item name (fixed vocabulary in the brief) and fixes them; checks restore the template values.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.config import load_config, resolve  # noqa: E402
from scoring import cases  # noqa: E402
from scoring.extract import extract  # noqa: E402
from tools.draft_build import SHORT, _Dumper, draft  # noqa: E402
from tools.draft_change import EDITS  # noqa: E402

ITEMS = {"live_grade": "汽车荷载等级", "temperature": "整体温度", "settlement": "支座沉降", "concrete": "混凝土等级",
         "pavement": "二期恒载", "stress": "张拉控制应力", "humidity": "环境湿度"}
PLAN = {
    "osis-bridge-precast-t-girder": ("简支变连续-T梁-30m-边梁", ["concrete", "stress"]),
    "osis-bridge-precast-small-box": ("简支变连续-小箱梁-20m-中梁", ["live_grade", "temperature", "pavement"]),
    "osis-bridge-hollow-slab": ("预制简支-空心板-16m-边梁", ["humidity", "settlement"]),
    "osis-bridge-conventional-box": ("现浇等截面箱梁-25+25-单箱三室-16.7-1", ["concrete", "settlement", "live_grade"]),
    "osis-bridge-cantilever-box": ("变截面悬浇连续梁-76+140+76", ["stress", "temperature"]),
    "osis-bridge-rigid-frame-box": ("变截面连续刚构-56+5x100+56-双肢", ["pavement", "humidity", "concrete"]),
}
VOCAB = "、".join(["跨径", "梁高", *ITEMS.values()])


def main() -> int:
    parent = resolve(load_config()["parent_repo"])
    names = cases.templates(parent / "skills")
    out = cases.DATASETS / "diagnose"
    written = 0
    for number, (bridge, (name, plants)) in enumerate(PLAN.items(), start=1):
        cid = f"diagnose-review-{number:03d}"
        if (out / f"{cid}.yaml").exists():
            continue
        folder, facts = names[name], extract(names[name])
        files = {f"prep/{p.name}": p.read_text(encoding="utf-8") for p in (folder / "prep").glob("*.py")}
        original, planted_checks, may_change = dict(files), [], []
        for kind in plants:
            _, wrong = EDITS[kind].apply(files, facts)  # the "wrong" values; the checks below restore the design
            planted_checks += wrong
            may_change += EDITS[kind].may_change
        for path, text in files.items():
            if text != original[path]:
                target = out / cid / "start" / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        design = draft(name, folder, bridge, 1, [])
        table = design["brief"].removeprefix("建一个计算模型：").rsplit("建好后", 1)[0].strip().replace("。", "。\n").strip()
        fields = {c["field"] for c in planted_checks}
        checks = [c for c in design["checks"] if c["field"] in fields]
        brief = ("这个模型是按下面的设计参数建的，交给我之前请对照复核：找出模型里和设计参数不一致的地方，逐项改正，"
                 "改完重建并求解确认能算通。\n设计参数：\n" + table + "\n"
                 f"回复里给出复核结论 JSON，item 取自：{VOCAB}。\n"
                 '{"inconsistencies":[{"item":"混凝土等级","model":"模型里的值","design":"设计值"}]}\n')
        case = {"id": cid, "line": "diagnose", "bridge": bridge, "status": "draft", "version": 1, "kind": "review",
                "start": "overlay:start", "base": name, "reference": f"template:{name}", "hide_templates": [name],
                "brief": brief, "hidden": {}, "must_ask": [], "checks": checks, "preserve": True,
                "may_change": sorted(set(may_change) - fields),
                "localization": [{"item": ITEMS[k]} for k in plants],
                "notes": f"复核：在模板 {name} 上埋入 {', '.join(plants)}（tools/draft_change.py 的编辑）。"}
        (out / f"{cid}.yaml").write_text(yaml.dump(case, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=200),
                                         encoding="utf-8")
        written += 1
    print(f"wrote {written} cases to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
