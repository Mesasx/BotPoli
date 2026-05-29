"""Tests for trend-based exit and the weekly report generator."""

from __future__ import annotations

from datetime import timedelta

from src.models import BUY, SELL, EquitySnapshot, Position, utcnow
from src.report import generate_weekly_report
from src.storage import Storage
from src.strategy import SimpleThresholdStrategy
from tests.conftest import make_order, make_snapshot


# -- trend exit -------------------------------------------------------------
def test_trend_exit_triggers_on_strong_drop(config):
    strat = SimpleThresholdStrategy(config)
    pos = Position(market_id="mkt1", slug="s", outcome="YES", token_id="tok_yes",
                   shares=10.0, avg_price=0.40, mark_price=0.38)
    # mid still within SL band but a strong adverse trend (-30% <= -25%)
    snap = make_snapshot(best_bid=0.38, best_ask=0.39, midpoint=0.38)
    snap.trend = -0.30
    sig = strat.generate(snap, pos)
    assert sig.signal_type == SELL
    assert "trend" in sig.reason.lower()


def test_trend_exit_skipped_when_unknown(config):
    strat = SimpleThresholdStrategy(config)
    pos = Position(market_id="mkt1", slug="s", outcome="YES", token_id="tok_yes",
                   shares=10.0, avg_price=0.40, mark_price=0.39)
    snap = make_snapshot(best_bid=0.39, best_ask=0.40, midpoint=0.39)
    snap.trend = None  # no history -> no trend exit
    sig = strat.generate(snap, pos)
    assert sig.signal_type == "HOLD"


# -- weekly report ----------------------------------------------------------
def test_weekly_report_summarises_activity(config):
    storage = Storage(config)
    try:
        now = utcnow()
        # A round-trip trade settled this week: buy then sell at a profit.
        buy = make_order(side=BUY, fill_price=0.40, shares=10.0)
        buy.timestamp = now - timedelta(hours=2)
        sell = make_order(side=SELL, fill_price=0.50, shares=10.0)
        sell.timestamp = now - timedelta(hours=1)
        sell.reason = "take profit"
        storage.save_order(buy)
        storage.save_order(sell)
        storage.save_market_snapshots([make_snapshot()])
        storage.save_equity_snapshot(EquitySnapshot(
            timestamp=now, cash=1001.0, positions_value=0.0, equity=1001.0,
            realized_pnl=1.0, unrealized_pnl=0.0, open_positions=0,
            exposure=0.0, drawdown=0.0,
        ))

        report = generate_weekly_report(storage, config, now=now)
        assert report.n_buys == 1
        assert report.n_sells == 1
        assert report.wins == 1
        assert report.losses == 0
        assert report.realized_pnl_week > 0.9  # ~ (0.50-0.40)*10 = 1.0
        md = report.to_markdown()
        assert "Informe semanal" in md
        assert "PnL semanal" in md
    finally:
        storage.close()


def test_weekly_report_flags_settlement(config):
    storage = Storage(config)
    try:
        now = utcnow()
        buy = make_order(side=BUY, fill_price=0.40, shares=10.0)
        buy.timestamp = now - timedelta(hours=3)
        settle = make_order(side=SELL, fill_price=1.0, shares=10.0)
        settle.timestamp = now - timedelta(hours=1)
        settle.reason = "settlement: market resolved, YES won (→1.0)"
        storage.save_order(buy)
        storage.save_order(settle)

        report = generate_weekly_report(storage, config, now=now)
        assert len(report.settlements) == 1
        assert report.settlements[0].exit_price == 1.0
    finally:
        storage.close()


def test_weekly_report_empty_db_is_safe(config):
    storage = Storage(config)
    try:
        report = generate_weekly_report(storage, config)
        assert report.n_buys == 0 and report.n_sells == 0
        assert report.total_pnl == 0.0
        assert "sin actividad" in report.explanation.lower()
    finally:
        storage.close()


# -- 15-minute review -------------------------------------------------------
def test_review_digest_flags_insufficient_weeks(config):
    from src.report import generate_review
    storage = Storage(config)
    try:
        digest = generate_review(storage, config)
        assert not digest.ready  # no data -> < 8 weeks -> not ready
        names = {c.name: c.status for c in digest.criteria}
        assert names["≥ 8 semanas de paper"] == "fail"
        assert names["Liquidación settle 0/1"] == "pass"
        md = digest.to_markdown()
        assert "Revisión de 15 minutos" in md
        assert "Veredicto" in md
    finally:
        storage.close()
