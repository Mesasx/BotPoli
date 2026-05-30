"""Tests for the smarter, value/edge-based entry strategy (Level A)."""

from __future__ import annotations

from dataclasses import replace

from src.models import BUY, HOLD, Position
from src.strategy import ValueStrategy, build_strategy
from tests.conftest import make_snapshot


def _snap(**kw):
    return make_snapshot(**kw)


def test_build_strategy_defaults_to_value(config):
    assert isinstance(build_strategy(config.strategy_name, config), ValueStrategy)
    assert isinstance(build_strategy("unknown-name", config), ValueStrategy)


def test_value_buys_quality_dip_with_edge(config):
    strat = ValueStrategy(config)
    # mid 0.40 in band, high volume/liquidity, ask 0.40 vs ref 0.46 -> ~13% edge
    snap = _snap(best_bid=0.39, best_ask=0.40, midpoint=0.40, volume=20000, liquidity=2000)
    snap.ref_price = 0.46
    snap.trend = 0.0
    sig = strat.generate(snap, None)
    assert sig.signal_type == BUY
    assert "edge" in sig.reason.lower()


def test_value_skips_longshot_outside_band(config):
    strat = ValueStrategy(config)
    # mid 0.08 -> longshot below MIN_ENTRY_PROB; even with a big "discount"
    snap = _snap(best_bid=0.07, best_ask=0.08, midpoint=0.08, volume=20000)
    snap.ref_price = 0.20
    sig = strat.generate(snap, None)
    assert sig.signal_type == HOLD
    assert "band" in sig.reason.lower()


def test_value_skips_when_no_edge(config):
    strat = ValueStrategy(config)
    # ask ~ ref -> no discount
    snap = _snap(best_bid=0.39, best_ask=0.40, midpoint=0.40, volume=20000)
    snap.ref_price = 0.405
    sig = strat.generate(snap, None)
    assert sig.signal_type == HOLD
    assert "edge" in sig.reason.lower()


def test_value_holds_on_cold_start_without_reference(config):
    strat = ValueStrategy(config)
    snap = _snap(best_bid=0.39, best_ask=0.40, midpoint=0.40, volume=20000)
    snap.ref_price = None  # no history yet
    assert strat.generate(snap, None).signal_type == HOLD


def test_value_skips_low_volume(config):
    strat = ValueStrategy(config)
    snap = _snap(best_bid=0.39, best_ask=0.40, midpoint=0.40, volume=100)  # < MIN_VOLUME
    snap.ref_price = 0.46
    sig = strat.generate(snap, None)
    assert sig.signal_type == HOLD
    assert "volume" in sig.reason.lower()


def test_value_skips_strong_downtrend(config):
    strat = ValueStrategy(config)
    snap = _snap(best_bid=0.39, best_ask=0.40, midpoint=0.40, volume=20000)
    snap.ref_price = 0.46
    snap.trend = -0.50  # falling knife
    sig = strat.generate(snap, None)
    assert sig.signal_type == HOLD
    assert "downtrend" in sig.reason.lower()


def test_value_inherits_exit_logic(config):
    # take-profit exit should still work (inherited from the threshold strategy)
    cfg = replace(config, take_profit_pct=0.15)
    strat = ValueStrategy(cfg)
    pos = Position(market_id="mkt1", slug="s", outcome="YES", token_id="tok_yes",
                   shares=10.0, avg_price=0.40, mark_price=0.50)
    snap = _snap(best_bid=0.50, best_ask=0.51, midpoint=0.505)
    sig = strat.generate(snap, pos)
    assert sig.signal_type == "SELL"
    assert "take profit" in sig.reason.lower()
