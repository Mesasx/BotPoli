"""Tests for the offline backtester."""

from __future__ import annotations

from datetime import timedelta

from src.backtest import Backtester, group_into_cycles
from src.models import utcnow
from src.strategy import build_strategy
from tests.conftest import make_snapshot


def _snap_at(ts, **kw):
    s = make_snapshot(**kw)
    s.timestamp = ts
    return s


def test_group_into_cycles_splits_on_gap():
    t0 = utcnow()
    history = [
        _snap_at(t0, token_id="a"),
        _snap_at(t0 + timedelta(milliseconds=10), token_id="b"),
        # big gap -> new cycle
        _snap_at(t0 + timedelta(seconds=30), token_id="a"),
        _snap_at(t0 + timedelta(seconds=30, milliseconds=5), token_id="b"),
    ]
    cycles = group_into_cycles(history, gap_seconds=5.0)
    assert len(cycles) == 2
    assert {s.token_id for s in cycles[0]} == {"a", "b"}


def test_group_into_cycles_dedups_token_within_cycle():
    t0 = utcnow()
    history = [
        _snap_at(t0, token_id="a", best_ask=0.40),
        _snap_at(t0 + timedelta(milliseconds=1), token_id="a", best_ask=0.41),
    ]
    cycles = group_into_cycles(history)
    assert len(cycles) == 1
    assert len(cycles[0]) == 1  # latest snapshot for token "a" only


def test_backtest_runs_and_reports_metrics(config):
    t0 = utcnow()
    # Cycle 1: cheap YES -> buy. Cycle 2: price jumps -> take profit sell.
    history = [
        _snap_at(t0, token_id="tok_yes", outcome="YES",
                 best_bid=0.39, best_ask=0.40, midpoint=0.395),
        _snap_at(t0 + timedelta(seconds=30), token_id="tok_yes", outcome="YES",
                 best_bid=0.55, best_ask=0.56, midpoint=0.555),
    ]
    result = Backtester(config, build_strategy("simple_threshold", config)).run(history)
    assert result.cycles == 2
    assert result.n_buys == 1
    assert result.n_sells == 1
    assert result.wins == 1
    assert result.total_pnl > 0
    assert "Backtest result" in result.to_text()


def test_backtest_empty_history(config):
    result = Backtester(config).run([])
    assert result.cycles == 0
    assert result.total_pnl == 0.0
    assert result.final_equity == config.initial_balance


def test_backtest_with_value_strategy_runs(config):
    # Three cycles so the value strategy gets a reference price; a dip in c3.
    t0 = utcnow()
    history = []
    # A *moderate* dip (between MIN_EDGE 4% and the 10% downtrend guard).
    for i, (ask, bid, mid) in enumerate([(0.46, 0.45, 0.455), (0.46, 0.45, 0.455), (0.43, 0.42, 0.43)]):
        history.append(_snap_at(t0 + timedelta(seconds=30 * i), token_id="tok_yes",
                                outcome="YES", best_ask=ask, best_bid=bid, midpoint=mid,
                                volume=20000.0, liquidity=2000.0))
    result = Backtester(config).run(history)   # default = value strategy
    assert result.cycles == 3
    assert result.n_buys >= 1   # buys the dip once a reference exists
