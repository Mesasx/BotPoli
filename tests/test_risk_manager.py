"""Risk manager: sizing caps, circuit breakers, and signal rejection."""

from __future__ import annotations

from dataclasses import replace

from src.models import BUY, HOLD, SELL, Signal
from src.portfolio import Portfolio
from src.risk_manager import RiskManager

from .conftest import make_order, make_snapshot


def _buy_signal(price=0.40, token_id="tok_yes", market_id="mkt1"):
    return Signal(market_id=market_id, slug="will-x-happen", outcome="YES",
                  token_id=token_id, signal_type=BUY, price=price, reason="t")


def test_hold_signal_rejected(config):
    rm = RiskManager(config)
    sig = Signal(market_id="m", slug="s", outcome="YES", token_id="t",
                 signal_type=HOLD, price=0.4, reason="hold")
    d = rm.evaluate(sig, make_snapshot(), Portfolio(1000.0))
    assert not d.approved


def test_buy_sized_to_max_trade(config):
    rm = RiskManager(config)
    d = rm.evaluate(_buy_signal(), make_snapshot(), Portfolio(1000.0))
    assert d.approved
    assert d.size_usdc == config.max_trade_size  # 5.0


def test_buy_capped_by_cash(config):
    rm = RiskManager(config)
    pf = Portfolio(3.0)  # less than max_trade_size
    d = rm.evaluate(_buy_signal(), make_snapshot(), pf)
    assert d.approved
    assert abs(d.size_usdc - 3.0) < 1e-9


def test_buy_blocked_by_daily_loss(config):
    rm = RiskManager(config)
    d = rm.evaluate(_buy_signal(), make_snapshot(), Portfolio(1000.0),
                    daily_realized_pnl=-25.0)  # beyond -20 limit
    assert not d.approved
    assert "daily loss" in d.reason


def test_buy_blocked_by_max_open_positions(config):
    cfg = replace(config, max_open_positions=1)
    rm = RiskManager(cfg)
    pf = Portfolio(1000.0)
    pf.apply_order(make_order(token_id="other", market_id="mOther", fill_price=0.4, shares=5))
    d = rm.evaluate(_buy_signal(token_id="new", market_id="mNew"), make_snapshot(), pf)
    assert not d.approved
    assert "max open positions" in d.reason


def test_buy_capped_by_market_exposure(config):
    cfg = replace(config, max_market_exposure=2.0, max_trade_size=100.0,
                  max_position_size=100.0, max_total_exposure=100.0)
    rm = RiskManager(cfg)
    pf = Portfolio(1000.0)
    pf.apply_order(make_order(token_id="a", market_id="mkt1", fill_price=0.50, shares=2))  # 1.0
    pf.mark_to_market({"a": 0.50})
    d = rm.evaluate(_buy_signal(token_id="b", market_id="mkt1"), make_snapshot(), pf)
    assert d.approved
    assert abs(d.size_usdc - 1.0) < 1e-9   # 2.0 cap - 1.0 used


def test_buy_rejected_when_no_budget(config):
    cfg = replace(config, max_total_exposure=0.0)
    rm = RiskManager(cfg)
    d = rm.evaluate(_buy_signal(), make_snapshot(), Portfolio(1000.0))
    assert not d.approved
    assert "budget" in d.reason


def test_sell_with_position_approved(config):
    rm = RiskManager(config)
    pf = Portfolio(1000.0)
    pf.apply_order(make_order(side=BUY, token_id="tok_yes", fill_price=0.40, shares=10))
    sig = Signal(market_id="mkt1", slug="will-x-happen", outcome="YES",
                 token_id="tok_yes", signal_type=SELL, price=0.45, reason="tp")
    d = rm.evaluate(sig, make_snapshot(), pf)
    assert d.approved


def test_sell_without_position_rejected(config):
    rm = RiskManager(config)
    sig = Signal(market_id="m", slug="s", outcome="YES", token_id="ghost",
                 signal_type=SELL, price=0.45, reason="tp")
    d = rm.evaluate(sig, make_snapshot(), Portfolio(1000.0))
    assert not d.approved
