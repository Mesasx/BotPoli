"""Tests for the new risk controls: weekly circuit breaker and per-trade % cap."""

from __future__ import annotations

from dataclasses import replace

from src.models import BUY, Signal
from src.portfolio import Portfolio
from src.risk_manager import RiskManager
from tests.conftest import make_snapshot


def _buy_signal(price=0.40):
    return Signal(
        market_id="mkt1", slug="will-x-happen", outcome="YES", token_id="tok_yes",
        signal_type=BUY, price=price, reason="test",
    )


def test_weekly_circuit_breaker_blocks_buys(config):
    rm = RiskManager(config)
    pf = Portfolio(config.initial_balance)
    # weekly loss beyond the cap -> halted
    d = rm.evaluate(_buy_signal(), make_snapshot(), pf,
                    daily_realized_pnl=0.0, weekly_realized_pnl=-config.max_weekly_loss)
    assert not d.approved
    assert "weekly" in d.reason.lower()
    assert rm.trading_halted(-config.max_weekly_loss)
    assert not rm.trading_halted(-config.max_weekly_loss + 1)


def test_weekly_breaker_allows_sells_even_when_halted(config):
    rm = RiskManager(config)
    pf = Portfolio.from_orders(config.initial_balance, [
        # open a position so there is something to sell
    ])
    from tests.conftest import make_order
    pf = Portfolio(config.initial_balance)
    pf.apply_order(make_order(side=BUY))
    sell = Signal(market_id="mkt1", slug="will-x-happen", outcome="YES",
                  token_id="tok_yes", signal_type="SELL", price=0.45, reason="exit")
    d = rm.evaluate(sell, make_snapshot(), pf, weekly_realized_pnl=-999.0)
    assert d.approved  # de-risking is always allowed


def test_per_trade_pct_cap(config):
    # 2% of equity. With small equity the pct cap binds below max_trade_size.
    cfg = replace(config, max_trade_pct=0.02, max_trade_size=5.0)
    rm = RiskManager(cfg)
    pf = Portfolio(100.0)  # equity 100 -> 2% = 2.0 USDC cap
    d = rm.evaluate(_buy_signal(), make_snapshot(), pf)
    assert d.approved
    assert abs(d.size_usdc - 2.0) < 1e-9


def test_per_trade_pct_disabled_when_zero(config):
    cfg = replace(config, max_trade_pct=0.0, max_trade_size=5.0)
    rm = RiskManager(cfg)
    pf = Portfolio(1000.0)
    d = rm.evaluate(_buy_signal(), make_snapshot(), pf)
    assert abs(d.size_usdc - 5.0) < 1e-9  # only the USDC cap applies
