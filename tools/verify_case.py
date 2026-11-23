"""Static verification of case files: the reference meets every check, and changes nothing it should not.

    uv run python tools/verify_case.py build            # every case of a line
    uv run python tools/verify_case.py build/build-tgirder-001 change/...
    uv run python tools/verify_case.py --solve build   # also rebuild + solve each reference in OSIS (Broker)
    uv run python tools/verify_case.py --solve --jobs 1 build/build-rigid-003   # override config jobs

For each case: materialize ``reference`` (default ``start`` for build: the template), run the checks; for
cases with ``preserve`` also compare start and reference. Prints one line per case and exits 1 on any
failure. ``--solve`` then rebuilds and solves every reference in OSIS (projects with identical files are solved
once, also across pyosis upgrades), logs each result to ``datasets/solve_log.yaml`` and flips a passing case to ``verified``. Run-error
cases also rebuild their start, which must fail; the error text becomes ``symptom``. Check-NG cases also run
post/main.py on reference and start: the target item must be NG only in the start, and every NG item of the
reference must also be NG in the start (repair allows no NG outside ``ng_seeded``); ``ng_baseline`` / ``ng_seeded`` are rewritten to the
measured NG items. Cases without a reference are logged but stay ``draft``.
A project case is verified turn by turn: each turn's reference against its checks, preserve against the previous
turn's reference, and every turn copied ``from`` a case must still carry that case's brief.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import osis  # noqa: E402
from bench.config import load_config, resolve  # noqa: E402
from scoring import cases  # noqa: E402
from scoring.checks import preserve, score_checks  # noqa: E402
from scoring.extract import extract  # noqa: E402


def verify(case: dict, skills: Path) -> list[str]:
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        reference = cases.materialize(case, "reference" if "reference" in case else "start", skills, Path(tmp) / "ref")
        facts = extract(reference)
        result = score_checks(facts, case["checks"])
        problems += [f"check {r['field']} {r['op']} {r.get('value')!r}: got {r['actual']!r}"
                     for r in result["results"] if not r["ok"]]
        if case.get("preserve") and case["start"] != "empty":
            start = extract(cases.materialize(case, "start", skills, Path(tmp) / "start"))
            exclude = {c["field"] for c in case["checks"]} | set(case.get("may_change") or [])  # same as scoring
            kept = preserve(start, facts, exclude)
            if kept["changed"]:
                problems.append(f"reference changes undeclared fields {kept['changed']} (add to may_change?)")
    return problems


def verify_project(case: dict, skills: Path) -> list[str]:
    problems, previous = [], None
    for k, turn in enumerate(case["turns"], start=1):
        if turn.get("from"):
            source = cases.load(cases.DATASETS / turn["from"].split("-")[0] / f"{turn['from']}.yaml")
            if source["brief"] != turn["brief"]:
                problems.append(f"turn{k}: brief differs from {turn['from']}")
        start = previous or "empty"
        problems += [f"turn{k}: {p}" for p in verify({**case, **turn, "start": start}, skills)]
        previous = turn["reference"]
    return problems


LOG = cases.DATASETS / "solve_log.yaml"


def _digest(py: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in py.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(path.relative_to(py).as_posix().encode() + b"\0" + path.read_bytes())
    return h.hexdigest()[:16]


_LOCK = threading.Lock()  # guards the shared log and _BUSY
_BUSY: dict[str, threading.Lock] = {}  # one lock per project digest: identical projects are solved once
_THIS_RUN: dict[str, dict] = {}  # digest -> entry solved by this process: reused even after machine trouble
_FIELDS = ("hash", "ok", "error", "rebuild_s", "solve_s", "check_s", "ng", "pyosis")


def _reusable(entry: object, digest: str) -> bool:
    # a pyosis upgrade alone does not re-solve; delete the log entries to force it
    return (isinstance(entry, dict) and entry.get("hash") == digest
            and (entry["ok"] or not osis.transient(entry.get("error"))))  # machine trouble: retried next run


def _solve(case: dict, which: str, key: str, osis_cfg: dict, skills: Path, log: dict, check: bool = False) -> dict:
    """Rebuild + solve ``case[which]`` once per distinct project content and record it as ``log[key]``;
    ``check`` also runs the project's post/main.py (from the template) and records the NG items."""
    today, version = datetime.date.today().isoformat(), osis.pyosis_version(osis_cfg["python"])
    with tempfile.TemporaryDirectory(dir=osis.work_dir(osis_cfg), ignore_cleanup_errors=True) as tmp:
        py = cases.materialize(case, which, skills, Path(tmp) / "project" / "py")
        digest = _digest(py)
        with _LOCK:
            busy = _BUSY.setdefault(digest, threading.Lock())
        with busy:  # another worker on the same project: wait for its result
            with _LOCK:
                done = _THIS_RUN.get(digest) or next((e for e in log.values() if _reusable(e, digest)), None)
                if done:
                    log[key] = entry = {**{k: done[k] for k in _FIELDS if k in done}, "date": today}
                    return entry
            with osis.slot(osis_cfg, case["bridge"]):
                error, steps = osis.rebuild_and_solve(py, osis_cfg["broker_url"], osis_cfg["solve_timeout_s"])
                ng = None
                if check and not error:
                    error, ng, steps["check"] = osis.check(py, osis_cfg["broker_url"], osis_cfg["solve_timeout_s"])
            if error:  # strip the temporary project path so the text reads like a user's own error
                error = re.sub(r"[A-Za-z]:[\\/][^\s'\"]*?[\\/]project[\\/]py[\\/]", "", error)
            seconds = {f"{k}_s": round(v.get("execution_time_ms", 0) / 1000, 1) for k, v in steps.items()}
            with _LOCK:
                log[key] = entry = {"hash": digest, "ok": error is None, "error": error,
                                    "rebuild_s": seconds.get("rebuild_s"), "solve_s": seconds.get("solve_s"),
                                    "pyosis": version, "date": today,
                                    **({"check_s": seconds.get("check_s"), "ng": ng} if check else {})}
                _THIS_RUN[digest] = entry
            return entry


def _set(path: Path, key: str, old: str, new: str) -> None:
    """Edit one top-level ``key: old`` line in place, keeping the rest of the hand-readable file as is."""
    text = path.read_text(encoding="utf-8")
    edited, n = re.subn(rf"^{key}: {re.escape(old)}$", lambda _: f"{key}: {new}", text, count=1, flags=re.M)
    if n != 1:
        raise ValueError(f"{path}: no line '{key}: {old}'")
    path.write_text(edited, encoding="utf-8")


def _set_list(path: Path, key: str, values: list[str]) -> None:
    """Replace one top-level block list ``key:`` (or ``key: []``) in place."""
    text = path.read_text(encoding="utf-8")
    new = yaml.safe_dump({key: values}, allow_unicode=True, width=10**6)
    edited, n = re.subn(rf"^{key}:(?: \[\])?\n(?:- .*\n)*", lambda _: new, text, count=1, flags=re.M)
    if n != 1:
        raise ValueError(f"{path}: no list '{key}:'")
    path.write_text(edited, encoding="utf-8")


def solve_case(case: dict, path: Path, skills: Path, osis_cfg: dict, log: dict) -> list[str]:
    """Solve the case's reference(s); returns problems. Updates ``log``, the case's status and symptom.
    Machine trouble (``osis.transient``) gives ``RETRY`` problems and leaves status and symptom alone."""
    problems, retry = [], []
    turns = case["turns"] if case["line"] == "project" else [case]
    refs = list(dict.fromkeys(t["reference"] for t in turns if t.get("reference")))
    check = case.get("kind") == "check_ng"
    for ref in refs:
        key = case["id"] if len(refs) == 1 else f"{case['id']}:{ref}"
        entry = _solve({**case, "reference": ref}, "reference", key, osis_cfg, skills, log, check)
        if osis.transient(entry["error"]):
            retry.append(f"RETRY reference {ref}: {entry['error']}")
        elif not entry["ok"]:
            problems.append(f"reference {ref} does not solve: {entry['error']}")
    if check and not retry and not problems:
        start = _solve(case, "start", f"{case['id']}:start", osis_cfg, skills, log, check)
        if osis.transient(start["error"]):
            retry.append(f"RETRY start: {start['error']}")
        elif not start["ok"]:
            problems.append(f"start does not solve: {start['error']}")
        else:
            target = case["target_item"]
            if target in entry["ng"]:
                problems.append(f"{target} is NG in the reference")
            if target not in start["ng"]:
                problems.append(f"the seed does not make {target} NG")
            if not set(entry["ng"]) <= set(start["ng"]):  # repair allows only ng_seeded: the reference must pass
                problems.append(f"NG in the reference but not in the start: {sorted(set(entry['ng']) - set(start['ng']))}")
            for name, ng in (("ng_baseline", entry["ng"]), ("ng_seeded", start["ng"])):
                if set(ng) != set(case[name]):  # the lists follow OSIS 5.1 + the template post/main.py, whatever was recorded
                    print(f"     {case['id']}: {name} {sorted(case[name])} -> {ng}", flush=True)
                    _set_list(path, name, ng)
    if case.get("kind") == "run_error":
        entry = _solve(case, "start", f"{case['id']}:start", osis_cfg, skills, log)
        if osis.transient(entry["error"]):
            retry.append(f"RETRY start: {entry['error']}")
        elif entry["ok"]:
            problems.append("start solves: the seeded error does not break the model")
        elif case.get("symptom") is None:
            symptom = entry["error"].split(": ", 1)[1]  # drop our "rebuild: " step label
            _set(path, "symptom", "null", yaml.safe_dump(symptom, allow_unicode=True, width=10**6).strip()
                 .removesuffix("\n...").strip())
    if retry:
        return problems + retry
    blocked = not refs  # nothing to solve
    if not problems and not blocked and case["status"] == "draft":
        _set(path, "status", "draft", "verified")
    elif problems and case["status"] == "verified":  # e.g. broke under a newer pyosis
        _set(path, "status", "verified", "draft")
    return problems


def _report(case_id: str, problems: list[str]) -> None:
    label = "ok   " if not problems else "RETRY" if all(p.startswith("RETRY") for p in problems) else "FAIL "
    print(label + " " * (label == "RETRY") + case_id + "".join(f"\n     - {p}" for p in problems), flush=True)


def main(argv: list[str]) -> int:
    config = load_config()
    skills = resolve(config["parent_repo"]) / "skills"
    solving = "--solve" in argv
    argv = [a for a in argv if a != "--solve"]
    jobs = int(config.get("jobs") or 1)
    if "--jobs" in argv:
        k = argv.index("--jobs")
        jobs = int(argv[k + 1])
        del argv[k:k + 2]
    log = (yaml.safe_load(LOG.read_text(encoding="utf-8")) or {}) if solving and LOG.is_file() else {}
    targets = argv or ["build", "change", "handover", "diagnose", "generalize", "project"]
    paths = [p for t in targets for p in ([cases.DATASETS / f"{t}.yaml"] if "/" in t
                                          else sorted((cases.DATASETS / t).glob("*.yaml")))]
    bad = todo = 0
    to_solve = []
    for path in paths:
        case = cases.load(path)
        if case["line"] == "generalize" and "reference" not in case:
            todo += 1  # generalize: the reference is built in OSIS later
            print("todo " + case["id"] + " (no reference yet)")
            continue
        problems = verify_project(case, skills) if case["line"] == "project" else verify(case, skills)
        if solving and not problems:
            to_solve.append((case, path))
            continue
        bad += bool(problems)
        _report(case["id"], problems)
    stop = threading.Event()

    def work(case: dict, path: Path) -> list[str] | None:
        if stop.is_set():
            return None
        try:
            problems = solve_case(case, path, skills, config["osis"], log)
        except osis.BrokerError as exc:  # infrastructure: stop, the log keeps what is done for a rerun
            stop.set()
            print(f"STOP {case['id']}: {exc}", flush=True)
            return None
        with _LOCK:
            LOG.write_text(yaml.safe_dump(log, allow_unicode=True, sort_keys=True, width=200), encoding="utf-8")
        _report(case["id"], problems)
        return problems

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(lambda item: work(*item), to_solve))
    retry = sum(bool(r) and all(p.startswith("RETRY") for p in r) for r in results if r is not None)
    bad += sum(bool(r) for r in results if r is not None) - retry
    skipped = sum(r is None for r in results) + retry
    print(f"{len(paths) - bad - todo - skipped}/{len(paths)} ok, {todo} without reference"
          + (f", {skipped} to rerun (Broker stopped or machine trouble)" if skipped else ""))
    return 2 if skipped else 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
