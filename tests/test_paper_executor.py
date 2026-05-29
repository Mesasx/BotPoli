"""Paper executor: slippage, share sizing, fees and rejections."""

from __future__ import annotations

from dataclasses import replace

from src.models import BUY, SELL, Position
from src.paper_executor import PaperExecutor

from .conftest import make_snapshot


def test_buy_applies_adverse_slippage(config):
    ex = PaperExecutor(config)  # 10 bps
    snap = make_snapshot(best_ask=0.40)
    order = ex.execute_buy(snap, size_usdc=5.0)
    assert order.status == "FILLED"
    assert order.side == BUY
    expected_fill = 0.40 * (1 + 0.001)   # 0.4004
    assert abs(order.fill_price - expected_fill) < 1e-9
    assert order.slippage > 0
    assert abs(order.shares - 5.0 / expected_fill) < 1e-6
    assert abs(order.value_usdc - order.shares * order.fill_price) < 1e-9


def test_buy_with_fee(config):
    cfg = replace(config, fee_bps=100.0)  # 1%
    ex = PaperExecutor(cfg)
    order = ex.execute_buy(make_snapshot(best_ask=0.40), size_usdc=10.0)
    assert abs(order.fee - order.value_usdc * 0.01) < 1e-9


def test_buy_rejected_on_invalid_price(config):
    ex = PaperExecutor(config)
    order = ex.execute_buy(make_snapshot(best_ask=0.0), size_usdc=5.0)
    assert order.status == "REJECTED"


def test_sell_applies_adverse_slippage(config):
    ex = PaperExecutor(config)
    snap = make_snapshot(best_bid=0.50)
    pos = Position(market_id="mkt1", slug="s", outcome="YES", token_id="tok_yes",
                   shares=10, avg_price=0.40)
    order = ex.execute_sell(snap, pos)
    assert order.status == "FILLED"
    assert order.side == SELL
    expected_fill = 0.50 * (1 - 0.001)
    assert abs(order.fill_price - expected_fill) < 1e-9
    assert order.shares == 10
    assert order.slippage > 0


def test_sell_rejected_without_shares(config):
    ex = PaperExecutor(config)
    pos = Position(market_id="mkt1", slug="s", outcome="YES", token_id="tok_yes",
                   shares=0.0, avg_price=0.40)
    order = ex.execute_sell(make_snapshot(best_bid=0.5), pos)
    assert order.status == "REJECTED"


def test_fill_price_clamped_to_valid_range(config):
    cfg = replace(config, slippage_bps=100000.0)  # absurd slippage
    ex = PaperExecutor(cfg)
    order = ex.execute_buy(make_snapshot(best_ask=0.95), size_usdc=5.0)
    assert order.fill_price < 1.0
