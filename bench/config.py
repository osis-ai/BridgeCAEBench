"""Load configs/platform.yaml (+ optional platform.local.yaml) and resolve paths against the repo root."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs" / "platform.yaml"


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def _read(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a mapping")
    return data


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """``<name>.local.yaml`` next to the config overrides it; its ``secrets`` become env vars.

    Secrets never stay in the returned config: it is copied into request.json and campaign.json.
    """
    source = Path(path) if path else DEFAULT_CONFIG
    data = _read(source)
    local = source.with_name(f"{source.stem}.local{source.suffix}")
    if local.is_file():
        data = _merge(data, _read(local))
    for name, value in (data.pop("secrets", None) or {}).items():
        if value and not os.environ.get(name, "").strip():
            os.environ[name] = str(value)
    return data


def resolve(value: str | Path, base: Path = ROOT) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()
