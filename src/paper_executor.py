"""Paper (simulated) order execution.

NO real orders are ever sent. This module turns an approved signal into a
:class:`Order` with a realistic simulated fill:

* BUY fills at the best ask, SELL fills at the best bid.
* Adverse slippage is applied (buys pay a bit more, sells receive a bit less).
* Optional fees (``FEE_BPS``) are deducted. Polymarket currently has 0 trading
  fees, so the default is 0, but the hook is here for realism / what-ifs.

Fill prices are clamped to the valid (0, 1) probability range.
"""

from __future__ import annotations

from .config import Config
from .logger import get_logger
from .models import BUY, SELL, MarketSnapshot, Order, Position

log = get_logger("executor")


def _clamp_price(price: float) -> float:
    return min(0.999, max(0.001, price))


class PaperExecutor:
    def __init__(self, config: Config):
        self.config = config

    def execute_buy(self, snap: MarketSnapshot, size_usdc: float) -> Order:
        requested = snap.best_ask
        if requested <= 0 or requested >= 1:
            return self._rejected(snap, BUY, requested, "invalid ask price")
        if size_usdc <= 0:
            return self._rejected(snap, BUY, requested, "non-positive size")

        fill_price = _clamp_price(requested * (1 + self.config.slippage_rate))
        shares = size_usdc / fill_price
        value = shares * fill_price
        fee = value * self.config.fee_rate
        return Order(
            market_id=snap.market_id,
            slug=snap.slug,
            outcome=snap.outcome,
            token_id=snap.token_id,
            side=BUY,
            requested_price=requested,
            fill_price=fill_price,
            shares=shares,
            value_usdc=value,
            slippage=fill_price - requested,
            fee=fee,
            status="FILLED",
            reason="paper buy",
        )

    def execute_sell(self, snap: MarketSnapshot, position: Position) -> Order:
        requested = snap.best_bid or snap.mid
        if requested <= 0 or requested >= 1:
            return self._rejected(snap, SELL, requested, "invalid bid price")
        if position.shares <= 1e-9:
            return self._rejected(snap, SELL, requested, "no shares to sell")

        fill_price = _clamp_price(requested * (1 - self.config.slippage_rate))
        shares = position.shares
        value = shares * fill_price
        fee = value * self.config.fee_rate
        return Order(
            market_id=snap.market_id,
            slug=snap.slug,
            outcome=snap.outcome,
            token_id=snap.token_id,
            side=SELL,
            requested_price=requested,
            fill_price=fill_price,
            shares=shares,
            value_usdc=value,
            slippage=requested - fill_price,
            fee=fee,
            status="FILLED",
            reason="paper sell",
        )

    def _rejected(self, snap: MarketSnapshot, side: str, price: float, reason: str) -> Order:
        log.debug("Order rejected (%s %s): %s", side, snap.slug, reason)
        return Order(
            market_id=snap.market_id,
            slug=snap.slug,
            outcome=snap.outcome,
            token_id=snap.token_id,
            side=side,
            requested_price=price,
            fill_price=0.0,
            shares=0.0,
            value_usdc=0.0,
            slippage=0.0,
            fee=0.0,
            status="REJECTED",
            reason=reason,
        )
