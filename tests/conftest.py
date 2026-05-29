"""Shared test fixtures and helpers.

Tests never touch the network: a :class:`FakeClient` returns canned Gamma/CLOB
responses so the scanner and engine logic can be exercised deterministically.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from src.config import Config
from src.models import BUY, MarketSnapshot, Order, utcnow


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        initial_balance=1000.0,
        allow_no=False,
        max_trade_size=5.0,
        max_trade_pct=0.0,          # disabled here; covered by dedicated tests
        max_position_size=25.0,
        max_daily_loss=20.0,
        max_weekly_loss=50.0,
        max_open_positions=5,
        max_market_exposure=25.0,
        max_total_exposure=100.0,
        max_spread=0.03,
        min_liquidity=500.0,
        min_hours_to_close=24.0,
        exit_hours_before_close=6.0,
        entry_price_max=0.45,
        take_profit_pct=0.15,
        stop_loss_pct=0.10,
        slippage_bps=10.0,
        fee_bps=0.0,
        database_url=f"sqlite:///{tmp_path}/test.db",
    )


def make_snapshot(
    *,
    token_id="tok_yes",
    market_id="mkt1",
    slug="will-x-happen",
    outcome="YES",
    best_bid=0.39,
    best_ask=0.40,
    midpoint=0.395,
    spread=0.01,
    volume=10000.0,
    liquidity=2000.0,
    hours_to_close=100.0,
) -> MarketSnapshot:
    end = utcnow() + timedelta(hours=hours_to_close) if hours_to_close is not None else None
    return MarketSnapshot(
        market_id=market_id, slug=slug, question="Will X happen?", category="crypto",
        outcome=outcome, token_id=token_id, best_bid=best_bid, best_ask=best_ask,
        midpoint=midpoint, spread=spread, volume=volume, liquidity=liquidity,
        end_date=end, hours_to_close=hours_to_close,
    )


def make_order(side=BUY, token_id="tok_yes", fill_price=0.40, shares=10.0,
               fee=0.0, slug="will-x-happen", outcome="YES", market_id="mkt1") -> Order:
    return Order(
        market_id=market_id, slug=slug, outcome=outcome, token_id=token_id, side=side,
        requested_price=fill_price, fill_price=fill_price, shares=shares,
        value_usdc=fill_price * shares, slippage=0.0, fee=fee, status="FILLED",
    )


class FakeClient:
    """Stand-in for :class:`PolymarketClient` returning canned data."""

    def __init__(self, markets=None, quotes=None, markets_by_id=None):
        self._markets = markets or []
        self._quotes = quotes or {}
        self._markets_by_id = markets_by_id or {}

    def get_markets(self, **_kwargs):
        return self._markets

    def get_quote(self, token_id):
        return self._quotes.get(
            token_id,
            {"best_bid": 0.39, "best_ask": 0.40, "midpoint": 0.395, "spread": 0.01},
        )

    def get_midpoint(self, token_id):
        return self.get_quote(token_id)["midpoint"]

    def get_market(self, market_id):
        return self._markets_by_id.get(market_id)


def sample_gamma_market() -> dict:
    """A realistic Gamma /markets entry (fields JSON-encoded as strings)."""
    end = (utcnow() + timedelta(hours=100)).isoformat()
    return {
        "id": "12345",
        "conditionId": "0xabc",
        "question": "Will X happen?",
        "slug": "will-x-happen",
        "category": "crypto",
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["tok_yes", "tok_no"]),
        "outcomePrices": json.dumps(["0.40", "0.60"]),
        "liquidityNum": 2000.0,
        "volumeNum": 10000.0,
        "volume24hr": 1500.0,
        "active": True,
        "closed": False,
        "endDate": end,
        "bestBid": 0.39,
        "bestAsk": 0.40,
        "spread": 0.01,
    }
