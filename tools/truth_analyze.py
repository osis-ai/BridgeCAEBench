"""Fill ``truth`` of analyze cases: rebuild + solve each start template in OSIS, dump the results the probes
need, then evaluate the probes locally.

    uv run python tools/truth_analyze.py            # every analyze case with truth: null (check questions also run post)
    uv run python tools/truth_analyze.py --reuse    # reuse datasets/analyze/results/<template>.json if present
    uv run python tools/truth_analyze.py hollow     # only cases whose id contains "hollow"
    uv run python tools/truth_analyze.py --redo     # also recompute filled truths (e.g. after a pyosis upgrade)

The raw dump (nodes, elements, the matched result rows, SI units) is kept in ``datasets/analyze/results/`` so a
truth can be re-derived or audited without OSIS. A case gets its truth and ``status: verified`` only when each
probe's load case matches exactly one result name. Probe semantics: see ``tools/draft_analyze.py``.
"""

from __future__ import annotations

import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import osis  # noqa: E402
from bench.config import load_config, resolve  # noqa: E402
from scoring import cases  # noqa: E402
from scoring.extract import extract  # noqa: E402
from tools.verify_case import _set  # noqa: E402

RESULTS = cases.DATASETS / "analyze" / "results"
# Runs in the solver after the solve; REQUESTS = {key: [call, load case, envelope kind]}.
DUMP = '''import json
from pathlib import Path
from pyosis import OSISEngine

REQUESTS = json.loads({requests!r})
engine = OSISEngine()
r = engine.result
out = {{"nodes": [[n.no, n.x, n.y, n.z] for n in engine.node.all()],
       "elements": [[e.no, str(e.element_type), e.node_i, e.node_j] for e in engine.element.all()], "results": {{}}}}
for key, (call, name, kind) in REQUESTS.items():
    df = r.envelope(None, kind) if call == "envelope" else getattr(r, call)(None)
    names = sorted(set(df["loadCase"]))
    hit = [n for n in names if n == name] or [n for n in names if n.startswith(name + "_")]
    out["results"][key] = {{"names": hit, "rows": df[df["loadCase"].isin(hit)].to_dict("records")}}
Path(__file__).resolve().parents[1].joinpath("results.json").write_text(
    json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
print("dumped", {{k: v["names"] for k, v in out["results"].items()}})
'''
SCALE = {**dict.fromkeys(("fx", "fy", "fz", "mx", "my", "mz"), 1e-3), **dict.fromkeys(("ux", "uy", "uz"), 1e3)}
# Check questions: run the template's post/main.py after the solve, keep the rows of the asked item's tables.
CHECK_DUMP = '''import json
import runpy
from pathlib import Path
from pyosis import OSISEngine

ITEM = {item!r}
runpy.run_path(str(Path(__file__).parent / "post" / "main.py"), run_name="__main__")
tables = OSISEngine().result.check_all()
out = {{"tables": {{name: df.to_dict("records") for name, df in tables.items() if name.split("_")[1:2] == [ITEM]}}}}
Path(__file__).resolve().parents[1].joinpath("check.json").write_text(
    json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
print("dumped", list(out["tables"]))
'''


def _key(probe: dict) -> str:
    return f"{probe['call']}|{probe.get('kind', '')}|{probe['load_case']}"


def _probes(probe: dict) -> list[dict]:
    return [probe] + ([probe["minus"]] if probe.get("minus") else [])


def _girder(data: dict) -> tuple[set[int], set[int]]:
    """Main-girder elements and their nodes: beams that run along the bridge (piers are near vertical)."""
    xyz = {n[0]: n[1:] for n in data["nodes"]}
    elements = {e[0]: (e[2], e[3]) for e in data["elements"]
                if abs(xyz[e[2]][2] - xyz[e[3]][2]) <= 0.2 * abs(xyz[e[2]][0] - xyz[e[3]][0])}
    return set(elements), {n for pair in elements.values() for n in pair}


def evaluate(probe: dict, data: dict, spans: list[float]) -> tuple[float, list[int]]:
    """(value in kN / kN·m / mm, elements where an element extreme occurs) of one probe."""
    result = data["results"][_key(probe)]
    if len(result["names"]) != 1:
        raise ValueError(f"{probe['load_case']!r} matches result names {result['names']}")
    xyz = {n[0]: n[1:] for n in data["nodes"]}
    rows, column, where = result["rows"], probe["column"], probe.get("where") or {}
    if probe.get("component"):
        rows = [r for r in rows if r["component"] == probe["component"]]
    girder, girder_nodes = _girder(data)
    if where.get("girder"):
        rows = [r for r in rows if r["ele"] in girder]
    if where.get("support") == "first":
        x0 = min(xyz[r["no"]][0] for r in rows)
        rows = [r for r in rows if xyz[r["no"]][0] <= x0 + 1.0]
        return sum(r[column] for r in rows) * SCALE[column], []
    if "span_mid" in where:
        k = where["span_mid"]
        x = sum(spans[:k]) + spans[k] / 2
        node = min(girder_nodes, key=lambda n: abs(xyz[n][0] - x))
        rows = [r for r in rows if r["no"] == node]
    values = [r[column] for r in rows]
    if not values:
        raise ValueError(f"probe {probe} selects no rows")
    best = {"max": max, "min": min}[probe["agg"]](values) if probe.get("agg") else values[0]
    elements = sorted({r["ele"] for r in rows if "ele" in r and abs(r[column] - best) <= 1e-6 * abs(best)})
    return best * SCALE[column], elements


def truth_check(probe: dict, data: dict) -> dict:
    """ok / extreme of ``column`` / its elements over every table of the item; ok must agree with 结果."""
    rows = [r for table in data["tables"].values() for r in table]
    if not rows:
        raise ValueError(f"no {probe['item']} table in the code checks")
    column, element = probe["column"], probe["answer"]["element"]
    best = {"max": max, "min": min}[probe["agg"]](float(r[column]) for r in rows)
    elements = sorted({int(r[element]) for r in rows if abs(float(r[column]) - best) <= 1e-6 * abs(best)})
    ok = "是" if eval(probe["answer"]["ok"], {"__builtins__": {}}, {"value": best}) else "否"
    if (ok == "是") != all(str(r.get("结果")) == "OK" for r in rows):
        raise ValueError(f"{probe['answer']['ok']} gives {ok} but 结果 says otherwise")
    return {"ok": ok, "value": round(best, 3), "element": elements}


def _dump_check(case: dict, skills: Path, osis_cfg: dict) -> dict:
    with tempfile.TemporaryDirectory(dir=osis.work_dir(osis_cfg), ignore_cleanup_errors=True) as tmp:
        py = cases.materialize(case, "start", skills, Path(tmp) / "project" / "py")
        with osis.slot(osis_cfg, case["bridge"]):
            error, _ = osis.rebuild_and_solve(py, osis_cfg["broker_url"], osis_cfg["solve_timeout_s"])
            if error:
                raise RuntimeError(f"{case['id']}: {error}")
            (py / "_bench_check_dump.py").write_text(CHECK_DUMP.format(item=case["probe"]["item"]), encoding="utf-8")
            result = osis.run_script(py / "_bench_check_dump.py", osis_cfg["broker_url"], osis_cfg["solve_timeout_s"])
        if not result.get("ok"):
            raise RuntimeError(f"{case['id']}: check dump failed: {result.get('exception') or result.get('error')}")
        return json.loads((py.parent / "check.json").read_text(encoding="utf-8"))


def fill_check(case: dict, skills: Path, osis_cfg: dict, reuse: bool) -> int:
    """Truth of one check question; returns 1 if it failed (-1 when the Broker itself failed)."""
    saved = RESULTS / f"{case['start'].split(':', 1)[1]}.check.json"
    try:
        if reuse and saved.is_file():
            data = json.loads(saved.read_text(encoding="utf-8"))
        else:
            data = _dump_check(case, skills, osis_cfg)
            data["pyosis"] = osis.pyosis_version(osis_cfg["python"])
            saved.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        value = truth_check(case["probe"], data)
    except osis.BrokerError as exc:
        print(f"STOP {case['id']}: {exc}", flush=True)
        return -1
    except (RuntimeError, ValueError) as exc:
        print(f"FAIL {case['id']}: {exc}", flush=True)
        return 1
    path = cases.DATASETS / "analyze" / f"{case['id']}.yaml"
    old = case.get("truth")
    if value != old:
        _set(path, "truth", json.dumps(old, ensure_ascii=False) if old else "null", json.dumps(value, ensure_ascii=False))
    if case["status"] == "draft":
        _set(path, "status", "draft", "verified")
    print(f"ok   {case['id']}: {value}", flush=True)
    return 0


def truth(case: dict, data: dict, spans: list[float]) -> dict:
    probe = case["probe"]
    value, elements = evaluate(probe, data, spans)
    if probe.get("minus"):
        value -= evaluate(probe["minus"], data, spans)[0]
    out: dict = {}
    for key, unit in probe["answer"].items():
        if unit in ("kN", "kN·m", "mm"):
            out[key] = round(value, 3)
        elif unit == "mm_down":
            out[key] = round(-value, 3)
        elif unit == "ele":
            out[key] = elements
        else:  # a formula on the reply value, from our own case file
            out[key] = "是" if eval(unit, {"__builtins__": {}}, {"value": out["value"], "abs": abs}) else "否"
    return out


def _dump(template: str, group: list[dict], skills: Path, osis_cfg: dict) -> dict:
    requests = {_key(p): [p["call"], p["load_case"], p.get("kind", "")] for c in group for p in _probes(c["probe"])}
    with tempfile.TemporaryDirectory(dir=osis.work_dir(osis_cfg), ignore_cleanup_errors=True) as tmp:
        py = cases.materialize(group[0], "start", skills, Path(tmp) / "project" / "py")
        with osis.slot(osis_cfg, group[0]["bridge"]):
            error, _ = osis.rebuild_and_solve(py, osis_cfg["broker_url"], osis_cfg["solve_timeout_s"])
        if error:
            raise RuntimeError(f"{template}: {error}")
        (py / "_bench_dump.py").write_text(DUMP.format(requests=json.dumps(requests, ensure_ascii=False)),
                                           encoding="utf-8")
        result = osis.run_script(py / "_bench_dump.py", osis_cfg["broker_url"], osis_cfg["solve_timeout_s"])
        if not result.get("ok"):
            raise RuntimeError(f"{template}: dump failed: {result.get('exception') or result.get('error')}")
        return json.loads((py.parent / "results.json").read_text(encoding="utf-8"))


def fill(template: str, group: list[dict], skills: Path, osis_cfg: dict, reuse: bool) -> int:
    """Truths for one template's cases; returns how many failed (-1 when the Broker itself failed)."""
    saved = RESULTS / f"{template}.json"
    try:
        if reuse and saved.is_file():
            data = json.loads(saved.read_text(encoding="utf-8"))
        else:
            data = _dump(template, group, skills, osis_cfg)
            data["pyosis"] = osis.pyosis_version(osis_cfg["python"])
            saved.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except osis.BrokerError as exc:  # infrastructure, not a case problem
        print(f"STOP {template}: {exc}", flush=True)
        return -1
    except RuntimeError as exc:
        print(f"FAIL {template}: {exc}", flush=True)
        return len(group)
    spans = extract(cases.templates(skills)[template])["span_lengths"]
    bad = 0
    for case in group:
        try:
            value = truth(case, data, spans)
        except ValueError as exc:
            bad += 1
            print(f"FAIL {case['id']}: {exc}", flush=True)
            continue
        path = cases.DATASETS / "analyze" / f"{case['id']}.yaml"
        old = case.get("truth")
        if value != old:  # first fill, or a redo that changed the value
            _set(path, "truth", json.dumps(old, ensure_ascii=False) if old else "null",
                 json.dumps(value, ensure_ascii=False))
            if old:
                print(f"CHANGED {case['id']}: was {old}", flush=True)
        if case["status"] == "draft":
            _set(path, "status", "draft", "verified")
        print(f"ok   {case['id']}: {value}", flush=True)
    return bad


def main(argv: list[str]) -> int:
    config = load_config()
    skills = resolve(config["parent_repo"]) / "skills"
    only = [a for a in argv if not a.startswith("--")]  # optional id substrings, e.g. "hollow"
    todo = [c for c in cases.load_line("analyze") if (c.get("truth") is None or "--redo" in argv)
            and (not only or any(o in c["id"] for o in only))]
    checks = [c for c in todo if c["kind"] == "check"]
    groups: dict[str, list[dict]] = {}
    for case in todo:
        if case["kind"] != "check":
            groups.setdefault(case["start"].split(":", 1)[1], []).append(case)
    RESULTS.mkdir(exist_ok=True)
    reuse = "--reuse" in argv
    with ThreadPoolExecutor(max_workers=int(config.get("jobs") or 1)) as pool:
        bad = list(pool.map(lambda tg: fill(*tg, skills, config["osis"], reuse), groups.items()))
        bad_checks = list(pool.map(lambda c: fill_check(c, skills, config["osis"], reuse), checks))
    failed = sum(len(g) if b < 0 else b for b, g in zip(bad, groups.values())) + sum(abs(b) for b in bad_checks)
    print(f"{len(todo) - failed}/{len(todo)} truths filled")
    return 2 if -1 in bad + bad_checks else 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
