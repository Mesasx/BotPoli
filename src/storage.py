"""SQLite persistence layer.

Everything the bot observes or does is recorded: market snapshots, signals,
simulated orders, position state and equity snapshots. The portfolio is rebuilt
from the ``orders`` table, so the database is the durable source of truth.

Uses the stdlib ``sqlite3`` driver (no ORM) for transparency. DataFrame getters
back the dashboard and CSV export.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from .config import Config
from .logger import get_logger
from .models import EquitySnapshot, MarketSnapshot, Order, Signal, iso
from .portfolio import Portfolio

log = get_logger("storage")

_PAPER_TABLES = ["signals", "orders", "equity_snapshots", "positions"]
_ALL_TABLES = ["market_snapshots", *_PAPER_TABLES]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


class Storage:
    def __init__(self, config: Config):
        self.config = config
        self.db_path: Path = config.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.init_db()

    # -- schema -------------------------------------------------------------
    def init_db(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, market_id TEXT, slug TEXT, question TEXT, category TEXT,
                outcome TEXT, token_id TEXT, best_bid REAL, best_ask REAL,
                midpoint REAL, spread REAL, volume REAL, liquidity REAL,
                end_date TEXT, hours_to_close REAL, signal TEXT
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, market_id TEXT, slug TEXT, outcome TEXT, token_id TEXT,
                signal_type TEXT, price REAL, reason TEXT,
                approved INTEGER, reject_reason TEXT, size_usdc REAL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, market_id TEXT, slug TEXT, outcome TEXT, token_id TEXT,
                side TEXT, requested_price REAL, fill_price REAL, shares REAL,
                value_usdc REAL, slippage REAL, fee REAL, status TEXT, reason TEXT
            );

            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, cash REAL, positions_value REAL, equity REAL,
                realized_pnl REAL, unrealized_pnl REAL, open_positions INTEGER,
                exposure REAL, drawdown REAL
            );

            CREATE TABLE IF NOT EXISTS positions (
                token_id TEXT PRIMARY KEY,
                market_id TEXT, slug TEXT, outcome TEXT, shares REAL,
                avg_price REAL, realized_pnl REAL, mark_price REAL,
                status TEXT, opened_at TEXT, closed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
            CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
            CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts);
            """
        )
        self.conn.commit()

    # -- writes -------------------------------------------------------------
    def save_market_snapshots(
        self, snaps: Iterable[MarketSnapshot], signals: dict[str, str] | None = None
    ) -> None:
        signals = signals or {}
        rows = [
            (
                iso(s.timestamp), s.market_id, s.slug, s.question, s.category,
                s.outcome, s.token_id, s.best_bid, s.best_ask, s.midpoint,
                s.spread, s.volume, s.liquidity, iso(s.end_date), s.hours_to_close,
                signals.get(s.token_id, ""),
            )
            for s in snaps
        ]
        self.conn.executemany(
            """INSERT INTO market_snapshots
               (ts, market_id, slug, question, category, outcome, token_id,
                best_bid, best_ask, midpoint, spread, volume, liquidity,
                end_date, hours_to_close, signal)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.conn.commit()

    def save_signal(self, s: Signal) -> None:
        self.conn.execute(
            """INSERT INTO signals
               (ts, market_id, slug, outcome, token_id, signal_type, price,
                reason, approved, reject_reason, size_usdc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (iso(s.timestamp), s.market_id, s.slug, s.outcome, s.token_id,
             s.signal_type, s.price, s.reason, int(s.approved), s.reject_reason,
             s.size_usdc),
        )
        self.conn.commit()

    def save_order(self, o: Order) -> None:
        self.conn.execute(
            """INSERT INTO orders
               (ts, market_id, slug, outcome, token_id, side, requested_price,
                fill_price, shares, value_usdc, slippage, fee, status, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (iso(o.timestamp), o.market_id, o.slug, o.outcome, o.token_id, o.side,
             o.requested_price, o.fill_price, o.shares, o.value_usdc, o.slippage,
             o.fee, o.status, o.reason),
        )
        self.conn.commit()

    def save_equity_snapshot(self, e: EquitySnapshot) -> None:
        self.conn.execute(
            """INSERT INTO equity_snapshots
               (ts, cash, positions_value, equity, realized_pnl, unrealized_pnl,
                open_positions, exposure, drawdown)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (iso(e.timestamp), e.cash, e.positions_value, e.equity, e.realized_pnl,
             e.unrealized_pnl, e.open_positions, e.exposure, e.drawdown),
        )
        self.conn.commit()

    def sync_positions(self, portfolio: Portfolio) -> None:
        """Overwrite the positions table with the portfolio's current state."""
        self.conn.execute("DELETE FROM positions")
        rows = [
            (p.token_id, p.market_id, p.slug, p.outcome, p.shares, p.avg_price,
             p.realized_pnl, p.mark_price, p.status, iso(p.opened_at), iso(p.closed_at))
            for p in portfolio.positions.values()
        ]
        self.conn.executemany(
            """INSERT INTO positions
               (token_id, market_id, slug, outcome, shares, avg_price, realized_pnl,
                mark_price, status, opened_at, closed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.conn.commit()

    # -- reads --------------------------------------------------------------
    def load_orders(self) -> list[Order]:
        cur = self.conn.execute("SELECT * FROM orders WHERE status='FILLED' ORDER BY id ASC")
        orders = []
        for r in cur.fetchall():
            orders.append(
                Order(
                    market_id=r["market_id"], slug=r["slug"], outcome=r["outcome"],
                    token_id=r["token_id"], side=r["side"],
                    requested_price=r["requested_price"], fill_price=r["fill_price"],
                    shares=r["shares"], value_usdc=r["value_usdc"], slippage=r["slippage"],
                    fee=r["fee"], status=r["status"], reason=r["reason"] or "",
                    timestamp=_parse_dt(r["ts"]) or datetime.now(timezone.utc),
                )
            )
        return orders

    def load_portfolio(self) -> Portfolio:
        portfolio = Portfolio.from_orders(self.config.initial_balance, self.load_orders())
        # Restore last-known mark prices from the positions table when present.
        cur = self.conn.execute("SELECT token_id, mark_price FROM positions")
        marks = {r["token_id"]: r["mark_price"] for r in cur.fetchall()}
        portfolio.mark_to_market({k: v for k, v in marks.items() if v})
        return portfolio

    def peak_equity(self) -> float:
        cur = self.conn.execute("SELECT MAX(equity) AS m FROM equity_snapshots")
        row = cur.fetchone()
        return row["m"] if row and row["m"] is not None else self.config.initial_balance

    def daily_realized_pnl(self, day: datetime | None = None) -> float:
        """Sum realised PnL from SELL orders for a given UTC day (default today)."""
        day = day or datetime.now(timezone.utc)
        prefix = day.strftime("%Y-%m-%d")
        # Replay all orders so realised PnL is computed against the running avg price,
        # then keep only the deltas from sells that happened on the target day.
        portfolio = Portfolio(self.config.initial_balance)
        realized = 0.0
        for o in self.load_orders():
            delta = portfolio.apply_order(o)
            ts = iso(o.timestamp) or ""
            if o.side == "SELL" and ts.startswith(prefix):
                realized += delta
        return realized

    def get_df(self, table: str) -> pd.DataFrame:
        if table not in _ALL_TABLES:
            raise ValueError(f"unknown table {table}")
        return pd.read_sql_query(f"SELECT * FROM {table}", self.conn)

    def latest_market_snapshots(self) -> pd.DataFrame:
        """Most recent snapshot row per token_id."""
        query = """
            SELECT ms.* FROM market_snapshots ms
            JOIN (SELECT token_id, MAX(id) AS max_id
                  FROM market_snapshots GROUP BY token_id) latest
            ON ms.id = latest.max_id
        """
        return pd.read_sql_query(query, self.conn)

    # -- maintenance --------------------------------------------------------
    def export_csv(self, out_dir: Path | None = None) -> list[Path]:
        out_dir = out_dir or (self.db_path.parent / "exports")
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        for table in _ALL_TABLES:
            df = self.get_df(table)
            path = out_dir / f"{table}_{stamp}.csv"
            df.to_csv(path, index=False)
            written.append(path)
        return written

    def reset_paper(self) -> None:
        """Clear all paper-trading state (keeps market_snapshots history)."""
        for table in _PAPER_TABLES:
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()
        log.info("Paper trading state reset.")

    def close(self) -> None:
        self.conn.close()
