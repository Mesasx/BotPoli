"""Runtime-editable configuration overlay.

The base :class:`~src.config.Config` is immutable and comes from the environment
/ ``.env``. The supervision platform lets you tweak a *whitelisted* subset of
paper-trading parameters live (initial balance, per-trade size, risk caps, max
open positions, polling cadence) without editing code or restarting.

Overrides are persisted as JSON in ``data/runtime_config.json`` and merged over
the base config. Only paper-trading knobs are editable here — there is no switch
that could enable real trading (``live_trading`` is NOT editable and the config
loader still aborts if it is ever set).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .logger import get_logger

log = get_logger("runtime_config")

# field name -> (type, label, minimum)
EDITABLE_FIELDS: dict[str, tuple[type, str, float]] = {
    "initial_balance": (float, "Saldo inicial (USDC paper)", 1.0),
    "max_trade_size": (float, "Máximo por operación (USDC)", 0.0),
    "max_trade_pct": (float, "Máximo por operación (% del equity)", 0.0),
    "max_position_size": (float, "Máximo por posición (USDC)", 0.0),
    "max_total_exposure": (float, "Riesgo máximo total (USDC)", 0.0),
    "max_open_positions": (int, "Máximo de posiciones abiertas", 0),
    "max_daily_loss": (float, "Stop diario (USDC)", 0.0),
    "max_weekly_loss": (float, "Stop semanal (USDC)", 0.0),
    "poll_interval_seconds": (int, "Intervalo de sondeo (s)", 2),
}


def runtime_config_path(config: Config) -> Path:
    return config.db_path.parent / "runtime_config.json"


def load_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read runtime config %s: %s", path, exc)
        return {}


def save_overrides(path: Path, overrides: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides, indent=2, sort_keys=True), encoding="utf-8")


def sanitize(overrides: dict[str, Any]) -> dict[str, Any]:
    """Keep only known fields, coerce types and clamp to the allowed minimum."""
    clean: dict[str, Any] = {}
    for key, value in overrides.items():
        if key not in EDITABLE_FIELDS:
            continue
        typ, _label, minimum = EDITABLE_FIELDS[key]
        try:
            coerced = typ(value)
        except (TypeError, ValueError):
            continue
        if coerced < minimum:
            coerced = typ(minimum)
        clean[key] = coerced
    return clean


def apply_overrides(base: Config, overrides: dict[str, Any]) -> Config:
    """Return a new Config with the whitelisted overrides applied."""
    clean = sanitize(overrides)
    return replace(base, **clean) if clean else base


def build_config(path: Path, base: Config | None = None) -> Config:
    """Load the base config and overlay any persisted runtime overrides."""
    base = base or load_config()
    return apply_overrides(base, load_overrides(path))


def editable_values(config: Config) -> dict[str, Any]:
    """The current values of the editable fields, for the UI form."""
    return {name: getattr(config, name) for name in EDITABLE_FIELDS}
