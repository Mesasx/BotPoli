"""Offline backtesting over recorded market snapshots.

Replays the ``market_snapshots`` history stored in SQLite through the *same*
strategy / risk / execution components used live, so a strategy can be evaluated
on real recorded prices without touching the network. It is deterministic and
self-contained.

Cycle reconstruction: snapshots are grouped into "cycles" by gaps in their
timestamps (within-cycle snapshots are written microseconds apart; cycles are
spaced by ``POLL_INTERVAL_SECONDS``). Within a cycle the latest snapshot per
token wins.

Scope: this models entries/exits and execution realism (slippage/fees) over the
recorded prices. It does NOT model market resolution (settle 0/1) — that depends
on live Gamma resolution data, not on the price history — nor does it place any
order. Paper-only, like the rest of the project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import Config
from .logger import get_logger
from .models import BUY, HOLD, SELL, MarketSnapshot
from .paper_executor import PaperExecutor
from .portfolio import Portfolio
from .risk_manager import RiskManager
from .storage import week_start
from .strategy import Strategy, build_strategy

log = get_logger("backtest")


@dataclass
class BacktestResult:
    cycles: int
    initial_balance: float
    final_equity: float
    total_pnl: float
    realized_pnl: float
    unrealized_pnl: float
    n_buys: int
    n_sells: int
    wins: int
    losses: int
    win_rate: float
    max_drawdown: float
    open_positions: int
    rejected: int
    equity_curve: list[float] = field(default_factory=list)

    def to_text(self) -> str:
        def m(x: float) -> str:
            return f"{x:,.2f}"

        return "\n".join([
            "=== Backtest result ===",
            f"Cycles replayed : {self.cycles}",
            f"Initial balance : {m(self.initial_balance)} USDC",
            f"Final equity    : {m(self.final_equity)} USDC",
            f"Total PnL       : {m(self.total_pnl)} USDC "
            f"({(self.total_pnl / self.initial_balance * 100) if self.initial_balance else 0:+.1f}%)",
            f"Realized PnL    : {m(self.realized_pnl)} USDC",
            f"Unrealized PnL  : {m(self.unrealized_pnl)} USDC",
            f"Buys / Sells    : {self.n_buys} / {self.n_sells}",
            f"Wins / Losses   : {self.wins} / {self.losses} "
            f"(win rate {self.win_rate * 100:.1f}%)",
            f"Rejected buys   : {self.rejected}",
            f"Max drawdown    : {self.max_drawdown * 100:.1f}%",
            f"Open at end     : {self.open_positions}",
        ])


def group_into_cycles(
    history: list[MarketSnapshot], gap_seconds: float = 5.0
) -> list[list[MarketSnapshot]]:
    """Bucket an id-ordered snapshot list into cycles by timestamp gaps.

    A new cycle starts whenever the gap to the previous snapshot exceeds
    ``gap_seconds``. Within a cycle, the latest snapshot per token is kept.
    """
    if not history:
        return []
    cycles: list[list[MarketSnapshot]] = []
    current: dict[str, MarketSnapshot] = {}
    prev_ts: datetime | None = None
    for snap in history:
        ts = snap.timestamp
        if prev_ts is not None and (ts - prev_ts).total_seconds() > gap_seconds:
            cycles.append(list(current.values()))
            current = {}
        current[snap.token_id] = snap
        prev_ts = ts
    if current:
        cycles.append(list(current.values()))
    return cycles


class Backtester:
    """Runs a strategy over recorded snapshot cycles using the live components."""

    def __init__(self, config: Config, strategy: Strategy | None = None):
        self.config = config
        self.strategy = strategy or build_strategy("simple_threshold", config)
        self.risk = RiskManager(config)
        self.executor = PaperExecutor(config)

    def run(
        self, history: list[MarketSnapshot], gap_seconds: float = 5.0
    ) -> BacktestResult:
        cfg = self.config
        cycles = group_into_cycles(history, gap_seconds)
        portfolio = Portfolio(cfg.initial_balance)

        price_hist: dict[str, list[float]] = {}
        realized_by_day: dict[str, float] = {}
        realized_by_week: dict[datetime, float] = {}
        n_buys = n_sells = n_rejected = 0
        peak = cfg.initial_balance
        max_dd = 0.0
        equity_curve: list[float] = []

        for cycle in cycles:
            cycle_ts = max((s.timestamp for s in cycle), default=None)
            day_key = cycle_ts.strftime("%Y-%m-%d") if cycle_ts else ""
            wk_key = week_start(cycle_ts) if cycle_ts else week_start()
            daily_pnl = realized_by_day.get(day_key, 0.0)
            weekly_pnl = realized_by_week.get(wk_key, 0.0)

            # Update trend annotations from accumulated per-token history.
            for snap in cycle:
                hist = price_hist.setdefault(snap.token_id, [])
                if len(hist) >= 2 and hist[0] > 0:
                    snap.trend = (snap.mid - hist[0]) / hist[0]
                hist.append(snap.mid)
                if len(hist) > cfg.trend_window:
                    del hist[0]

            portfolio.mark_to_market({s.token_id: s.mid for s in cycle})

            for snap in cycle:
                position = portfolio.get_position(snap.token_id)
                signal = self.strategy.generate(snap, position)
                if signal.signal_type == HOLD:
                    continue
                decision = self.risk.evaluate(
                    signal, snap, portfolio, daily_pnl, weekly_pnl
                )
                if not decision.approved:
                    n_rejected += 1
                    continue
                order = None
                if signal.signal_type == BUY:
                    order = self.executor.execute_buy(snap, decision.size_usdc)
                elif signal.signal_type == SELL and position is not None:
                    order = self.executor.execute_sell(snap, position)
                if order is None or order.status != "FILLED":
                    continue
                delta = portfolio.apply_order(order)
                if order.side == BUY:
                    n_buys += 1
                else:
                    n_sells += 1
                    daily_pnl += delta
                    weekly_pnl += delta
                    realized_by_day[day_key] = daily_pnl
                    realized_by_week[wk_key] = weekly_pnl

            portfolio.mark_to_market({s.token_id: s.mid for s in cycle})
            equity = portfolio.equity()
            equity_curve.append(equity)
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)

        final_equity = portfolio.equity()
        return BacktestResult(
            cycles=len(cycles),
            initial_balance=cfg.initial_balance,
            final_equity=final_equity,
            total_pnl=final_equity - cfg.initial_balance,
            realized_pnl=portfolio.realized_pnl,
            unrealized_pnl=portfolio.unrealized_pnl(),
            n_buys=n_buys,
            n_sells=n_sells,
            wins=sum(1 for t in portfolio.closed_trades if t.realized_pnl > 0),
            losses=sum(1 for t in portfolio.closed_trades if t.realized_pnl <= 0),
            win_rate=portfolio.win_rate(),
            max_drawdown=max_dd,
            open_positions=portfolio.open_position_count(),
            rejected=n_rejected,
            equity_curve=equity_curve,
        )
