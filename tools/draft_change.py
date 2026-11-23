"""Draft change cases: a complete model plus one design change, with a scripted reference solution.

    uv run python tools/draft_change.py           # writes datasets/change/change-*.yaml (skips existing)

Geometry changes pair two templates of one family (start = train template, reference = test template with its
design conditions, ``CONDITIONS``, pinned back to the start's values as an overlay).
Every other change is an ``EDIT``: a precise text edit of the template code that becomes the reference
overlay; each edit declares the facts it changes, and verify_case confirms nothing else moved.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.config import load_config, resolve  # noqa: E402
from scoring import cases  # noqa: E402
from scoring.extract import extract  # noqa: E402
from tools.draft_build import SHORT, _Dumper, _num, _secondary  # noqa: E402

Files = dict[str, str]  # relative path (prep/_8_loadcase.py) -> text
NUM = r"-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?"


class Edit:
    """One design change: ``apply(files, facts)`` edits the code and returns (brief sentence, checks);
    ``may_change(facts)`` names the facts it changes, one key (``a.b``) of a dict fact where it touches one."""

    def __init__(self, may_change: Callable[[dict[str, Any]], list[str]],
                 apply: Callable[[Files, dict[str, Any]], tuple[str, list[dict]]]):
        self.may_change, self.apply = may_change, apply


def _sub(files: Files, pattern: str, repl: Callable[[re.Match], str], *, must: bool = True) -> int:
    total = 0
    for path, text in files.items():
        new, n = re.subn(pattern, repl, text)
        files[path], total = new, total + n
    if must and not total:
        raise ValueError(f"edit found nothing for {pattern!r}")
    return total


def live_grade(files: Files, facts: dict) -> tuple[str, list[dict]]:
    _sub(files, r'("HIGHWAY_)I(")', lambda m: m.group(1) + "II" + m.group(2))
    return ("这条路按二级公路标准建设，汽车荷载等级由公路-I 级改为公路-II 级。",
            [{"field": "live_grade", "op": "eq", "value": "HIGHWAY_II"}])


def temperature(files: Files, facts: dict) -> tuple[str, list[dict]]:
    up, down = max(facts["uniform_temp"]) + 5, min(facts["uniform_temp"]) - 5
    _sub(files, rf'(create\("UTEMP", [^,]+, "X", )({NUM})\)',
         lambda m: m.group(1) + f"{up if float(m.group(2)) > 0 else down:.1f})")
    return (f"按桥址气温资料复核，整体升温改为 {_num(up)}℃、整体降温改为 {_num(abs(down))}℃。",
            [{"field": "uniform_temp", "op": "approx_list", "value": [down, up], "tol": 0.5}])


def settlement(files: Files, facts: dict) -> tuple[str, list[dict]]:
    def bump(m: re.Match) -> str:
        v = float(m.group(2))
        return m.group(1) + f"{(abs(v) + 0.005) * (1 if v > 0 else -1):g}" + m.group(3)

    _sub(files, rf'(engine\.settlement\.group\.create\((?:"[^"]*"|\d+), )({NUM})(,)', bump)
    mm = sorted({round(v + 5, 1) for v in facts["settlement_mm"]})
    text = (f"主墩 {_num(max(mm))}mm、桥台 {_num(min(mm))}mm" if len(mm) > 1 else f"{_num(mm[0])}mm")
    return (f"地勘复核后地基条件变差，支座不均匀沉降加大为{text}。",
            [{"field": "settlement_mm", "op": "approx_list", "value": mm, "tol": 0.5}])


def concrete(files: Files, facts: dict) -> tuple[str, list[dict]]:
    old = facts["concrete_grade"]
    new = 55 if old != 55 else 50
    # main girder material only (rigid frames also define a pier concrete); creep fcuk follows the grade
    # the name may carry the grade with more text ("C50混凝土")
    _sub(files, rf'(engine\.material\.create\({NUM}, "[^"]*?)C{old}([^"]*"[^\n]*?"CONC"[^\n]*?")C{old}(")',
         lambda m: f"{m.group(1)}C{new}{m.group(2)}C{new}{m.group(3)}")
    _sub(files, rf"fcuk={old}e6", lambda m: f"fcuk={new}e6", must=False)
    return (f"主梁混凝土由 C{old} 改为 C{new}，收缩徐变参数相应调整。",
            [{"field": "concrete_grade", "op": "eq", "value": new}])


def _pavement_case(facts: dict) -> tuple[str, str]:
    """(case as the brief names it, load case name in the code)."""
    secondary = _secondary(facts)
    case = next((c for c in secondary if "铺装" in c), None) or next(iter(secondary))
    return case, next(k for k in facts["line_loads"] if re.sub(r"(工况|_.*|[左右])$", "", k) == case)


def pavement(files: Files, facts: dict) -> tuple[str, list[dict]]:
    secondary = _secondary(facts)
    case, raw = _pavement_case(facts)
    old = secondary[case]
    new = round(old * 1.2, 2)
    _sub(files, rf'(engine\.load\.get\("{re.escape(raw)}"\)\.create\("LINE"[^\n]*?)(-?{old * 1000:g}(?:\.0)?)\b',
         lambda m: m.group(1) + f"{-new * 1000:.1f}")
    _sub(files, rf'(engine\.load\.get\("{re.escape(raw)}"\)\.create\("LINE"[^\n]*?)(-?{old * 1000:g}(?:\.0)?)\b',
         lambda m: m.group(1) + f"{-new * 1000:.1f}", must=False)  # the end value of a two-point line load
    what = "桥面铺装加厚（10cm 改为 12cm），铺装荷载" if "铺装" in case else "二期恒载复核后增加，二期荷载"
    checks = [{"field": "line_load_kn", "op": "contains", "value": new}]
    if list(secondary.values()).count(old) == 1:
        checks.append({"field": "line_load_kn", "op": "absent", "value": old})
    return f"{what}由 {_num(old)}kN/m 改为 {_num(new)}kN/m。", checks


def stress(files: Files, facts: dict) -> tuple[str, list[dict]]:
    old = next(v for v in facts["tendon_stress"] if isinstance(v, (int, float)))
    new = 1302e6 if abs(old - 1302e6) > 1 else 1395e6
    n = _sub(files, rf'("PST", "[^"]*", "[^"]*", "[^"]*", )({NUM}), ({NUM})\)',
             lambda m: m.group(1) + (f"{new:.1f}, {new:.1f})" if abs(float(m.group(2)) - old) < 1 else m.group(0)[len(m.group(1)):]))
    assert n
    return (f"为控制锚下应力，钢束张拉控制应力由 {_num(old / 1e6)}MPa 改为 {_num(new / 1e6)}MPa（约 {new / 1860e6:.2f}fpk）。",
            [{"field": "tendon_stress", "op": "contains", "value": new}, {"field": "tendon_stress", "op": "absent", "value": old}])


def humidity(files: Files, facts: dict) -> tuple[str, list[dict]]:
    _sub(files, rf"avg_humidity={NUM}", lambda m: "avg_humidity=55.0")
    return ("桥位在西北干燥地区，环境年平均相对湿度按 55% 计。",
            [{"field": "humidity", "op": "approx", "value": 55.0, "tol": 1}])


def _stage(facts: dict, word: str) -> str:
    return next(n for n in facts["stage_durations"] if word in n)


def storage(files: Files, facts: dict) -> tuple[str, list[dict]]:
    name = _stage(facts, "存梁")
    old = facts["stage_durations"][name]
    _sub(files, rf'(engine\.stage\.create\({NUM}, "{re.escape(name)}", )({NUM})\)', lambda m: m.group(1) + "90.0)")
    return (f"预制梁存梁期由 {_num(old)} 天改为 90 天。",
            [{"field": f"stage_durations.{name}", "op": "approx", "value": 90.0, "tol": 0.5}])


def creep(files: Files, facts: dict) -> tuple[str, list[dict]]:
    name = _stage(facts, "徐变")
    _sub(files, rf'(engine\.stage\.create\({NUM}, "{re.escape(name)}", )({NUM})\)', lambda m: m.group(1) + "7300.0)")
    return ("成桥后收缩徐变计算时长由 10 年改为 20 年。",
            [{"field": "creep_days", "op": "approx", "value": 7300.0, "tol": 1}])


EDITS = {
    "live_grade": Edit(lambda f: ["live_grade"], live_grade),
    "temperature": Edit(lambda f: ["uniform_temp"], temperature),
    "settlement": Edit(lambda f: ["settlements", "settlement_mm"], settlement),
    "concrete": Edit(lambda f: ["concrete_grade"], concrete),
    "pavement": Edit(lambda f: [f"line_loads.{_pavement_case(f)[1]}"], pavement),  # line_load_kn is derived
    "stress": Edit(lambda f: ["tendon_stress"], stress),
    "humidity": Edit(lambda f: ["humidity"], humidity),
    "storage": Edit(lambda f: [f"stage_durations.{_stage(f, '存梁')}"], storage),
    "creep": Edit(lambda f: [f"stage_durations.{_stage(f, '徐变')}", "creep_days"], creep),
}
# bridge -> (geometry pair, base template for edits, single edits, combined edits)
PLAN = {
    "osis-bridge-precast-t-girder": (("简支变连续-T梁-35m-中梁", "简支变连续-T梁-40m-中梁"), "简支变连续-T梁-25m-中梁",
                                     ["live_grade", "pavement", "storage", "stress"], ["concrete", "temperature"]),
    "osis-bridge-precast-small-box": (("简支变连续-小箱梁-35m-边梁", "简支变连续-小箱梁-40m-边梁"), "预制简支-小箱梁-30m-边梁",
                                      ["temperature", "concrete", "humidity", "live_grade"], ["storage", "pavement"]),
    "osis-bridge-hollow-slab": (("预制简支-空心板-13m-中梁", "预制简支-空心板-16m-中梁"), "预制简支-空心板-10m-边梁",
                                ["pavement", "settlement", "concrete", "storage"], ["live_grade", "humidity"]),
    "osis-bridge-conventional-box": (("现浇等截面箱梁-25+25-单箱四室-20.7", "现浇等截面箱梁-30+30-单箱四室-20.7"),
                                     "现浇等截面箱梁-30+35+22-单箱三室-16.75",
                                     ["temperature", "settlement", "stress", "humidity"], ["live_grade", "pavement"]),
    "osis-bridge-cantilever-box": (("变截面悬浇连续梁-60+110+60", "变截面悬浇连续梁-65+120+65"), "变截面悬浇连续梁-70+130+70",
                                   ["settlement", "concrete", "creep", "pavement"], ["temperature", "humidity"]),
    "osis-bridge-rigid-frame-box": (("变截面连续刚构-102+190+102-双肢", "变截面连续刚构-108+200+108-双肢"),
                                    "变截面连续刚构-95+180+95-双肢",
                                    ["temperature", "live_grade", "creep", "stress"], ["settlement", "humidity"]),
}


def _layout(spans: list[float]) -> str:
    return "+".join(_num(round(s)) for s in spans)


# Design conditions a geometry change keeps ("荷载和其他设计条件不变"): the reference template is pinned to the start
# template's values on these, so they stay out of may_change and the agent must keep them.
CONDITIONS = ("concrete_grade", "line_loads", "settlements", "settlement_mm", "pier_heights", "live_grade",
              "uniform_temp", "humidity")


def pin(files: Files, a: dict, b: dict) -> None:
    """Edit the reference template's code (facts ``b``) to the start's (facts ``a``) design conditions."""
    if a["concrete_grade"] != b["concrete_grade"]:
        old, new = b["concrete_grade"], a["concrete_grade"]
        _sub(files, rf'(engine\.material\.create\({NUM}, "[^"]*?)C{old}([^"]*"[^\n]*?"CONC"[^\n]*?")C{old}(")',
             lambda m: f"{m.group(1)}C{new}{m.group(2)}C{new}{m.group(3)}")
        _sub(files, rf"fcuk={old}e6", lambda m: f"fcuk={new}e6", must=False)
    for case, values in b["line_loads"].items():
        if a["line_loads"].get(case) != values:
            assert len(values) == 1 and len(a["line_loads"][case]) == 1, (case, values)
            old, new = values[0], a["line_loads"][case][0]
            _sub(files, rf'(engine\.load\.get\("{re.escape(case)}"\)\.create\("LINE"[^\n]*?){re.escape(f"{old:.1f}")}\b',
                 lambda m: m.group(1) + f"{new:.1f}")
            _sub(files, rf'(engine\.load\.get\("{re.escape(case)}"\)\.create\("LINE"[^\n]*?){re.escape(f"{old:.1f}")}\b',
                 lambda m: m.group(1) + f"{new:.1f}", must=False)  # the end value of a two-point line load
    if a["settlements"] != b["settlements"]:
        start_groups = dict(re.findall(rf'engine\.settlement\.group\.create\("([^"]+)", ({NUM}),',
                                       "\n".join(a["_files"].values())))
        _sub(files, rf'(engine\.settlement\.group\.create\("([^"]+)", )({NUM})(,)',
             lambda m: m.group(1) + start_groups[m.group(2)] + m.group(4))
    if a.get("pier_heights") != b.get("pier_heights"):
        (h_a,), (h_b,), top = set(a["pier_heights"]), set(b["pier_heights"]), -b["H_root"]

        def move(m: re.Match) -> str:  # scale the pier below the girder: same nodes, start's height
            z = float(m.group(2))
            return m.group(0) if z >= top - 1e-6 else f"{m.group(1)}{round(top + (z - top) * h_a / h_b, 4)})"

        _sub(files, rf"(engine\.node\.create\({NUM}, {NUM}, {NUM}, )({NUM})\)", move)


def _facts(files: Files) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        for path, text in files.items():
            (Path(tmp) / path).parent.mkdir(parents=True, exist_ok=True)
            (Path(tmp) / path).write_text(text, encoding="utf-8")
        return extract(Path(tmp))


def geometry(bridge: str, start: str, target: str, names: dict[str, Path], out_dir: Path) -> dict[str, Any]:
    a, b = extract(names[start]), extract(names[target])
    files = {f"prep/{p.name}": p.read_text(encoding="utf-8") for p in (names[target] / "prep").glob("*.py")}
    original = dict(files)
    pin(files, {**a, "_files": {p.name: p.read_text(encoding="utf-8") for p in (names[start] / "prep").glob("*.py")}},
        b)
    b = _facts(files)
    kept = [k for k in CONDITIONS if a.get(k) != b.get(k)]
    assert not kept, f"{start} -> {target}: reference still differs on {kept}"
    for path, text in files.items():
        if text != original[path]:
            (out_dir / "reference" / path).parent.mkdir(parents=True, exist_ok=True)
            (out_dir / "reference" / path).write_text(text, encoding="utf-8")
    variable = abs(b["H_root"] - b["H_mid"]) > 0.01
    height = (f"根部梁高改为 {_num(round(b['H_root'], 2))}m、跨中梁高改为 {_num(round(b['H_mid'], 2))}m" if variable
              else f"梁高改为 {_num(b['H_mid'])}m")
    if bridge in ("osis-bridge-precast-t-girder", "osis-bridge-precast-small-box", "osis-bridge-hollow-slab"):
        what = f"标准跨径由 {_num(a['span_lengths'][0])}m 改为 {_num(b['span_lengths'][0])}m（跨数不变），采用对应跨径的通用图"
    else:
        what = f"跨径布置由 {_layout(a['span_lengths'])}m 调整为 {_layout(b['span_lengths'])}m"
    checks = [{"field": "span_lengths", "op": "approx_list", "value": b["span_lengths"], "tol": 0.5},
              {"field": "H_mid", "op": "approx", "value": round(b["H_mid"], 2), "tol": 0.02}]
    if variable:
        checks.append({"field": "H_root", "op": "approx", "value": round(b["H_root"], 2), "tol": 0.02})
    changed = (k for k in b if a.get(k) != b[k] and k not in {c["field"] for c in checks} and k not in CONDITIONS)
    return {"kind": "geometry", "start": f"template:{start}", "base": target,
            "reference": "overlay:reference" if files != original else f"template:{target}",
            "hide_templates": [target],
            "brief": f"方案调整：{what}，{height}；截面、钢束、施工阶段随之调整，荷载和其他设计条件不变。",
            "checks": checks, "may_change": sorted(changed),
            "notes": f"几何变更：起始模板 {start}，参考解为模板 {target}，设计条件（{'、'.join(CONDITIONS)}）改回起始值。"}


def edited(base: str, folder: Path, kinds: list[str], out_dir: Path) -> dict[str, Any]:
    facts = extract(folder)
    files = {f"prep/{p.name}": p.read_text(encoding="utf-8") for p in (folder / "prep").glob("*.py")}
    original = dict(files)
    sentences, checks, may_change = [], [], []
    for kind in kinds:
        sentence, new_checks = EDITS[kind].apply(files, facts)
        sentences.append(sentence)
        checks += new_checks
        may_change += EDITS[kind].may_change(facts)
    for path, text in files.items():
        if text != original[path]:
            target = out_dir / "reference" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    lead = "设计变更：" if len(kinds) == 1 else "设计变更（两项）："
    return {"kind": "+".join(kinds), "start": f"template:{base}", "base": base, "reference": "overlay:reference",
            "hide_templates": [base], "brief": lead + "".join(sentences) + "其他不变，改完重建并求解确认能算通。",
            "checks": checks, "may_change": sorted(set(may_change) - {c["field"] for c in checks}),
            "notes": f"脚本变更（tools/draft_change.py: {', '.join(kinds)}），起始模板 {base}。"}


def main() -> int:
    parent = resolve(load_config()["parent_repo"])
    names = cases.templates(parent / "skills")
    out = cases.DATASETS / "change"
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for bridge, (pair, base, singles, combined) in PLAN.items():
        jobs = [("geometry", None)] + [(k, [k]) for k in singles] + [("combined", combined)]
        for number, (label, kinds) in enumerate(jobs, start=1):
            cid = f"change-{SHORT[bridge]}-{number:03d}"
            if (out / f"{cid}.yaml").exists():
                continue
            body = (geometry(bridge, *pair, names, out / cid) if kinds is None
                    else edited(base, names[base], kinds, out / cid))
            if body["kind"] == "geometry":
                body["brief"] += "改完重建并求解确认能算通。"
            case = {"id": cid, "line": "change", "bridge": bridge, "status": "draft", "version": 1, **body,
                    "hidden": {}, "hidden_profile": True, "must_ask": [], "preserve": True}
            order = ["id", "line", "bridge", "status", "version", "kind", "start", "base", "reference", "hide_templates",
                     "brief", "hidden", "hidden_profile", "must_ask", "checks", "preserve", "may_change", "notes"]
            case = {k: case[k] for k in order if k in case}
            (out / f"{cid}.yaml").write_text(yaml.dump(case, Dumper=_Dumper, allow_unicode=True, sort_keys=False,
                                                       width=200), encoding="utf-8")
            written += 1
    print(f"wrote {written} cases to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
