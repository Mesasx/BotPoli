"""Tests for the runtime-editable configuration overlay."""

from __future__ import annotations

from src.runtime_config import (
    apply_overrides,
    build_config,
    editable_values,
    load_overrides,
    runtime_config_path,
    sanitize,
    save_overrides,
)


def test_sanitize_keeps_whitelist_and_coerces(config):
    raw = {"initial_balance": "2000", "max_open_positions": "7",
           "live_trading": True, "unknown": 1}
    clean = sanitize(raw)
    assert clean == {"initial_balance": 2000.0, "max_open_positions": 7}
    assert "live_trading" not in clean  # not editable -> dropped
    assert "unknown" not in clean


def test_sanitize_clamps_to_minimum(config):
    clean = sanitize({"initial_balance": -50, "poll_interval_seconds": 0})
    assert clean["initial_balance"] == 1.0
    assert clean["poll_interval_seconds"] == 2


def test_apply_overrides_returns_new_config(config):
    updated = apply_overrides(config, {"initial_balance": 5000, "max_trade_size": 12})
    assert updated.initial_balance == 5000.0
    assert updated.max_trade_size == 12.0
    assert config.initial_balance == 1000.0  # original untouched


def test_save_and_build_roundtrip(config):
    path = runtime_config_path(config)
    save_overrides(path, {"initial_balance": 3000, "max_open_positions": 9})
    assert load_overrides(path) == {"initial_balance": 3000, "max_open_positions": 9}
    rebuilt = build_config(path, base=config)
    assert rebuilt.initial_balance == 3000.0
    assert rebuilt.max_open_positions == 9


def test_editable_values_exposes_current(config):
    vals = editable_values(config)
    assert vals["initial_balance"] == 1000.0
    assert "max_total_exposure" in vals
    assert "live_trading" not in vals
