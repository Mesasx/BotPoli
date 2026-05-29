"""Market resolution (settle 0/1).

When a Polymarket market resolves, each outcome token is worth exactly 1 USDC
(if it was the winning outcome) or 0 USDC (if it lost). A paper-trading bot that
only ever exits on price/time would never realise that final PnL, so this module
closes the loop:

1. For every open position, look up its market via the Gamma API.
2. Detect a *resolved* market: it is ``closed`` and its ``outcomePrices`` have
   collapsed to a clear winner (~1) and loser (~0).
3. Build a settlement SELL order at the final 0/1 price (no slippage, no fee —
   resolution pays the notional exactly), so the portfolio realises the PnL and
   the position is closed.

Everything here is still PAPER ONLY: it observes public market data and records
simulated settlement fills. It never sends an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .config import Config
from .logger import get_logger
from .models import SELL, Order, Position, utcnow
from .polymarket_client import _to_float, parse_json_field

log = get_logger("resolution")

# A market is considered resolved when the winner is at/above this price and the
# loser at/below (1 - this). Anything in between is still "live" / ambiguous.
_WIN_THRESHOLD = 0.99


class _MarketSource(Protocol):
    def get_market(self, market_id: str) -> dict[str, Any] | None: ...


@dataclass
class Resolution:
    market_id: str
    slug: str
    outcome: str
    token_id: str
    settle_price: float          # 1.0 if this outcome won, 0.0 if it lost
    won: bool


def detect_resolution(market: dict[str, Any] | None, outcome: str) -> Resolution | None:
    """Return a :class:`Resolution` for ``outcome`` if ``market`` has resolved.

    Returns ``None`` when the market is missing, still open, or its prices are
    not yet a clear 0/1 split.
    """
    if not market:
        return None

    closed = bool(market.get("closed"))
    uma = str(market.get("umaResolutionStatus") or "").lower()
    if not closed and uma != "resolved":
        return None

    outcomes = [str(o).strip().upper() for o in parse_json_field(market.get("outcomes"))]
    prices = [(_to_float(p) or 0.0) for p in parse_json_field(market.get("outcomePrices"))]
    if not outcomes or len(outcomes) != len(prices):
        return None

    # Require an unambiguous resolution: a clear winner near 1 and loser near 0.
    if max(prices) < _WIN_THRESHOLD or min(prices) > (1.0 - _WIN_THRESHOLD):
        return None

    target = outcome.strip().upper()
    if target not in outcomes:
        return None
    idx = outcomes.index(target)
    settle_price = 1.0 if prices[idx] >= _WIN_THRESHOLD else 0.0
    return Resolution(
        market_id=str(market.get("id") or market.get("conditionId") or ""),
        slug=str(market.get("slug") or ""),
        outcome=target,
        token_id="",
        settle_price=settle_price,
        won=settle_price >= _WIN_THRESHOLD,
    )


class MarketResolver:
    """Builds settlement orders for resolved markets we still hold."""

    def __init__(self, config: Config, client: _MarketSource):
        self.config = config
        self.client = client

    def settlement_orders(self, positions: list[Position]) -> list[Order]:
        orders: list[Order] = []
        if not self.config.settle_resolved:
            return orders
        # One Gamma call per distinct market, not per position.
        markets: dict[str, dict[str, Any] | None] = {}
        for pos in positions:
            if pos.shares <= 1e-9:
                continue
            if pos.market_id not in markets:
                markets[pos.market_id] = self.client.get_market(pos.market_id)
            res = detect_resolution(markets[pos.market_id], pos.outcome)
            if res is None:
                continue
            orders.append(self._settlement_order(pos, res.settle_price, res.won))
        return orders

    def _settlement_order(self, pos: Position, settle_price: float, won: bool) -> Order:
        value = pos.shares * settle_price
        verdict = "won (→1.0)" if won else "lost (→0.0)"
        return Order(
            market_id=pos.market_id,
            slug=pos.slug,
            outcome=pos.outcome,
            token_id=pos.token_id,
            side=SELL,
            requested_price=settle_price,
            fill_price=settle_price,   # resolution pays exactly 0/1, no slippage
            shares=pos.shares,
            value_usdc=value,
            slippage=0.0,
            fee=0.0,
            status="FILLED",
            reason=f"settlement: market resolved, {pos.outcome} {verdict}",
            timestamp=utcnow(),
        )
