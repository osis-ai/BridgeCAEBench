"""Method registry. A method is a directory here with ``method.yaml`` and ``adapter.py``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

METHODS_DIR = Path(__file__).resolve().parent


def discover() -> dict[str, dict[str, Any]]:
    import yaml

    registry: dict[str, dict[str, Any]] = {}
    for directory in sorted(METHODS_DIR.iterdir()):
        spec_path = directory / "method.yaml"
        if not spec_path.is_file():
            continue
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        spec["module"] = f"methods.{directory.name}.adapter"
        spec.setdefault("tasks", "all")
        spec.setdefault("serial", False)
        spec.setdefault("legacy", False)  # retired methods run only when named (--methods)
        if spec.get("python") not in {"main", "venv", "parent"}:
            raise ValueError(f"{spec_path}: python must be main, venv, or parent")
        if spec["id"] in registry:
            raise ValueError(f"duplicate method id {spec['id']}")
        registry[spec["id"]] = spec
    return registry


def supports(spec: dict[str, Any], task: str) -> bool:
    return spec["tasks"] == "all" or task in spec["tasks"]
