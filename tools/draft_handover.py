"""Draft handover cases: a test template cut off after a module, to be completed from a colleague's note.

    uv run python tools/draft_handover.py         # writes datasets/handover/handover-*.yaml (skips existing)

Variant A keeps prep/_0.._6 (boundary, loads, live load, stages to do); variant B keeps _0.._8 (live load
and stages to do). main.py stays, as the colleague wrote it. The start is an overlay that only deletes
files; ``may_change`` is every fact that differs between start and the full template, so ``preserve``
scores whether the agent left the colleague's modules alone.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.config import load_config, resolve  # noqa: E402
from scoring import cases  # noqa: E402
from scoring.extract import extract  # noqa: E402
from tools.draft_build import SHORT, _Dumper, _num, _secondary  # noqa: E402

VARIANTS = {
    "A": (["_7_boundary.py", "_8_loadcase.py", "_9_analysis.py", "_10_stage.py"],
          "控制参数、钢束线形、材料、截面、节点、单元已经建好（prep/_1～_6），边界、荷载、活载分析和施工阶段还没做"),
    "B": (["_9_analysis.py", "_10_stage.py"],
          "边界和荷载也已经做完（prep/_1～_8），只剩活载分析（含沉降）和施工阶段没做"),
}


def _stages(names: list[str]) -> str:
    shown = names if len(names) <= 8 else names[:3] + ["……"] + names[-5:]
    return f"施工阶段（共 {len(names)} 个）：" + " → ".join(n.split("_", 1)[-1] for n in shown)


def draft(name: str, folder: Path, bridge: str, number: int, variant: str, skills: Path) -> dict[str, Any]:
    removed, done = VARIANTS[variant]
    facts = extract(folder)
    secondary = _secondary(facts)
    stress = sorted({v for v in facts["tendon_stress"] if isinstance(v, (int, float))})
    temps, mm = facts["uniform_temp"], facts["settlement_mm"]
    notes, checks = [f"汽车荷载公路-I 级，车道数 {facts['lane_count']}"], [
        {"field": "live_grade", "op": "eq", "value": facts["live_grade"]},
        {"field": "lane_count", "op": "eq", "value": facts["lane_count"]}]
    if mm:
        notes.append(f"支座不均匀沉降主墩 {_num(max(mm))}mm、桥台 {_num(min(mm))}mm" if len(mm) > 1
                     else f"支座不均匀沉降 {_num(mm[0])}mm")
        checks.append({"field": "settlement_mm", "op": "approx_list", "value": mm, "tol": 0.5})
    if variant == "A":
        if secondary:
            notes.append("二期恒载 " + "、".join(f"{'' if c == '二期' else c + ' '}{_num(v)}kN/m" for c, v in secondary.items()))
            checks += [{"field": "line_load_kn", "op": "contains", "value": v} for v in sorted(set(secondary.values()))]
        if temps:
            notes.append(f"整体升温 {_num(max(temps))}℃、降温 {_num(abs(min(temps)))}℃，考虑正负温度梯度")
            checks += [{"field": "uniform_temp", "op": "approx_list", "value": temps, "tol": 0.5},
                       {"field": "has_temp_gradient", "op": "eq", "value": True}]
        if stress:
            notes.append(f"钢束张拉控制应力 {'、'.join(_num(s / 1e6) for s in stress)}MPa")
            checks += [{"field": "tendon_stress", "op": "contains", "value": s} for s in stress]
    names = facts.get("stage_names") or []
    if names:
        notes.append(_stages(names))
    checks.append({"field": "stage_count", "op": "eq", "value": facts["stage_count"]})
    brief = (f"这个项目是同事做到一半交给我的：{done}，main.py 已经写好调用顺序。"
             "请接着补完，不要改动已经做好的部分，最后重建并求解确认能算通。\n设计参数：\n"
             + "".join(f"- {n}\n" for n in notes))
    case: dict[str, Any] = {
        "id": f"handover-{SHORT[bridge]}-{number:03d}", "line": "handover", "bridge": bridge, "status": "draft",
        "version": 1, "kind": variant, "start": "overlay:start", "base": name, "reference": f"template:{name}",
        "hide_templates": [name], "brief": brief,
        "hidden": {k: facts[k] for k in ("lane_count", "settlement_mm", "uniform_temp", "tendon_stress", "stage_count")
                   if facts.get(k) not in (None, [])} | {"二期恒载_kN_m": secondary},
        "hidden_profile": True, "must_ask": [], "checks": checks, "preserve": True,
    }
    with tempfile.TemporaryDirectory() as tmp:
        start = Path(tmp) / "start"
        (start / "prep").mkdir(parents=True)
        for path in (folder / "prep").glob("*.py"):
            if path.name not in removed:
                (start / "prep" / path.name).write_bytes(path.read_bytes())
        before = extract(start)
    case["may_change"] = sorted(k for k in facts if before.get(k) != facts[k])
    case["notes"] = f"由 tools/draft_handover.py 从模板 {name} 截断生成：删去 {', '.join(removed)}。"
    return case


def main() -> int:
    parent = resolve(load_config()["parent_repo"])
    skills = parent / "skills"
    names = cases.templates(skills)
    split = yaml.safe_load((parent / "configs" / "datasets.yaml").read_text(encoding="utf-8"))["bridges"]
    out = cases.DATASETS / "handover"
    written = 0
    for bridge in SHORT:
        test = split[bridge]["test"]
        for number, (variant, name) in enumerate((("A", test[0]), ("B", test[-1])), start=1):
            case = draft(name, names[name], bridge, number, variant, skills)
            path = out / f"{case['id']}.yaml"
            if path.exists():
                continue
            (out / case["id"]).mkdir(parents=True, exist_ok=True)
            (out / case["id"] / "start.delete").write_text(
                "".join(f"prep/{f}\n" for f in VARIANTS[variant][0]), encoding="utf-8")
            path.write_text(yaml.dump(case, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=200),
                            encoding="utf-8")
            written += 1
    print(f"wrote {written} cases to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
