"""Draft analyze cases: questions about the results of a template the agent solves itself (five kinds per bridge).

    uv run python tools/draft_analyze.py          # writes datasets/analyze/analyze-*.yaml (skips existing)

``probe`` says how the truth is read with ``engine.result`` after the template is rebuilt and solved
(the check question also runs post/main.py). ``tools/truth_analyze.py`` fills ``truth``.
Probe keys: ``call`` (node_displacement / element_force / support_reaction / envelope / check_all), ``kind``
(envelope result type), ``load_case`` (a construction-stage load is reported as ``<name>_<stage>``: matched by
that prefix), ``component`` (envelope row, e.g. ``Uz-Min``), ``column`` (DataFrame column), ``agg`` (max / min),
``where`` (``girder`` = main-girder elements, ``support: first`` = all supports at the smallest x, summed,
``span_mid: k`` = the girder node nearest the nominal middle of span k, spans laid from x = 0), ``minus``
(a second probe subtracted from the first), ``item`` (check item for check_all), ``answer`` (reply key -> unit:
kN, kN·m, mm, ``mm_down`` = downward positive, ``ele`` = element numbers, or a formula on ``value``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scoring import cases  # noqa: E402
from tools.draft_build import _Dumper  # noqa: E402

GRADIENT = ("正温度梯度", "负温度梯度")
# short -> (bridge, template, main span index, main span m, pavement case, live case, gradient cases)
PLAN = {
    "cantilever": ("osis-bridge-cantilever-box", "变截面悬浇连续梁-70+130+70", 1, 130, "铺装右", "中载工况", GRADIENT),
    "cipbox": ("osis-bridge-conventional-box", "现浇等截面箱梁-30+35+22-单箱三室-16.75", 1, 35, "二期_二期", "公路一级",
               ("梯度升温_梯度升温", "梯度降温_梯度降温")),
    "hollow": ("osis-bridge-hollow-slab", "预制简支-空心板-10m-边梁", 0, 10, "铺装工况", "车道荷载包络", GRADIENT),
    "rigid": ("osis-bridge-rigid-frame-box", "变截面连续刚构-95+180+95-双肢", 1, 180, "铺装左", "中载工况", GRADIENT),
    "smallbox": ("osis-bridge-precast-small-box", "预制简支-小箱梁-30m-边梁", 0, 30, "铺装工况", "车道荷载包络", GRADIENT),
    "tgirder": ("osis-bridge-precast-t-girder", "简支变连续-T梁-25m-中梁", 1, 25, "铺装工况", "车道荷载包络", GRADIENT),
}
OPENING = "请先重建并求解这个模型，再从结果里查：\n"
OPENING_CHECK = "请先重建并求解这个模型，再运行 py/post/main.py 做组合与验算，然后从结果里查：\n"


def _questions(main: int, span: float, pavement: str, live: str, gradient: tuple[str, str]
               ) -> list[tuple[str, str, dict, dict]]:
    """(kind, question + reply format, probe, tolerance) for one bridge."""
    where = "主跨" if main else ""
    return [
        ("extreme", f"成桥运营阶段，“{pavement}”工况作用下主梁的最大正弯矩（单元局部 My）是多少 kN·m，出现在哪个单元？\n"
                    '回复 JSON：{"value": 数值, "element": 单元号}',
         {"call": "element_force", "load_case": pavement, "column": "my", "agg": "max", "where": {"girder": True},
          "answer": {"value": "kN·m", "element": "ele"}}, {"value": 0.02}),
        ("reaction", f"“{pavement}”工况下，0 号台（桥梁起点）支座的竖向反力合计是多少 kN？\n" '回复 JSON：{"value": 数值}',
         {"call": "support_reaction", "load_case": pavement, "column": "fz", "where": {"support": "first"},
          "answer": {"value": "kN"}}, {"value": 0.02}),
        ("displacement", f"“{live}”作用下{where}跨中的最大竖向位移（向下）是多少 mm？按 L/600 判断是否满足（L={span}m）。\n"
                         '回复 JSON：{"value": 数值, "ok": "是" 或 "否"}',
         {"call": "envelope", "kind": "displacement", "load_case": live, "component": "Uz-Min", "column": "uz",
          "agg": "min", "where": {"span_mid": main},
          "answer": {"value": "mm_down", "ok": f"abs(value) <= {span} * 1000 / 600"}}, {"value": 0.02}),
        ("check", "使用阶段正截面压应力验算是否全部通过？最小安全系数是多少，在哪个单元？\n"
                  '回复 JSON：{"ok": "是" 或 "否", "value": 最小安全系数, "element": 单元号}',
         {"call": "check_all", "item": "PC使用阶段正截面压应力验算", "column": "安全系数", "agg": "min",
          "answer": {"ok": "value >= 1.0", "value": "", "element": "单元"}}, {"value": 0.02}),
        ("compare", f"“{gradient[0]}”和“{gradient[1]}”两个工况下，{where}跨中的竖向位移相差多少 mm"
                    "（前者减后者，向上为正）？\n" '回复 JSON：{"value": 数值}',
         {"call": "node_displacement", "load_case": gradient[0], "column": "uz", "where": {"span_mid": main},
          "minus": {"call": "node_displacement", "load_case": gradient[1], "column": "uz",
                    "where": {"span_mid": main}},
          "answer": {"value": "mm"}}, {"value": 0.02}),
    ]


def main() -> int:
    out = cases.DATASETS / "analyze"
    out.mkdir(exist_ok=True)
    written = 0
    for short, (bridge, template, main_span, span, pavement, live, gradient) in PLAN.items():
        for number, (kind, question, probe, tolerance) in enumerate(
                _questions(main_span, span, pavement, live, gradient), 1):
            cid = f"analyze-{short}-{number:03d}"
            if (out / f"{cid}.yaml").exists():
                continue
            case = {"id": cid, "line": "analyze", "bridge": bridge, "status": "draft", "version": 1, "kind": kind,
                    "start": f"template:{template}",
                    "brief": (OPENING_CHECK if kind == "check" else OPENING) + question + "\n", "checks": [], "probe": probe, "tolerance": tolerance,
                    "truth": None, "notes": f"结果问答：模板 {template}。truth 待在 OSIS 中按 probe 取值。"}
            (out / f"{cid}.yaml").write_text(yaml.dump(case, Dumper=_Dumper, allow_unicode=True, sort_keys=False,
                                                       width=200), encoding="utf-8")
            written += 1
    print(f"wrote {written} cases to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
