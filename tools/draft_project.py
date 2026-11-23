"""Draft project cases: one session of four turns on one template (build -> change A -> change B -> review).

    uv run python tools/draft_project.py          # writes datasets/project/project-*.yaml (skips existing)

Turns 1-3 copy a build case and two change cases of the same template (``from`` keeps the source id);
the two changes touch different files, so the reference after turn 3 is the template with both overlays
(written to ``<id>/turn2`` and ``<id>/turn3``). Turn 3 must keep turn 2's change; turn 4 asks for a
full review against the final design: the build checks with the changed fields replaced.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scoring import cases  # noqa: E402
from tools.draft_build import _Dumper  # noqa: E402

PLAN = {  # bridge short name -> (build case, change A, change B)
    "cantilever": ("build-cantilever-003", "change-cantilever-003", "change-cantilever-005"),
    "cipbox": ("build-cipbox-009", "change-cipbox-003", "change-cipbox-004"),
    "hollow": ("build-hollow-001", "change-hollow-004", "change-hollow-005"),
    "rigid": ("build-rigid-001", "change-rigid-003", "change-rigid-004"),
    "smallbox": ("build-smallbox-006", "change-smallbox-003", "change-smallbox-002"),
    "tgirder": ("build-tgirder-006", "change-tgirder-002", "change-tgirder-005"),
}
REVIEW = ("项目要交了。按最终的设计参数把整个模型完整复核一遍：参数以第一次交代的为准，之后两次变更都要体现。"
          "发现和设计参数不一致的地方逐项改正，改完重建并求解确认能算通。\n"
          "回复里给出复核结论 JSON，没有不一致就给空列表：\n"
          '{"inconsistencies":[{"item":"混凝土等级","model":"模型里的值","design":"设计值"}]}\n')


def _load(cid: str) -> dict:
    return cases.load(cases.DATASETS / cid.split("-")[0] / f"{cid}.yaml")


def _merge(base: list[dict], *changes: list[dict]) -> list[dict]:
    """Checks after the changes: base checks on changed fields are replaced by the changes' checks."""
    changed = {c["field"] for checks in changes for c in checks}
    return [c for c in base if c["field"] not in changed] + [c for checks in changes for c in checks]


def _overlay(case: dict, dest: Path) -> None:
    source = case["_dir"] / case["reference"].split(":", 1)[1]
    for path in (p for p in source.rglob("*") if p.is_file()):
        target = dest / path.relative_to(source)
        if target.exists():
            raise ValueError(f"{case['id']} and the earlier change both edit {target.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def main() -> int:
    out = cases.DATASETS / "project"
    out.mkdir(exist_ok=True)
    written = 0
    for short, (bid, aid, cid_b) in PLAN.items():
        pid = f"project-{short}-001"
        if (out / f"{pid}.yaml").exists():
            continue
        build, a, b = _load(bid), _load(aid), _load(cid_b)
        template = build["reference"].split(":", 1)[1]
        if not (a["base"] == b["base"] == template):
            raise ValueError(f"{pid}: {aid}/{cid_b} are not on template {template}")
        _overlay(a, out / pid / "turn2")
        _overlay(a, out / pid / "turn3")
        _overlay(b, out / pid / "turn3")
        turns = [
            {"from": bid, "brief": build["brief"], "checks": build["checks"], "reference": build["reference"]},
            {"from": aid, "brief": a["brief"], "checks": a["checks"], "reference": "overlay:turn2",
             "preserve": True, "may_change": a.get("may_change") or []},
            {"from": cid_b, "brief": b["brief"], "checks": _merge(a["checks"], b["checks"]),
             "reference": "overlay:turn3", "preserve": True, "may_change": b.get("may_change") or []},
            {"brief": REVIEW, "checks": _merge(build["checks"], a["checks"], b["checks"]),
             "reference": "overlay:turn3", "preserve": True, "may_change": []},
        ]
        case = {"id": pid, "line": "project", "bridge": build["bridge"], "status": "draft", "version": 1,
                "start": "empty", "base": template,
                "hide_templates": sorted({t for c in (build, a, b) for t in c.get("hide_templates") or []}),
                "hidden": build.get("hidden") or {}, "hidden_profile": build.get("hidden_profile", False),
                "turns": turns,
                "notes": f"同一会话四轮：{bid} → {aid} → {cid_b} → 按最终参数复核。每轮在上一轮自己的产出上继续。"}
        (out / f"{pid}.yaml").write_text(yaml.dump(case, Dumper=_Dumper, allow_unicode=True, sort_keys=False,
                                                   width=200), encoding="utf-8")
        written += 1
    print(f"wrote {written} cases to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
