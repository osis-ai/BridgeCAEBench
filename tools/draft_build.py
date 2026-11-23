"""Draft build cases from the test templates: brief, checks and hidden parameters from the template code.

    uv run python tools/draft_build.py            # writes datasets/build/build-*.yaml (skips existing files)

Every value a brief states comes from the template's extracted facts and has a check, so a correct model
passes every check. Missing-parameter variants drop the parameters in ``MISSING`` from the brief and list
them in ``must_ask``. Briefs are drafts: review the wording, then keep or edit by hand (edits are kept,
existing files are not overwritten).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.config import load_config, resolve  # noqa: E402
from scoring.cases import DATASETS, templates  # noqa: E402
from scoring.extract import extract  # noqa: E402

SHORT = {"osis-bridge-precast-t-girder": "tgirder", "osis-bridge-precast-small-box": "smallbox",
         "osis-bridge-hollow-slab": "hollow", "osis-bridge-conventional-box": "cipbox",
         "osis-bridge-cantilever-box": "cantilever", "osis-bridge-rigid-frame-box": "rigid"}
GIRDER = {"tgirder": "T 梁", "smallbox": "小箱梁", "hollow": "空心板"}
SECONDARY = ("铺装", "护栏", "二期", "人行道", "中央分隔带")  # 二期恒载 load cases (人群荷载 is live load)
# Missing-parameter variants: which templates, and what their brief leaves out.
MISSING = {
    "预制简支-T梁-30m-边梁": ["梁高", "二期恒载"],
    "简支变连续-T梁-25m-中梁": ["梁高", "张拉控制应力"],
    "简支变连续-小箱梁-25m-边梁": ["梁高", "二期恒载"],
    "预制简支-小箱梁-40m-中梁": ["混凝土等级", "温度"],
    "预制简支-空心板-16m-中梁": ["梁高", "二期恒载"],
    "预制简支-空心板-10m-边梁": ["混凝土等级", "沉降"],
    "现浇等截面箱梁-26+35+26-单箱双室-12.25": ["梁高", "张拉控制应力"],
    "现浇等截面箱梁-30+45+30-单箱三室-16.5": ["梁高", "温度"],
    "变截面悬浇连续梁-65+120+65": ["梁高", "沉降"],
    "变截面悬浇连续梁-76+140+76": ["混凝土等级", "二期恒载"],
    "变截面连续刚构-95+180+95-双肢": ["梁高", "沉降"],
    "变截面连续刚构-108+200+108-双肢": ["二期恒载", "温度"],
}


def _num(x: float) -> str:
    return f"{round(x, 3):g}"


def _layout(spans: list[float]) -> str:
    """[56, 100, 100, 100, 100, 100, 56] -> 56+5×100+56."""
    groups: list[list[float]] = []
    for span in (round(s) for s in spans):
        if groups and groups[-1][0] == span:
            groups[-1].append(span)
        else:
            groups.append([span])
    return "+".join(f"{len(g)}×{_num(g[0])}" if len(g) > 2 else "+".join(_num(x) for x in g) for g in groups)


def _girder(short: str) -> str:
    return " T 梁" if short == "tgirder" else GIRDER[short]


def _width(template: Path) -> str | None:
    profile = (template / "项目画像.md").read_text(encoding="utf-8") if (template / "项目画像.md").is_file() else ""
    found = re.search(r"桥宽[:：]\s*([\d.]+)\s*m", profile)
    if found:
        return found.group(1)
    if "现浇" not in template.name:
        return None
    if "变宽" in template.name:  # 现浇变宽等截面箱梁-40-单箱四室-23-18.45
        return "-".join(template.name.split("-")[-2:])
    found = re.search(r"室-([\d.]+)", template.name)  # ...-单箱双室-12.75-1: the trailing -1 is a variant number
    return found.group(1) if found else None


def _secondary(facts: dict[str, Any]) -> dict[str, float]:
    """二期恒载 cases with one value each, in kN/m."""
    out = {}
    for case, values in facts["line_loads"].items():
        nums = [v for v in values if isinstance(v, (int, float))]
        if any(k in case for k in SECONDARY) and len(nums) == 1 and abs(nums[0]) >= 100:
            out[re.sub(r"(工况|_.*|[左右])$", "", case) or case] = round(abs(nums[0]) / 1000, 3)
    return out


def _system(short: str, name: str, facts: dict[str, Any], width: str | None) -> str:
    spans = facts["span_lengths"]
    layout = _layout(spans)
    if short in GIRDER:
        position = "中梁" if "中梁" in name else "边梁"
        if "简支变连续" in name:
            return (f"{len(spans)}×{_num(spans[0])}m 先简支后连续预制{_girder(short)}，一联 {len(spans)} 跨，只建一片{position}。"
                    "预制梁架设后浇筑墩顶湿接缝、张拉负弯矩束，体系转换为连续。")
        return f"单跨 {_num(spans[0])}m 预制简支{_girder(short)}，只建一片{position}，整跨预制、架设即成桥。"
    if short == "cipbox":
        cells = re.search(r"单箱[双三四单]室", name).group(0)
        wide = f"桥宽由 {width.replace('-', 'm 渐变到 ')}m" if width and "-" in width else f"桥宽 {width}m"
        return (f"{layout}m 现浇预应力混凝土{'连续' if len(spans) > 1 else '简支'}箱梁，{cells}，{wide}，"
                "满堂支架整跨现浇、一次落架。")
    if short == "cantilever":
        return (f"{layout}m 变截面预应力混凝土连续箱梁，单箱单室，桥宽 {width}m，主墩两侧挂篮对称悬臂浇筑，"
                "先边跨后中跨合龙。")
    piers = "双肢薄壁墩" if "双肢" in name else "单肢墩"
    return (f"{layout}m 变截面预应力混凝土连续刚构，单箱单室，桥宽 {width}m，墩梁固结、{piers}，"
            "挂篮对称悬臂浇筑，先边跨后中跨合龙。")


def draft(name: str, folder: Path, bridge: str, number: int, missing: list[str]) -> dict[str, Any]:
    short = SHORT[bridge]
    facts = extract(folder)
    width = _width(folder)
    variable = abs((facts["H_root"] or 0) - (facts["H_mid"] or 0)) > 0.01
    secondary = _secondary(facts)
    stress = sorted({v for v in facts["tendon_stress"] if isinstance(v, (int, float))})
    temps = facts["uniform_temp"]
    checks: list[dict[str, Any]] = [
        {"field": "span_lengths", "op": "approx_list", "value": facts["span_lengths"], "tol": 0.5}]
    parts = [_system(short, name, facts, width)]

    def say(label: str, text: str, *new_checks: dict[str, Any]) -> None:
        checks.extend(new_checks)  # a missing parameter is still checked: the agent has to ask for it
        if label not in missing:
            parts.append(text)

    if variable:
        say("梁高", f"根部梁高 {_num(round(facts['H_root'], 2))}m，跨中梁高 {_num(round(facts['H_mid'], 2))}m。",
            {"field": "H_root", "op": "approx", "value": round(facts["H_root"], 2), "tol": 0.02},
            {"field": "H_mid", "op": "approx", "value": facts["H_mid"], "tol": 0.02})
    else:
        say("梁高", f"梁高 {_num(facts['H_mid'])}m。", {"field": "H_mid", "op": "approx", "value": facts["H_mid"], "tol": 0.02})
    say("混凝土等级", f"主梁 C{facts['concrete_grade']}。",
        {"field": "concrete_grade", "op": "eq", "value": facts["concrete_grade"]})
    loads = ["汽车荷载公路-I 级"]
    checks.append({"field": "live_grade", "op": "eq", "value": facts["live_grade"]})
    if secondary and "二期恒载" not in missing:
        loads.append("二期恒载 " + "、".join(f"{'' if c == '二期' else c + ' '}{_num(v)}kN/m" for c, v in secondary.items()))
    checks += [{"field": "line_load_kn", "op": "contains", "value": v} for v in sorted(set(secondary.values()))]
    if temps and "温度" not in missing:
        loads.append(f"整体升温 {_num(max(temps))}℃、降温 {_num(abs(min(temps)))}℃，考虑正负温度梯度")
    if temps:
        checks += [{"field": "uniform_temp", "op": "approx_list", "value": temps, "tol": 0.5},
                   {"field": "has_temp_gradient", "op": "eq", "value": True}]
    if facts["settlement_mm"]:
        mm = facts["settlement_mm"]
        if "沉降" not in missing:
            loads.append(f"支座不均匀沉降主墩 {_num(max(mm))}mm、桥台 {_num(min(mm))}mm" if len(mm) > 1
                         else f"支座不均匀沉降 {_num(mm[0])}mm")
        checks.append({"field": "settlement_mm", "op": "approx_list", "value": mm, "tol": 0.5})
    parts.append("荷载：" + "；".join(loads) + "。")
    if stress:
        say("张拉控制应力", f"钢束张拉控制应力 {'、'.join(_num(s / 1e6) for s in stress)}MPa。",
            *[{"field": "tendon_stress", "op": "contains", "value": s} for s in stress])
    if facts["humidity"]:
        parts.append(f"环境年平均相对湿度 {_num(facts['humidity'])}%。")
        checks.append({"field": "humidity", "op": "approx", "value": facts["humidity"], "tol": 1})
    parts.append("建好后重建并求解，确认模型能算通。")

    hidden = {k: facts[k] for k in ("span_lengths", "H_mid", "H_root", "T_mid", "T_root", "T_top_mid", "T_top_root",
                                    "web_t_mid", "web_t_support", "beam_width", "zero_block_len", "concrete_grade",
                                    "live_grade", "lane_count", "uniform_temp", "has_temp_gradient", "settlement_mm",
                                    "humidity", "tendon_stress", "tendon_count", "stage_count", "stage_names")
              if facts.get(k) not in (None, [], "")}
    hidden["二期恒载_kN_m"] = secondary
    if width:
        hidden["桥宽_m"] = width
    kind = "missing" if missing else "complete"
    return {
        "id": f"build-{short}-{number:03d}", "line": "build", "bridge": bridge, "status": "draft", "version": 1,
        "kind": kind, "start": "empty", "reference": f"template:{name}", "hide_templates": [name],
        "brief": "建一个计算模型：" + "".join(parts),
        "hidden": hidden, "hidden_profile": True, "must_ask": list(missing), "checks": checks,
        "notes": f"由 tools/draft_build.py 从模板 {name} 的代码提取生成（{kind}）。",
    }


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


_Dumper.add_representer(str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None))


def main() -> int:
    parent = resolve(load_config()["parent_repo"])
    names = templates(parent / "skills")
    split = yaml.safe_load((parent / "configs" / "datasets.yaml").read_text(encoding="utf-8"))["bridges"]
    out = DATASETS / "build"
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for bridge in SHORT:
        number = 0
        for name in split[bridge]["test"]:
            for missing in ([[]] + ([MISSING[name]] if name in MISSING else [])):
                number += 1
                case = draft(name, names[name], bridge, number, missing)
                case["brief"] = case["brief"].replace("。", "。\n").strip() + "\n"
                path = out / f"{case['id']}.yaml"
                if path.exists():
                    continue
                path.write_text(yaml.dump(case, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=200),
                                encoding="utf-8")
                written += 1
    print(f"wrote {written} cases to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
