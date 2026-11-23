"""The osis-check skill's scripts/gen_post.py reproduces OSIS 5.1's GUI auto-check combinations as runnable pyosis code."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from bench.config import load_config, resolve
from tools.gen_post import skill_script

ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "tests" / "data" / "OSISPost-预制简支-空心板-10m-中梁-5.1.out"
# 10m 空心板模板（cr-002 底模型）在 OSIS 里读出的工况，按模型顺序
HOLLOW = {"loads": [["防撞护栏工况", "CS"], ["铺装工况", "CS"], ["封端混凝土工况", "CS"], ["负温度梯度", "TG"],
                    ["整体降温", "T"], ["预应力", "CS"], ["正温度梯度", "TG"], ["整体升温", "T"], ["主梁单元自重", "CS"]],
          "lives": [["车道荷载包络", 15.6377]], "settles": ["沉降工况"]}


class _Batch:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


@pytest.fixture
def gen_post(monkeypatch):
    script = skill_script(resolve(load_config()["parent_repo"]) / "skills")
    if not script.is_file():
        pytest.skip(f"skill script not found: {script}")
    monkeypatch.setitem(sys.modules, "pyosis", types.SimpleNamespace(OSISEngine=object, batch=_Batch))
    spec = importlib.util.spec_from_file_location("gen_post_skill", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hollow_slab_matches_gui(gen_post):
    lines = ["combinedel,all; clc;", ""]
    for name, kind, op, prompt, members in gen_post.combinations(**HOLLOW):
        kind = {"Quasipermanent": "QuasiPermanent"}.get(kind, kind)
        lines.append(f"combine,{name},env,Concrete,{kind},{op},{prompt};")
        lines += [f"post,combine,{name},{m},{f:.2f}" for m, f in members] + [""]  # OSISPost.out prints 2 decimals
    lines += ["CombineSolve"] + [f"Check,{item},{c}" for item, c in gen_post.CHECKS[:-1]]  # the last is not printed
    assert lines == [line.strip() for line in GUI.read_text(encoding="utf-8").splitlines()[4:]]


def test_rendered_module_makes_the_same_calls(gen_post):
    calls = []

    class Post:
        def __getattr__(self, method):
            return lambda *args: calls.append((method, *args))

    scope = {}
    exec(gen_post.render("预制简支-空心板-10m-中梁", HOLLOW, "Pre"), scope)
    scope["build_post"](types.SimpleNamespace(post=Post()))
    combos = gen_post.combinations(**HOLLOW)
    assert [c[1:] for c in calls if c[0] == "create_combine"] == [(n, "Env", "Concrete", k, o, p) for n, k, o, p, _ in combos]
    assert [c[1:] for c in calls if c[0] == "add_to_combine"] == [(n, m, f) for n, *_, ms in combos for m, f in ms]
    assert ("add_to_combine", "频遇组合30", "车道荷载包络", 0.483) in calls  # 0.7 / (1 + 0.45), three decimals
    assert [c[1:] for c in calls if c[0] == "add_check"] == list(gen_post.CHECKS)
    assert calls[-4:] == [("select_elements", "All"), ("set_member_type", "APC", "Pre", "Post"),
                          ("set_check_items", "All"), ("solve_checks",)]


def test_impact_factor(gen_post):
    assert gen_post.impact(0.85) == 0.05 and gen_post.impact(20) == 0.45
    assert round(0.7 / (1 + gen_post.impact(3.89845)), 2) == 0.57  # 小箱梁 30m，旧 OSISPost.out 的频遇系数


def test_skip_bare_leaves_out_elements_without_rebar_or_tendons(gen_post):
    from tools.gen_post import skip_bare

    calls = []

    class Post:
        def __getattr__(self, method):
            return lambda *args: calls.append((method, *args))

    ns = types.SimpleNamespace
    # 1-2 配筋；3 在钢束组里、两端都在钢束范围内；4 不在钢束组；5 在钢束组但 j 端超出锚固点
    engine = ns(post=Post(),
                section=ns(all=lambda: [ns(rebar=ns(longitudinal_rebars_count=4), related_elements=[1, 2]),
                                        ns(rebar=ns(longitudinal_rebars_count=0), related_elements=[3, 4, 5])]),
                node=ns(all=lambda: [ns(no=n, x=float(n)) for n in range(1, 7)]),
                tendon=ns(shape=ns(all=lambda: [ns(ele_grp="束组", points=[{"x": 2.0}, {"x": 5.5}])])),
                element=ns(all=lambda: [ns(no=n, node_i=n, node_j=n + 1) for n in range(1, 6)],
                           group=ns(all=lambda: [ns(name="束组", elements=[3, 5]), ns(name="墩", elements=[4])])))
    scope = {}
    exec(skip_bare(gen_post.render("变截面连续刚构-95+180+95-双肢", HOLLOW, "Cast")), scope)
    scope["build_post"](engine)
    i = calls.index(("select_elements", "All"))
    assert calls[i + 1] == ("select_elements", "U", 4, 5)
