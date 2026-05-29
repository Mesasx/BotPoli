"""Command-line entry point and paper-trading engine.

Commands:
    python -m src.main scan          # scan & print markets, store snapshots
    python -m src.main paper         # run paper-trading cycles (loop)
    python -m src.main paper --once  # run a single cycle
    python -m src.main status        # print portfolio summary
    python -m src.main report        # generate the weekly report (--save to persist)
    python -m src.main dashboard     # launch the Streamlit dashboard
    python -m src.main export        # export all tables to CSV
    python -m src.main reset-paper   # wipe paper-trading state
    python -m src.main close SLUG    # manually close positions matching a slug/token
"""

from __future__ import annotations

import argparse
import sys
import time

from .config import Config, load_config
from .logger import get_logger, setup_logging
from .market_scanner import MarketScanner
from .models import BUY, HOLD, SELL, EquitySnapshot, MarketSnapshot, Order, utcnow
from .paper_executor import PaperExecutor
from .polymarket_client import PolymarketClient
from .report import generate_weekly_report, save_report
from .resolution import MarketResolver
from .risk_manager import RiskManager
from .storage import Storage
from .strategy import Strategy, build_strategy

log = get_logger("main")


class PaperEngine:
    """Orchestrates one paper-trading cycle: scan -> signal -> risk -> fill -> persist."""

    def __init__(self, config: Config, storage: Storage, client: PolymarketClient,
                 strategy: Strategy | None = None):
        self.config = config
        self.storage = storage
        self.client = client
        self.scanner = MarketScanner(config, client)
        self.strategy = strategy or build_strategy("simple_threshold", config)
        self.risk = RiskManager(config)
        self.executor = PaperExecutor(config)
        self.resolver = MarketResolver(config, client)

    def run_cycle(self) -> dict:
        snapshots = self.scanner.scan(enrich=True, apply_filters=False)
        portfolio = self.storage.load_portfolio()
        daily_pnl = self.storage.daily_realized_pnl()
        weekly_pnl = self.storage.weekly_realized_pnl()

        # --- Settle resolved markets (settle 0/1) before anything else -------
        n_settled = 0
        for settle_order in self.resolver.settlement_orders(portfolio.open_positions()):
            self.storage.save_order(settle_order)
            delta = portfolio.apply_order(settle_order)
            daily_pnl += delta
            weekly_pnl += delta
            n_settled += 1
            log.info("Settled %s [%s] @ %.2f -> realised %.2f",
                     settle_order.slug, settle_order.outcome, settle_order.fill_price, delta)

        snap_by_token = {s.token_id: s for s in snapshots}

        # Ensure we have a snapshot for every open position (for exit logic),
        # even if it was filtered out of / missing from the scan.
        for pos in portfolio.open_positions():
            if pos.token_id not in snap_by_token:
                extra = self._snapshot_for_position(pos)
                if extra is not None:
                    snap_by_token[pos.token_id] = extra

        # Annotate each snapshot with a short-term trend from stored history.
        for token_id, snap in snap_by_token.items():
            snap.trend = self._compute_trend(token_id, snap)

        # Mark portfolio to the latest mids before deciding exits.
        portfolio.mark_to_market({tid: s.mid for tid, s in snap_by_token.items()})

        halted = self.risk.trading_halted(weekly_pnl)
        if halted:
            log.warning("Weekly loss limit reached (%.2f); new buys halted this week.", weekly_pnl)

        signal_labels: dict[str, str] = {}
        n_signals = n_buys = n_sells = n_rejected = 0

        for token_id, snap in snap_by_token.items():
            position = portfolio.get_position(token_id)
            signal = self.strategy.generate(snap, position)
            signal_labels[token_id] = signal.signal_type

            if signal.signal_type == HOLD:
                continue

            decision = self.risk.evaluate(signal, snap, portfolio, daily_pnl, weekly_pnl)
            signal.approved = decision.approved
            signal.reject_reason = "" if decision.approved else decision.reason
            signal.size_usdc = decision.size_usdc
            self.storage.save_signal(signal)
            n_signals += 1

            if not decision.approved:
                n_rejected += 1
                continue

            order: Order | None = None
            if signal.signal_type == BUY:
                order = self.executor.execute_buy(snap, decision.size_usdc)
            elif signal.signal_type == SELL and position is not None:
                order = self.executor.execute_sell(snap, position)

            if order is None:
                continue
            self.storage.save_order(order)
            if order.status == "FILLED":
                delta = portfolio.apply_order(order)
                if order.side == BUY:
                    n_buys += 1
                else:
                    n_sells += 1
                    daily_pnl += delta
                    weekly_pnl += delta

        # Persist market snapshots with their signal labels.
        self.storage.save_market_snapshots(snapshots, signal_labels)

        # Re-mark and record an equity snapshot.
        portfolio.mark_to_market({tid: s.mid for tid, s in snap_by_token.items()})
        equity = portfolio.equity()
        peak = max(self.storage.peak_equity(), equity)
        drawdown = (peak - equity) / peak if peak > 0 else 0.0

        self.storage.save_equity_snapshot(
            EquitySnapshot(
                timestamp=utcnow(),
                cash=portfolio.cash,
                positions_value=portfolio.positions_value(),
                equity=equity,
                realized_pnl=portfolio.realized_pnl,
                unrealized_pnl=portfolio.unrealized_pnl(),
                open_positions=portfolio.open_position_count(),
                exposure=portfolio.total_exposure(),
                drawdown=drawdown,
            )
        )
        self.storage.sync_positions(portfolio)

        summary = {
            "markets": len({s.market_id for s in snapshots}),
            "tokens": len(snap_by_token),
            "signals": n_signals,
            "buys": n_buys,
            "sells": n_sells,
            "settled": n_settled,
            "rejected": n_rejected,
            "halted": halted,
            "equity": equity,
            "cash": portfolio.cash,
            "realized_pnl": portfolio.realized_pnl,
            "unrealized_pnl": portfolio.unrealized_pnl(),
            "weekly_pnl": weekly_pnl,
            "open_positions": portfolio.open_position_count(),
            "drawdown": drawdown,
        }
        return summary

    def _compute_trend(self, token_id: str, snap: MarketSnapshot) -> float | None:
        """Fraction change of the mark price over the recent history window.

        Uses the oldest midpoint in the window as the reference. ``None`` when
        there isn't enough history yet (so trend exits don't fire spuriously).
        """
        if self.config.trend_window <= 1:
            return None
        hist = self.storage.price_history(token_id, self.config.trend_window)
        if len(hist) < 2 or hist[0] <= 0:
            return None
        current = snap.mid or hist[-1]
        return (current - hist[0]) / hist[0]

    def _snapshot_for_position(self, pos) -> MarketSnapshot | None:
        quote = self.client.get_quote(pos.token_id)
        if quote["best_bid"] is None and quote["midpoint"] is None:
            return None
        best_bid = quote["best_bid"] or 0.0
        best_ask = quote["best_ask"] or 0.0
        midpoint = quote["midpoint"] or ((best_bid + best_ask) / 2.0 if (best_bid or best_ask) else 0.0)
        spread = quote["spread"] or max(0.0, best_ask - best_bid)
        return MarketSnapshot(
            market_id=pos.market_id, slug=pos.slug, question="", category="",
            outcome=pos.outcome, token_id=pos.token_id, best_bid=best_bid,
            best_ask=best_ask, midpoint=midpoint, spread=spread, volume=0.0,
            liquidity=0.0, end_date=None, hours_to_close=None,
        )

    def force_close(self, match: str) -> int:
        """Manually close any open position whose slug or token_id matches ``match``."""
        portfolio = self.storage.load_portfolio()
        closed = 0
        for pos in portfolio.open_positions():
            if match.lower() in pos.slug.lower() or match == pos.token_id:
                snap = self._snapshot_for_position(pos)
                if snap is None:
                    log.warning("No price for %s; cannot close.", pos.slug)
                    continue
                order = self.executor.execute_sell(snap, pos)
                self.storage.save_order(order)
                if order.status == "FILLED":
                    portfolio.apply_order(order)
                    closed += 1
                    log.info("Closed %s (%s) @ %.3f", pos.slug, pos.outcome, order.fill_price)
        self.storage.sync_positions(portfolio)
        return closed


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------
def _fmt_money(x: float) -> str:
    return f"{x:,.2f}"


def cmd_scan(cfg: Config) -> None:
    from tabulate import tabulate

    with PolymarketClient(cfg) as client:
        scanner = MarketScanner(cfg, client)
        snaps = scanner.scan(enrich=True, apply_filters=True)
        storage = Storage(cfg)
        try:
            storage.save_market_snapshots(snaps)
        finally:
            storage.close()

    snaps.sort(key=lambda s: s.volume, reverse=True)
    rows = [
        [s.slug[:34], s.outcome, f"{s.volume:,.0f}", f"{s.liquidity:,.0f}",
         f"{s.best_bid:.3f}", f"{s.best_ask:.3f}", f"{s.midpoint:.3f}",
         f"{s.spread:.3f}", f"{s.hours_to_close:.0f}h" if s.hours_to_close else "n/a"]
        for s in snaps[:40]
    ]
    print(tabulate(
        rows,
        headers=["market", "out", "volume", "liquidity", "bid", "ask", "mid", "spread", "close"],
        tablefmt="github",
    ))
    print(f"\n{len(snaps)} tradable outcome tokens scanned and stored.")


def cmd_paper(cfg: Config, once: bool, iterations: int | None) -> None:
    storage = Storage(cfg)
    with PolymarketClient(cfg) as client:
        engine = PaperEngine(cfg, storage, client)
        count = 0
        try:
            while True:
                count += 1
                summary = engine.run_cycle()
                halt = " HALTED" if summary.get("halted") else ""
                print(
                    f"[cycle {count}] tokens={summary['tokens']} "
                    f"signals={summary['signals']} buys={summary['buys']} "
                    f"sells={summary['sells']} settled={summary['settled']} "
                    f"rejected={summary['rejected']} | "
                    f"equity={_fmt_money(summary['equity'])} "
                    f"realPnL={_fmt_money(summary['realized_pnl'])} "
                    f"unrealPnL={_fmt_money(summary['unrealized_pnl'])} "
                    f"open={summary['open_positions']} dd={summary['drawdown']*100:.1f}%{halt}"
                )
                if once or (iterations is not None and count >= iterations):
                    break
                time.sleep(cfg.poll_interval_seconds)
        except KeyboardInterrupt:
            print("\nStopped by user.")
        finally:
            storage.close()


def cmd_status(cfg: Config) -> None:
    storage = Storage(cfg)
    try:
        portfolio = storage.load_portfolio()
        df = storage.latest_market_snapshots()
        if not df.empty:
            marks = dict(zip(df["token_id"], df["midpoint"]))
            portfolio.mark_to_market({k: v for k, v in marks.items() if v})
        equity = portfolio.equity()
        peak = max(storage.peak_equity(), equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        print("=== Paper Trading Status ===")
        print(f"Initial balance : {_fmt_money(cfg.initial_balance)} USDC")
        print(f"Cash            : {_fmt_money(portfolio.cash)} USDC")
        print(f"Positions value : {_fmt_money(portfolio.positions_value())} USDC")
        print(f"Equity          : {_fmt_money(equity)} USDC")
        print(f"Realized PnL    : {_fmt_money(portfolio.realized_pnl)} USDC")
        print(f"Unrealized PnL  : {_fmt_money(portfolio.unrealized_pnl())} USDC")
        print(f"Total PnL       : {_fmt_money(equity - cfg.initial_balance)} USDC")
        print(f"Open positions  : {portfolio.open_position_count()}")
        print(f"Total exposure  : {_fmt_money(portfolio.total_exposure())} USDC")
        print(f"Closed trades   : {len(portfolio.closed_trades)}")
        print(f"Win rate        : {portfolio.win_rate()*100:.1f}%")
        print(f"Max drawdown    : {dd*100:.1f}%")
        for p in portfolio.open_positions():
            print(f"  - {p.slug[:40]} [{p.outcome}] {p.shares:.1f} sh "
                  f"@ {p.avg_price:.3f} mark {p.mark_price:.3f} "
                  f"PnL {_fmt_money(p.unrealized_pnl)} ({p.return_pct*100:+.1f}%)")
    finally:
        storage.close()


def cmd_report(cfg: Config, save: bool) -> None:
    storage = Storage(cfg)
    try:
        report = generate_weekly_report(storage, cfg)
        print(report.to_markdown())
        if save:
            path = save_report(report, cfg)
            print(f"\nInforme guardado en: {path}")
    finally:
        storage.close()


def cmd_dashboard(cfg: Config) -> None:
    import subprocess
    from pathlib import Path

    dashboard = Path(__file__).resolve().parent / "dashboard.py"
    print(f"Launching Streamlit dashboard: {dashboard}")
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(dashboard)], check=False)


def cmd_export(cfg: Config) -> None:
    storage = Storage(cfg)
    try:
        paths = storage.export_csv()
        print("Exported:")
        for p in paths:
            print(f"  - {p}")
    finally:
        storage.close()


def cmd_reset(cfg: Config, yes: bool) -> None:
    if not yes:
        confirm = input("This wipes all paper-trading state. Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return
    storage = Storage(cfg)
    try:
        storage.reset_paper()
        print("Paper trading state reset.")
    finally:
        storage.close()


def cmd_close(cfg: Config, match: str) -> None:
    storage = Storage(cfg)
    with PolymarketClient(cfg) as client:
        engine = PaperEngine(cfg, storage, client)
        n = engine.force_close(match)
        print(f"Closed {n} position(s) matching '{match}'.")
    storage.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.main", description="Polymarket paper trading bot (paper-only).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="Scan and print active markets")

    p_paper = sub.add_parser("paper", help="Run paper-trading cycles")
    p_paper.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    p_paper.add_argument("--iterations", type=int, default=None, help="Run N cycles then exit")

    sub.add_parser("status", help="Print portfolio status")

    p_report = sub.add_parser("report", help="Generate the weekly paper-trading report")
    p_report.add_argument("--save", action="store_true", help="Also save the report to data/reports/")

    sub.add_parser("dashboard", help="Launch the Streamlit dashboard")
    sub.add_parser("export", help="Export all tables to CSV")

    p_reset = sub.add_parser("reset-paper", help="Wipe paper-trading state")
    p_reset.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    p_close = sub.add_parser("close", help="Manually close positions matching a slug/token")
    p_close.add_argument("match", help="Slug substring or exact token_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config()
    setup_logging(cfg.log_level)

    if args.command == "scan":
        cmd_scan(cfg)
    elif args.command == "paper":
        cmd_paper(cfg, once=args.once, iterations=args.iterations)
    elif args.command == "status":
        cmd_status(cfg)
    elif args.command == "report":
        cmd_report(cfg, save=args.save)
    elif args.command == "dashboard":
        cmd_dashboard(cfg)
    elif args.command == "export":
        cmd_export(cfg)
    elif args.command == "reset-paper":
        cmd_reset(cfg, yes=args.yes)
    elif args.command == "close":
        cmd_close(cfg, args.match)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
