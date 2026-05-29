"""Strategy signal generation: entry, exit and rejection-to-HOLD conditions."""

from __future__ import annotations

from src.models import BUY, HOLD, SELL, Position
from src.strategy import SimpleThresholdStrategy

from .conftest import make_snapshot


def test_buy_when_cheap_tight_spread_liquid(config):
    strat = SimpleThresholdStrategy(config)
    snap = make_snapshot(best_ask=0.40, spread=0.01, liquidity=2000, hours_to_close=100)
    sig = strat.generate(snap, None)
    assert sig.signal_type == BUY
    assert sig.price == 0.40


def test_hold_when_price_above_max(config):
    strat = SimpleThresholdStrategy(config)
    snap = make_snapshot(best_ask=0.50)  # > entry_price_max 0.45
    sig = strat.generate(snap, None)
    assert sig.signal_type == HOLD
    assert "ENTRY_PRICE_MAX" in sig.reason


def test_hold_when_spread_too_wide(config):
    strat = SimpleThresholdStrategy(config)
    snap = make_snapshot(best_ask=0.40, spread=0.10)
    sig = strat.generate(snap, None)
    assert sig.signal_type == HOLD
    assert "MAX_SPREAD" in sig.reason


def test_hold_when_liquidity_too_low(config):
    strat = SimpleThresholdStrategy(config)
    snap = make_snapshot(best_ask=0.40, liquidity=100)
    sig = strat.generate(snap, None)
    assert sig.signal_type == HOLD
    assert "MIN_LIQUIDITY" in sig.reason


def test_hold_when_close_to_expiry(config):
    strat = SimpleThresholdStrategy(config)
    snap = make_snapshot(best_ask=0.40, hours_to_close=2)
    sig = strat.generate(snap, None)
    assert sig.signal_type == HOLD
    assert "MIN_HOURS_TO_CLOSE" in sig.reason


def test_no_outcome_blocked_by_default(config):
    strat = SimpleThresholdStrategy(config)
    snap = make_snapshot(outcome="NO", best_ask=0.30)
    sig = strat.generate(snap, None)
    assert sig.signal_type == HOLD
    assert "ALLOW_NO" in sig.reason


def test_no_outcome_allowed_when_enabled(config):
    from dataclasses import replace
    cfg = replace(config, allow_no=True)
    strat = SimpleThresholdStrategy(cfg)
    snap = make_snapshot(outcome="NO", best_ask=0.30, spread=0.01, liquidity=2000)
    sig = strat.generate(snap, None)
    assert sig.signal_type == BUY


def _open_position(avg_price=0.40, shares=10):
    return Position(market_id="mkt1", slug="will-x-happen", outcome="YES",
                    token_id="tok_yes", shares=shares, avg_price=avg_price,
                    mark_price=avg_price)


def test_take_profit_exit(config):
    strat = SimpleThresholdStrategy(config)
    pos = _open_position(avg_price=0.40)
    snap = make_snapshot(best_bid=0.50)  # +25% > TP 15%
    sig = strat.generate(snap, pos)
    assert sig.signal_type == SELL
    assert "take profit" in sig.reason


def test_stop_loss_exit(config):
    strat = SimpleThresholdStrategy(config)
    pos = _open_position(avg_price=0.40)
    snap = make_snapshot(best_bid=0.35)  # -12.5% < -SL 10%
    sig = strat.generate(snap, pos)
    assert sig.signal_type == SELL
    assert "stop loss" in sig.reason


def test_close_before_expiry_takes_priority(config):
    strat = SimpleThresholdStrategy(config)
    pos = _open_position(avg_price=0.40)
    snap = make_snapshot(best_bid=0.41, hours_to_close=3)  # within exit window
    sig = strat.generate(snap, pos)
    assert sig.signal_type == SELL
    assert "expiry" in sig.reason


def test_hold_within_band(config):
    strat = SimpleThresholdStrategy(config)
    pos = _open_position(avg_price=0.40)
    snap = make_snapshot(best_bid=0.41, hours_to_close=100)  # +2.5%
    sig = strat.generate(snap, pos)
    assert sig.signal_type == HOLD
