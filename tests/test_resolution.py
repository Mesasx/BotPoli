"""Tests for market resolution (settle 0/1) and the resolver."""

from __future__ import annotations

import json

from src.models import Position
from src.resolution import MarketResolver, detect_resolution
from tests.conftest import FakeClient


def _resolved_market(winner="Yes", market_id="mkt1"):
    prices = ["1", "0"] if winner == "Yes" else ["0", "1"]
    return {
        "id": market_id,
        "slug": "will-x-happen",
        "closed": True,
        "umaResolutionStatus": "resolved",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(prices),
    }


def test_detect_resolution_winner_and_loser():
    market = _resolved_market(winner="Yes")
    yes = detect_resolution(market, "YES")
    no = detect_resolution(market, "NO")
    assert yes is not None and yes.won and yes.settle_price == 1.0
    assert no is not None and not no.won and no.settle_price == 0.0


def test_detect_resolution_skips_open_market():
    market = _resolved_market()
    market["closed"] = False
    market["umaResolutionStatus"] = "pending"
    assert detect_resolution(market, "YES") is None


def test_detect_resolution_skips_ambiguous_prices():
    market = _resolved_market()
    market["outcomePrices"] = json.dumps(["0.55", "0.45"])  # not a clear 0/1
    assert detect_resolution(market, "YES") is None


def test_resolver_builds_settlement_order_for_winner(config):
    client = FakeClient(markets_by_id={"mkt1": _resolved_market(winner="Yes")})
    resolver = MarketResolver(config, client)
    pos = Position(
        market_id="mkt1", slug="will-x-happen", outcome="YES", token_id="tok_yes",
        shares=10.0, avg_price=0.40, mark_price=0.40,
    )
    orders = resolver.settlement_orders([pos])
    assert len(orders) == 1
    o = orders[0]
    assert o.side == "SELL" and o.status == "FILLED"
    assert o.fill_price == 1.0
    assert o.value_usdc == 10.0
    assert "settlement" in o.reason.lower()


def test_resolver_settles_losing_position_to_zero(config):
    client = FakeClient(markets_by_id={"mkt1": _resolved_market(winner="No")})
    resolver = MarketResolver(config, client)
    pos = Position(
        market_id="mkt1", slug="will-x-happen", outcome="YES", token_id="tok_yes",
        shares=10.0, avg_price=0.40, mark_price=0.40,
    )
    orders = resolver.settlement_orders([pos])
    assert len(orders) == 1
    assert orders[0].fill_price == 0.0
    assert orders[0].value_usdc == 0.0


def test_resolver_disabled_returns_nothing(config):
    from dataclasses import replace
    cfg = replace(config, settle_resolved=False)
    client = FakeClient(markets_by_id={"mkt1": _resolved_market()})
    resolver = MarketResolver(cfg, client)
    pos = Position(market_id="mkt1", slug="s", outcome="YES", token_id="t",
                   shares=10.0, avg_price=0.4, mark_price=0.4)
    assert resolver.settlement_orders([pos]) == []
