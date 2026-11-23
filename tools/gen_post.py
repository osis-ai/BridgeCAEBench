"""Generate every template's post module (``<template>/post/main.py``) with the osis-check skill's scripts/gen_post.py.

    uv run python tools/gen_post.py                          # every template of the parent repo's skills
    uv run python tools/gen_post.py 预制简支-空心板-10m-中梁 ...

Each template is rebuilt in OSIS (solver mode, not solved) so the skill script can read its load cases. Code checks
use A-class partially prestressed members, post-tensioned; precast girders are "Pre", the rest cast in place.
Regenerate after a template's load cases change.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import osis  # noqa: E402
from bench.config import load_config, resolve  # noqa: E402
from scoring.cases import templates  # noqa: E402

PRECAST = {"osis-bridge-hollow-slab", "osis-bridge-precast-t-girder", "osis-bridge-precast-small-box"}
_RUN = """import runpy
import sys
from pathlib import Path

here = Path(__file__).parent
runpy.run_path(str(here / "prep" / "main.py"), run_name="__main__")
sys.argv = ["gen_post.py", "--construct", {construct!r}, "--name", {name!r}, "--out", str(here / "post" / "main.py")]
runpy.run_path({script!r}, run_name="__main__")
"""


# OSIS 5.1 验算遇到既无纵筋也无钢束的截面报错 zero-axial moment capacity is not positive：刚构的墩、
# 不配筋的预制梁在锚固点外的端头单元。OSIS 开发的建议是选单元时跳过它们。运行时按模型取，模型改了也对。
_BARE = '''

def bare_elements(engine: OSISEngine) -> list[int]:
    """既没纵筋、两端也不都在钢束范围内的单元（如刚构的墩、梁端锚固点外的单元），验算时跳过。"""
    # shortcut: 钢束范围按整体 x 判断，只适合沿 x 布置的直桥；平曲线桥要改成按钢束线形投影
    rebar = {e for s in engine.section.all() if s.rebar.longitudinal_rebars_count for e in s.related_elements}
    x = {n.no: n.x for n in engine.node.all()}
    groups = {g.name: set(g.elements) for g in engine.element.group.all()}
    spans = [(groups.get(s.ele_grp, set()), min(p["x"] for p in s.points) - 1e-6, max(p["x"] for p in s.points) + 1e-6)
             for s in engine.tendon.shape.all() if s.points]
    return sorted(e.no for e in engine.element.all() if e.no not in rebar and not any(
        e.no in grp and lo <= x[e.node_i] <= hi and lo <= x[e.node_j] <= hi for grp, lo, hi in spans))
'''
_SELECT = '    post.select_elements("All")'


def skip_bare(text: str) -> str:
    """Make a generated post module leave out the elements with neither rebar nor tendons."""
    if text.count(_SELECT) != 1 or "\ndef build_post" not in text:
        raise ValueError("post/main.py layout changed: select_elements('All') / build_post not found once")
    text = text.replace("\ndef build_post", _BARE.rstrip("\n") + "\n\n\ndef build_post", 1)
    return text.replace(_SELECT, _SELECT + '\n    bare = bare_elements(engine)\n    if bare:\n'
                        '        post.select_elements("U", *bare)', 1)


def skill_script(skills: Path) -> Path:
    return skills / "osis-check" / "scripts" / "gen_post.py"


def generate(name: str, folder: Path, skills: Path, osis_cfg: dict) -> str:
    """Rebuild ``folder``'s template in OSIS and write ``folder/post/main.py``."""
    bridge = folder.parents[2].name  # skills/<bridge>/references/templates/<name>
    construct = "Pre" if bridge in PRECAST else "Cast"
    with tempfile.TemporaryDirectory(dir=osis.work_dir(osis_cfg), ignore_cleanup_errors=True) as tmp:
        py = Path(tmp) / "project" / "py"
        shutil.copytree(folder / "prep", py / "prep")
        (py / "_gen_post.py").write_text(_RUN.format(construct=construct, name=name, script=str(skill_script(skills))),
                                         encoding="utf-8")
        with osis.slot(osis_cfg, bridge):
            result = osis.run_script(py / "_gen_post.py", osis_cfg["broker_url"], 3600)
        if not result.get("ok"):
            exc = result.get("exception") or result.get("error") or {}
            return f"FAIL {name}: {exc.get('type', '')}: {exc.get('message', '')}"[:600]
        (folder / "post").mkdir(exist_ok=True)
        text = skip_bare((py / "post" / "main.py").read_text(encoding="utf-8"))
        (folder / "post" / "main.py").write_text(text, encoding="utf-8")
    return f"ok   {name} ({construct})"


def main(argv: list[str]) -> int:
    config = load_config()
    skills = resolve(config["parent_repo"]) / "skills"
    names = templates(skills)
    failed = 0
    with ThreadPoolExecutor(max_workers=int(config.get("jobs") or 1)) as pool:
        for line in pool.map(lambda n: generate(n, names[n], skills, config["osis"]), argv or sorted(names)):
            print(line, flush=True)
            failed += line.startswith("FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
