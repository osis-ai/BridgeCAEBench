"""Draft diagnose/run_error cases: one reference error seeded into a test template.

    uv run python tools/seed_run_error.py         # writes datasets/diagnose/diagnose-run-*.yaml (skips existing)

The seed is a text edit (start overlay); the reference is the template. ``symptom`` (the error text the
engineer pastes) is left empty: it has to be captured by rebuilding the start in OSIS, which also proves
the seed really breaks the model. Until then the case stays draft and the task line skips it.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path
from typing import Callable

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.config import load_config, resolve  # noqa: E402
from scoring import cases  # noqa: E402
from scoring.checks import _same  # noqa: E402
from scoring.extract import extract  # noqa: E402
from tools.draft_build import SHORT, _Dumper  # noqa: E402

Files = dict[str, str]
CODES = {
    "section_ref": ("单元引用了不存在的截面号", "prep/_6_element.py"),
    "material_ref": ("单元引用了不存在的材料号", "prep/_6_element.py"),
    "stage_group_ref": ("施工阶段激活的单元组名与 _6 建的组名不一致", "prep/_10_stage.py"),
    "boundary_ref": ("施工阶段激活的边界组名与 _7 建的组名不一致", "prep/_10_stage.py"),
    "load_ref": ("施工阶段激活的荷载工况名与 _8 建的工况名不一致", "prep/_10_stage.py"),
    "tendon_prop_ref": ("钢束引用的钢束规格名未定义", "prep/_8_loadcase.py"),
}


def _once(files: Files, path: str, pattern: str, repl: Callable[[re.Match], str], nth: int = 0) -> str:
    """Edit the ``nth`` match of ``pattern`` in ``path``; return a description of the change."""
    text = files[path]
    found = list(re.finditer(pattern, text))
    if len(found) <= nth:
        raise ValueError(f"{path}: no match {nth} for {pattern!r}")
    m = found[nth]
    files[path] = text[:m.start()] + repl(m) + text[m.end():]
    return f"{m.group(0)} -> {repl(m)}"


def section_ref(files: Files) -> str:  # element.create(no, "BEAM3D", n1, n2, mat, sec1, sec2, ...)
    count = len(re.findall(r'engine\.element\.create\(\d+, "BEAM3D"', files["prep/_6_element.py"]))
    return _once(files, "prep/_6_element.py", r'(engine\.element\.create\(\d+, "BEAM3D", \d+, \d+, \d+, )(\d+), (\d+)',
                 lambda m: m.group(1) + "99, 99", nth=count // 2)


def material_ref(files: Files) -> str:
    count = len(re.findall(r'engine\.element\.create\(\d+, "BEAM3D"', files["prep/_6_element.py"]))
    return _once(files, "prep/_6_element.py", r'(engine\.element\.create\(\d+, "BEAM3D", \d+, \d+, )(\d+)',
                 lambda m: m.group(1) + "9", nth=count // 2)


def stage_group_ref(files: Files) -> str:
    return _once(files, "prep/_10_stage.py", r'(define_element\(\d+, \d+, ")([^"]+)(")', lambda m: m.group(1) + m.group(2) + "组" + m.group(3))


def boundary_ref(files: Files) -> str:
    return _once(files, "prep/_10_stage.py", r'(define_boundary\(\d+, \d+, ")([^"]+)(")', lambda m: m.group(1) + m.group(2) + "1" + m.group(3))


def load_ref(files: Files) -> str:
    def typo(m: re.Match) -> str:
        name = m.group(2)
        return m.group(1) + (name.replace("工况", "荷载") if "工况" in name else name + "工况") + m.group(3)
    return _once(files, "prep/_10_stage.py", r'(define_loadcase\(\d+, \d+, "[^"]*", ")([^"]+)(")', typo)


def tendon_prop_ref(files: Files) -> str:
    path = next(p for p, t in files.items() if "engine.tendon.prop.create(" in t)
    pattern = r'(engine\.tendon\.prop\.create\(")([^"]+)(")'
    used = set(re.findall(r'engine\.tendon\.shape\.create\("[^"]*", \d+, "([^"]+)"', files[path]))
    nth = next(k for k, m in enumerate(re.finditer(pattern, files[path])) if m.group(2) in used)  # unused props break nothing
    return _once(files, path, pattern, lambda m: m.group(1) + m.group(2) + "A" + m.group(3), nth)


SEEDS = {"section_ref": section_ref, "material_ref": material_ref, "stage_group_ref": stage_group_ref,
         "boundary_ref": boundary_ref, "load_ref": load_ref, "tendon_prop_ref": tendon_prop_ref}
PLAN = [  # (code, bridge, template)
    ("section_ref", "osis-bridge-precast-t-girder", "简支变连续-T梁-20m-边梁"),
    ("section_ref", "osis-bridge-cantilever-box", "变截面悬浇连续梁-70+130+70"),
    ("material_ref", "osis-bridge-hollow-slab", "预制简支-空心板-10m-边梁"),
    ("material_ref", "osis-bridge-rigid-frame-box", "变截面连续刚构-95+180+95-双肢"),
    ("stage_group_ref", "osis-bridge-precast-small-box", "简支变连续-小箱梁-25m-边梁"),
    ("stage_group_ref", "osis-bridge-conventional-box", "现浇等截面箱梁-26+35+26-单箱双室-12.25"),
    ("boundary_ref", "osis-bridge-precast-t-girder", "预制简支-T梁-40m-中梁"),
    ("boundary_ref", "osis-bridge-rigid-frame-box", "变截面连续刚构-108+200+108-双肢"),
    ("load_ref", "osis-bridge-conventional-box", "现浇等截面箱梁-30+30-单箱四室-20.7"),
    ("load_ref", "osis-bridge-cantilever-box", "变截面悬浇连续梁-76+140+76"),
    ("tendon_prop_ref", "osis-bridge-precast-small-box", "预制简支-小箱梁-20m-中梁"),
    ("tendon_prop_ref", "osis-bridge-hollow-slab", "预制简支-空心板-16m-中梁"),
]


def main() -> int:
    parent = resolve(load_config()["parent_repo"])
    names = cases.templates(parent / "skills")
    out = cases.DATASETS / "diagnose"
    codes = "codes:\n" + "".join(f"  {k}:\n    label: {v[0]}\n" for k, v in CODES.items())
    written = 0
    for number, (code, bridge, name) in enumerate(PLAN, start=1):
        cid = f"diagnose-run-{number:03d}"
        if (out / f"{cid}.yaml").exists():
            continue
        files = {f"prep/{p.name}": p.read_text(encoding="utf-8") for p in (names[name] / "prep").glob("*.py")}
        original = dict(files)
        change = SEEDS[code](files)
        for path, text in files.items():
            if text != original[path]:
                target = out / cid / "start" / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:  # facts the seed moves as a side effect may differ from the reference
            before = extract(cases.materialize({"id": cid, "start": "overlay:start", "base": name, "_dir": out / cid},
                                               "start", parent / "skills", Path(tmp) / "s"))
        after = extract(names[name])
        (out / cid / "start" / "reason_codes.yaml").write_text("# 跑不通类原因短码\n" + codes, encoding="utf-8")
        brief = ("重建模型时报错跑不通，报错如下。帮我查出原因并修好，不要改动无关部分，改完重建并求解确认能算通。\n"
                 "报错：\n{symptom}\n"
                 "回复里给出定位 JSON，reason_code 取自 py/reason_codes.yaml：\n"
                 '{"localizations":[{"reason_code":"短码"}]}\n')
        case = {"id": cid, "line": "diagnose", "bridge": bridge, "status": "draft", "version": 1, "kind": "run_error",
                "start": "overlay:start", "base": name, "reference": f"template:{name}", "hide_templates": [name],
                "brief": brief, "symptom": None, "hidden": {}, "must_ask": [], "checks": [], "preserve": True,
                "may_change": sorted(k for k in after if not _same(before.get(k), after[k])),
                "localization": [{"reason_code": code}], "seed": {"file": CODES[code][1], "how": change},
                "notes": f"跑不通：在模板 {name} 上种 {code}。symptom 待在 OSIS 中重建起始项目后采集（同时确认确实报错）。"}
        (out / f"{cid}.yaml").write_text(yaml.dump(case, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=200),
                                         encoding="utf-8")
        written += 1
    print(f"wrote {written} cases to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
