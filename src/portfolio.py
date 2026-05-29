"""In-memory portfolio state machine.

The portfolio is the single source of truth for cash, open positions and realised
PnL. It is rebuilt deterministically by replaying FILLED orders (see
:meth:`from_orders`), which keeps it consistent with the stored history and makes
it trivial to unit-test.

Average-cost accounting is used: buys update the weighted-average price; sells
realise PnL against that average and never change it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .models import BUY, SELL, Order, Position, utcnow


@dataclass
class ClosedTrade:
    token_id: str
    slug: str
    outcome: str
    shares: float
    avg_price: float
    exit_price: float
    realized_pnl: float


class Portfolio:
    def __init__(self, initial_balance: float):
        self.initial_balance = initial_balance
        self.cash = initial_balance
        self.realized_pnl = 0.0
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[ClosedTrade] = []

    # -- queries ------------------------------------------------------------
    def get_position(self, token_id: str) -> Position | None:
        pos = self.positions.get(token_id)
        if pos and pos.shares > 1e-9:
            return pos
        return None

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.shares > 1e-9]

    def open_position_count(self) -> int:
        return len(self.open_positions())

    def market_exposure(self, market_id: str) -> float:
        return sum(
            p.market_value for p in self.open_positions() if p.market_id == market_id
        )

    def total_exposure(self) -> float:
        return sum(p.market_value for p in self.open_positions())

    def positions_value(self) -> float:
        return sum(p.market_value for p in self.open_positions())

    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.open_positions())

    def equity(self) -> float:
        return self.cash + self.positions_value()

    def win_rate(self) -> float:
        if not self.closed_trades:
            return 0.0
        wins = sum(1 for t in self.closed_trades if t.realized_pnl > 0)
        return wins / len(self.closed_trades)

    # -- mutations ----------------------------------------------------------
    def apply_order(self, order: Order) -> float:
        """Apply a FILLED order. Returns the realised PnL delta (0 for buys)."""
        if order.status != "FILLED":
            return 0.0
        if order.side == BUY:
            return self._apply_buy(order)
        if order.side == SELL:
            return self._apply_sell(order)
        return 0.0

    def _apply_buy(self, order: Order) -> float:
        self.cash -= order.value_usdc + order.fee
        pos = self.positions.get(order.token_id)
        if pos is None or pos.shares <= 1e-9:
            pos = Position(
                market_id=order.market_id,
                slug=order.slug,
                outcome=order.outcome,
                token_id=order.token_id,
                shares=order.shares,
                avg_price=order.fill_price,
                mark_price=order.fill_price,
                status="open",
                opened_at=order.timestamp,
            )
            self.positions[order.token_id] = pos
        else:
            new_shares = pos.shares + order.shares
            pos.avg_price = (pos.cost_basis + order.value_usdc) / new_shares
            pos.shares = new_shares
            pos.status = "open"
        return 0.0

    def _apply_sell(self, order: Order) -> float:
        pos = self.positions.get(order.token_id)
        if pos is None or pos.shares <= 1e-9:
            # Cannot sell what we don't hold; ignore defensively.
            return 0.0

        shares_sold = min(order.shares, pos.shares)
        realized = (order.fill_price - pos.avg_price) * shares_sold - order.fee
        self.cash += order.value_usdc - order.fee
        self.realized_pnl += realized
        pos.realized_pnl += realized
        pos.shares -= shares_sold

        if pos.shares <= 1e-9:
            pos.shares = 0.0
            pos.status = "closed"
            pos.closed_at = order.timestamp
            self.closed_trades.append(
                ClosedTrade(
                    token_id=pos.token_id,
                    slug=pos.slug,
                    outcome=pos.outcome,
                    shares=shares_sold,
                    avg_price=pos.avg_price,
                    exit_price=order.fill_price,
                    realized_pnl=pos.realized_pnl,
                )
            )
        return realized

    def mark_to_market(self, prices: dict[str, float]) -> None:
        """Update mark prices for open positions. Unknown tokens keep their avg."""
        for token_id, pos in self.positions.items():
            if pos.shares <= 1e-9:
                continue
            price = prices.get(token_id)
            if price is not None and price > 0:
                pos.mark_price = price
            elif pos.mark_price <= 0:
                pos.mark_price = pos.avg_price

    # -- construction -------------------------------------------------------
    @classmethod
    def from_orders(cls, initial_balance: float, orders: Iterable[Order]) -> "Portfolio":
        portfolio = cls(initial_balance)
        for order in orders:
            portfolio.apply_order(order)
        return portfolio
