"""Rebase diagnose check_ng start overlays (migrated full copies of older templates) onto the current templates.

    uv run python tools/rebase_overlays.py            # dry run: print what each seed file keeps
    uv run python tools/rebase_overlays.py --write    # rewrite the overlays

The overlay keeps only the case's seed file (``seed.file``): the current template's file with the overlay's
differing lines applied, except lines that differ only by out-of-date pyosis spelling (old creep/shrinkage
call, upper-case rebar type, 3-argument stress point, material creep/shrinkage number). Other files come from
the template. A block that cannot be paired line by line is reported and the case is left unchanged.
"""

from __future__ import annotations

import difflib
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.config import load_config, resolve  # noqa: E402
from scoring import cases  # noqa: E402


def _args(line: str) -> list[str]:
    return [a.strip() for a in line[line.find("(") + 1:line.rfind(")")].split(",")]


def _old(line: str) -> bool:
    """A line of a block whose spelling changed between pyosis versions (taken from the template instead)."""
    return any(k in line for k in ("creep_shrink", "CrpShrk", "batch_state", "set_stress_point", "收缩徐变特性",
                                   "6 字段 CrpShrk"))


def stale(template: str, overlay: str) -> bool:
    """The two lines differ only by an out-of-date spelling, not by a planted change."""
    if "creep_shrink" in template or "CrpShrk" in overlay or "set_stress_point" in template:
        return True
    if "add_rebar_s" in template and template.lower() == overlay.lower():
        return True
    if "material.create" in template:
        a, b = _args(template), _args(overlay)
        return len(a) == len(b) and [x for k, x in enumerate(a) if k != 5] == [x for k, x in enumerate(b) if k != 5]
    return False


def _key(line: str) -> str:
    """What a line defines (call + first argument): changed lines are paired by this, not by position."""
    return line.split(",")[0].strip().lower()  # lower: rebar type names changed case


def rebase(template: list[str], overlay: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(new seed file lines, kept planted changes, problems)."""
    out, kept, problems = [], [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=template, b=overlay, autojunk=False).get_opcodes():
        if op == "equal":
            out += template[i1:i2]
        elif op == "replace" and i2 - i1 > 0 and j2 - j1 > 0:  # re-align the block by what each line defines
            t_block, o_block = template[i1:i2], overlay[j1:j2]
            inner = difflib.SequenceMatcher(a=[_key(t) for t in t_block], b=[_key(o) for o in o_block],
                                            autojunk=False)
            for op2, a1, a2, b1, b2 in inner.get_opcodes():
                if op2 == "equal":
                    for t, o in zip(t_block[a1:a2], o_block[b1:b2]):
                        planted = t != o and not stale(t, o)
                        out.append(o if planted else t)
                        if planted:
                            kept.append(f"- {t.strip()}\n      + {o.strip()}")
                else:
                    _unpaired(t_block[a1:a2], o_block[b1:b2], out, kept, problems, f"{i1 + a1 + 1}", f"{j1 + b1 + 1}")
        else:
            _unpaired(template[i1:i2], overlay[j1:j2], out, kept, problems, f"{i1 + 1}", f"{j1 + 1}")
    return out, kept, problems


def _unpaired(t_lines: list[str], o_lines: list[str], out: list[str], kept: list[str], problems: list[str],
              at_t: str, at_o: str) -> None:
    """Lines only one side defines: fine if the template side is out-of-date spellings or blank; the overlay's
    remaining lines are planted additions. Anything else would drop template lines, so it is reported."""
    if [t for t in t_lines if t.strip() and not _old(t)]:
        problems.append(f"unpaired template line {at_t} / overlay line {at_o}: "
                        f"{[t.strip()[:80] for t in t_lines]} -> {[o.strip()[:80] for o in o_lines]}")
        return
    new = [o for o in o_lines if o.strip() and not _old(o)]
    out += t_lines + new
    kept += [f"+ {o.strip()}" for o in new]


def main(argv: list[str]) -> int:
    skills = resolve(load_config()["parent_repo"]) / "skills"
    names = cases.templates(skills)
    bad = 0
    for case in cases.load_line("diagnose"):
        if case.get("kind") != "check_ng":
            continue
        seed = case["seed"]["file"]
        start = case["_dir"] / "start"
        template = (names[case["base"]] / seed).read_text(encoding="utf-8")
        overlay = (start / seed).read_text(encoding="utf-8")
        lines, kept, problems = rebase(template.splitlines(), overlay.splitlines())
        print(f"{'FAIL' if problems or not kept else 'ok  '} {case['id']} {seed}: {case['seed']['how']}")
        for k in kept[:6]:
            print(f"      {k}")
        if len(kept) > 6:
            print(f"      ... {len(kept)} planted lines")
        for p in problems or ([] if kept else ["no planted change left"]):
            print(f"      ! {p}")
        if problems or not kept:
            bad += 1
            continue
        if "--write" in argv:
            shutil.rmtree(start / "prep")  # keep the case files beside prep (项目画像.md, reason_codes.yaml)
            (start / seed).parent.mkdir(parents=True, exist_ok=True)
            (start / seed).write_text("\n".join(lines) + ("\n" if template.endswith("\n") else ""), encoding="utf-8")
    print(f"{bad} cases need a look" if bad else "all cases rebased" if "--write" in argv else "dry run ok")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
