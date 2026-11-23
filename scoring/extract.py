"""Static facts of an OSIS project, read from its .py files (no OSIS needed).

``extract(root)`` = the conformance scorer's ``_extract_params`` (geometry, sections, stages, materials)
plus the load and analysis facts below. Only literal arguments are read, also through a module-level constant
(``LOAD = -11220.0``): a value the code computes at run time comes back as None, which a check reports as
``unextracted``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from scoring.evaluation.levels.model_conformance import collect_python_files
from scoring.evaluation.levels.model_conformance.common import _extract_params


_ARITH = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
          ast.Div: lambda a, b: a / b, ast.Pow: lambda a, b: a ** b}


def _literal(node: ast.AST, names: dict[str, Any] | None = None) -> Any:
    """Literal value, also of constant arithmetic (``0.75 * 1860e6``) and of ``names`` (module constants);
    None for anything computed at run time."""
    if isinstance(node, ast.Name) and names and node.id in names:
        return names[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _ARITH:
        a, b = _literal(node.left, names), _literal(node.right, names)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
            try:
                return _ARITH[type(node.op)](a, b)
            except (ZeroDivisionError, OverflowError):
                return None
        return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _literal(node.operand, names)
        return -value if isinstance(value, (int, float)) else None
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return None


def _constants(tree: ast.Module) -> dict[str, Any]:
    """Module-level ``NAME = <literal>`` assigned exactly once."""
    seen: dict[str, list[Any]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            seen.setdefault(node.targets[0].id, []).append(_literal(node.value))
    return {k: v[0] for k, v in seen.items() if len(v) == 1 and v[0] is not None}


def _chain(call: ast.Call, names: dict[str, Any] | None = None) -> list[tuple[str, list[Any]]]:
    """``engine.load.get("A").create("UTEMP", 1)`` -> [("engine", []), ("load", []), ("get", ["A"]), ("create", [...])]."""
    out: list[tuple[str, list[Any]]] = []
    node: ast.AST = call
    args: list[Any] = []
    while True:
        if isinstance(node, ast.Call):
            args = [_literal(a, names) for a in node.args]
            node = node.func
        elif isinstance(node, ast.Attribute):
            out.append((node.attr, args))
            args = []
            node = node.value
        elif isinstance(node, ast.Name):
            out.append((node.id, args))
            return out[::-1]
        else:
            return []


def _calls(files: dict[str, str]) -> list[tuple[str, list[Any], dict[str, Any], list[tuple[str, list[Any]]]]]:
    """(dotted name, args, kwargs, chain) of every call, e.g. ("engine.load.get.create", [...], {}, chain)."""
    found = []
    for text in files.values():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        names = _constants(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                chain = _chain(node, names)
                if chain:
                    kwargs = {k.arg: _literal(k.value, names) for k in node.keywords if k.arg}
                    found.append((".".join(n for n, _ in chain), chain[-1][1], kwargs, chain))
    return found


def load_facts(files: dict[str, str]) -> dict[str, Any]:
    cases: dict[str, Any] = {}
    utemp: set[Any] = set()
    tendons: set[Any] = set()
    stresses: set[Any] = set()
    line_loads: dict[str, set[Any]] = {}
    settlements: set[Any] = set()
    lanes = 0
    stages: dict[str, Any] = {}
    live_code = live_grade = humidity = None
    for name, args, kwargs, chain in _calls(files):
        arg = lambda i: args[i] if len(args) > i else None  # noqa: E731
        if name == "engine.load.create":
            cases[str(arg(0))] = arg(1)
        elif name == "engine.load.get.create":
            case = chain[-2][1][0] if chain[-2][1] else None
            if arg(0) == "UTEMP":
                utemp.add(arg(3))
            elif arg(0) == "PST":
                tendons.add(arg(1))
                stresses.add(arg(4))
            elif arg(0) == "LINE":
                line_loads.setdefault(str(case), set()).add(arg(9))
            elif arg(0) == "BLINE":  # OSIS exports some beam line loads as BLINE: one more flag (eProj) before the values
                line_loads.setdefault(str(case), set()).add(arg(10))
        elif name == "engine.live.grade.create":
            live_code, live_grade = arg(1), arg(2)
        elif name == "engine.stage.create":
            stages[str(arg(1))] = arg(2)
        elif name == "engine.live.lane.create":
            lanes += 1
        elif name == "engine.settlement.group.create":
            settlements.add(arg(1))
        elif name == "engine.prop.creep_shrink.create":
            # positional: (no, name, humidity, ...) before pyosis 0.7.5; OSIS exports (no, name, code, fcuk, humidity, ...)
            humidity = kwargs["avg_humidity"] if "avg_humidity" in kwargs else arg(4) if isinstance(arg(2), str) else arg(2)
    srt = lambda values: sorted(values, key=lambda v: (v is None, str(type(v)), v if v is not None else 0))  # noqa: E731
    return {
        "load_cases": sorted(cases), "load_case_types": cases,
        "has_uniform_temp": any(t == "T" for t in cases.values()), "uniform_temp": srt(utemp),
        "has_temp_gradient": any(t == "TG" for t in cases.values()),
        "live_code": live_code, "live_grade": live_grade, "lane_count": lanes,
        "settlements": srt(settlements), "humidity": humidity,
        "settlement_mm": sorted({round(abs(v) * 1000, 1) for v in settlements if isinstance(v, (int, float))}),
        "tendon_names": srt(tendons), "tendon_count": len(tendons), "tendon_stress": srt(stresses),
        "line_loads": {k: srt(v) for k, v in sorted(line_loads.items())},
        "stage_durations": stages,
        "creep_days": next((d for n, d in stages.items() if "徐变" in n), None),  # the long-term creep stage
        # kN/m, sign dropped: what a brief states ("铺装 10.85 kN/m"), whatever the case is called
        "line_load_kn": sorted({round(abs(v) / 1000, 3) for vs in line_loads.values() for v in vs
                                if isinstance(v, (int, float))}),
    }


def extract(root: Path) -> dict[str, Any]:
    """``root`` is the project's py/ folder or a template folder (either holds prep/)."""
    files = collect_python_files(root)
    params = {k: v for k, v in _extract_params(files).items()
              if k not in ("bridge_type", "is_continuous", "is_prestressed")}  # injected by the caller, None here
    return {**params, **load_facts(files)}
