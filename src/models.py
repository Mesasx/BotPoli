"""Domain models shared across the bot.

These are plain dataclasses (no ORM) to keep the data layer transparent and the
modules decoupled. Money values are USDC; prices are in [0, 1] USDC per share, as
returned by Polymarket; ``shares`` is the outcome-token quantity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# Order / signal sides
BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"


@dataclass
class MarketSnapshot:
    """A point-in-time view of a single tradable outcome token."""

    market_id: str
    slug: str
    question: str
    category: str
    outcome: str            # "YES" / "NO"
    token_id: str
    best_bid: float
    best_ask: float
    midpoint: float
    spread: float
    volume: float
    liquidity: float
    end_date: datetime | None
    hours_to_close: float | None
    timestamp: datetime = field(default_factory=utcnow)
    # Recent adverse/favourable move in the mark price, as a fraction
    # (e.g. -0.30 = mid dropped 30% over the trend window). Computed per cycle
    # from stored history; transient (not persisted). ``None`` = unknown.
    trend: float | None = None
    # Recent average midpoint over the signal window — a "reference level" the
    # value strategy compares the current ask against. Transient; ``None`` until
    # enough history has been collected.
    ref_price: float | None = None

    @property
    def mid(self) -> float:
        """Best available mark price (midpoint, falling back to last/avg of bid-ask)."""
        if self.midpoint and self.midpoint > 0:
            return self.midpoint
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2.0
        return self.best_bid or self.best_ask or 0.0


@dataclass
class Signal:
    market_id: str
    slug: str
    outcome: str
    token_id: str
    signal_type: str        # BUY / SELL / HOLD
    price: float            # reference price (best_ask for buys, best_bid for sells)
    reason: str
    approved: bool = False
    reject_reason: str = ""
    size_usdc: float = 0.0
    timestamp: datetime = field(default_factory=utcnow)


@dataclass
class Order:
    """A simulated (paper) fill."""

    market_id: str
    slug: str
    outcome: str
    token_id: str
    side: str               # BUY / SELL
    requested_price: float
    fill_price: float       # price after slippage
    shares: float
    value_usdc: float       # fill_price * shares
    slippage: float         # absolute price slippage applied
    fee: float
    status: str             # FILLED / REJECTED
    reason: str = ""
    timestamp: datetime = field(default_factory=utcnow)


@dataclass
class Position:
    market_id: str
    slug: str
    outcome: str
    token_id: str
    shares: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    mark_price: float = 0.0
    status: str = "open"    # open / closed
    opened_at: datetime = field(default_factory=utcnow)
    closed_at: datetime | None = None

    @property
    def cost_basis(self) -> float:
        return self.shares * self.avg_price

    @property
    def market_value(self) -> float:
        return self.shares * self.mark_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.mark_price - self.avg_price) * self.shares

    @property
    def return_pct(self) -> float:
        if self.avg_price <= 0:
            return 0.0
        return (self.mark_price - self.avg_price) / self.avg_price


@dataclass
class EquitySnapshot:
    timestamp: datetime
    cash: float
    positions_value: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    open_positions: int
    exposure: float
    drawdown: float
