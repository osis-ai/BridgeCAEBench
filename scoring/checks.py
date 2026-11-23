"""Checks on extracted project facts, and ``preserve`` (nothing outside the request changed).

A check is ``{field, op, value, tol}``; ``field`` may go one level into a dict with ``a.b``
(e.g. ``line_loads.铺装工况``). Ops:
  eq           actual == value
  approx       |actual - value| <= tol (default 0.01)
  rel          |actual - value| <= tol * |value| (default 0.02)
  approx_list  same length, element-wise approx
  contains     value is in actual (list or dict keys)
  absent       value is not in actual
  in           actual is one of value (a list)
"""

from __future__ import annotations

import math
from typing import Any

_MISSING = object()


def _get(facts: dict[str, Any], field: str) -> Any:
    head, _, rest = field.partition(".")
    value = facts.get(head, _MISSING)
    if rest:
        value = value.get(rest, _MISSING) if isinstance(value, dict) else _MISSING
    return value


def _close(a: Any, b: Any, tol: float) -> bool:
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) <= tol


def check(facts: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    op, expected = spec["op"], spec.get("value")
    actual = _get(facts, spec["field"])
    unextracted = actual is _MISSING or actual is None
    if op == "absent":
        ok = actual is _MISSING or actual is None or expected not in actual
    elif unextracted:
        ok = False
    elif op == "eq":
        ok = actual == expected
    elif op == "approx":
        ok = _close(actual, expected, float(spec.get("tol", 0.01)))
    elif op == "rel":
        ok = _close(actual, expected, float(spec.get("tol", 0.02)) * abs(expected))
    elif op == "approx_list":
        tol = float(spec.get("tol", 0.01))
        ok = isinstance(actual, list) and len(actual) == len(expected) and all(
            _close(a, b, tol) for a, b in zip(actual, expected))
    elif op == "contains":
        ok = isinstance(actual, (list, dict)) and expected in actual
    elif op == "in":
        ok = actual in expected
    else:
        raise ValueError(f"unknown check op {op!r}")
    return {**spec, "actual": None if actual is _MISSING else actual, "ok": bool(ok),
            "unextracted": unextracted and op != "absent"}


def score_checks(facts: dict[str, Any], specs: list[dict[str, Any]]) -> dict[str, Any]:
    results = [check(facts, s) for s in specs]
    return {"score": sum(r["ok"] for r in results) / len(results) if results else 1.0, "results": results}


# Facts that are scores or summaries of other facts; comparing them would double-count one change.
_DERIVED = {"concrete_ratio", "component_thickness_avg", "section_area_avg", "pst_steel_ratio", "load_case_types",
            "line_load_kn"}


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return a == b


def _units(start: dict[str, Any], candidate: dict[str, Any], exclude: set[str]):
    """(name, start value, candidate value) per compared unit: a dict fact per key (``a.b``), others whole."""
    for k in sorted(start):
        if k in exclude or k in _DERIVED or start[k] is None:
            continue
        a, b = start[k], candidate.get(k)
        if not isinstance(a, dict):
            yield k, a, b
            continue
        b = b if isinstance(b, dict) else {}
        for sub in sorted(set(a) | set(b), key=str):  # a key added or dropped is a change too
            if f"{k}.{sub}" not in exclude:
                yield f"{k}.{sub}", a.get(sub, _MISSING), b.get(sub, _MISSING)


def preserve(start: dict[str, Any], candidate: dict[str, Any], exclude: set[str]) -> dict[str, Any]:
    """Share of start facts that are unchanged, leaving out ``exclude`` (the fields the request may change:
    ``a`` leaves out a whole fact, ``a.b`` one key of a dict fact)."""
    units = list(_units(start, candidate, exclude))
    changed = [name for name, a, b in units if not _same(a, b)]
    return {"score": 1 - len(changed) / len(units) if units else 1.0, "compared": len(units), "changed": changed}
