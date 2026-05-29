"""Storage persistence, portfolio replay, daily PnL, reset/export, and market parsing."""

from __future__ import annotations

from src.market_scanner import MarketScanner
from src.models import BUY, SELL, EquitySnapshot, utcnow
from src.polymarket_client import parse_json_field
from src.storage import Storage

from .conftest import FakeClient, make_order, sample_gamma_market


def test_init_creates_tables(config):
    st = Storage(config)
    try:
        for table in ("market_snapshots", "signals", "orders", "equity_snapshots", "positions"):
            df = st.get_df(table)
            assert df is not None
    finally:
        st.close()


def test_save_and_replay_orders(config):
    st = Storage(config)
    try:
        st.save_order(make_order(side=BUY, fill_price=0.40, shares=10))
        st.save_order(make_order(side=SELL, fill_price=0.50, shares=10))
        pf = st.load_portfolio()
        assert abs(pf.realized_pnl - 1.0) < 1e-9
        assert pf.open_position_count() == 0
    finally:
        st.close()


def test_daily_realized_pnl(config):
    st = Storage(config)
    try:
        st.save_order(make_order(side=BUY, fill_price=0.40, shares=10))
        st.save_order(make_order(side=SELL, fill_price=0.45, shares=10))  # +0.5
        assert abs(st.daily_realized_pnl() - 0.5) < 1e-9
    finally:
        st.close()


def test_equity_snapshot_and_peak(config):
    st = Storage(config)
    try:
        st.save_equity_snapshot(EquitySnapshot(
            timestamp=utcnow(), cash=996.0, positions_value=6.0, equity=1002.0,
            realized_pnl=0.0, unrealized_pnl=2.0, open_positions=1, exposure=6.0,
            drawdown=0.0,
        ))
        assert st.peak_equity() == 1002.0
    finally:
        st.close()


def test_reset_paper_clears_state(config):
    st = Storage(config)
    try:
        st.save_order(make_order())
        st.reset_paper()
        assert st.get_df("orders").empty
    finally:
        st.close()


def test_export_csv_writes_files(config, tmp_path):
    st = Storage(config)
    try:
        st.save_order(make_order())
        paths = st.export_csv(tmp_path / "exports")
        assert paths
        assert all(p.exists() for p in paths)
    finally:
        st.close()


def test_parse_json_field_handles_encoded_strings():
    assert parse_json_field('["Yes", "No"]') == ["Yes", "No"]
    assert parse_json_field(["a", "b"]) == ["a", "b"]
    assert parse_json_field(None) == []
    assert parse_json_field("not json") == []


def test_scanner_expands_market_outcomes(config):
    client = FakeClient(
        markets=[sample_gamma_market()],
        quotes={
            "tok_yes": {"best_bid": 0.39, "best_ask": 0.40, "midpoint": 0.395, "spread": 0.01},
            "tok_no": {"best_bid": 0.59, "best_ask": 0.60, "midpoint": 0.595, "spread": 0.01},
        },
    )
    scanner = MarketScanner(config, client)
    snaps = scanner.scan(enrich=True, apply_filters=False)
    assert len(snaps) == 2
    yes = next(s for s in snaps if s.outcome == "Yes")
    assert yes.token_id == "tok_yes"
    assert yes.best_ask == 0.40
    assert yes.volume == 10000.0
    assert yes.liquidity == 2000.0
    assert yes.hours_to_close is not None and yes.hours_to_close > 90


def test_scanner_filters_untradable(config):
    market = sample_gamma_market()
    client = FakeClient(
        markets=[market],
        quotes={
            "tok_yes": {"best_bid": 0.30, "best_ask": 0.40, "midpoint": 0.35, "spread": 0.10},  # wide spread
            "tok_no": {"best_bid": 0.59, "best_ask": 0.60, "midpoint": 0.595, "spread": 0.01},
        },
    )
    scanner = MarketScanner(config, client)
    snaps = scanner.scan(enrich=True, apply_filters=True)
    tokens = {s.token_id for s in snaps}
    assert "tok_yes" not in tokens  # filtered by spread
