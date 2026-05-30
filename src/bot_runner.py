"""Background bot runner for the live supervision platform.

Runs the paper-trading engine in a dedicated thread so the bot can operate
*continuously* while the web layer serves a live view and control commands.

Design notes:
* All SQLite access happens inside the runner thread (sqlite connections are not
  shareable across threads). The web layer never touches the DB directly — it
  reads an in-memory **snapshot** (under a lock) and submits **commands** through
  a thread-safe queue that the runner processes between cycles.
* The loop never crashes the process: every cycle is wrapped, errors are logged
  and surfaced as alerts, and the loop keeps going.
* Two cadences are decoupled: the engine polls Polymarket at a safe interval
  (``poll_interval_seconds``); the web view can refresh much faster because it
  only reads the cached snapshot.

PAPER ONLY: the runner just drives the existing paper components. There is no
path to real trading here.
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import UTC, datetime
from typing import Any

from .config import Config, load_config
from .logger import get_logger, get_recent_logs
from .main import PaperEngine
from .models import iso
from .polymarket_client import PolymarketClient
from .risk_manager import RiskManager
from .runtime_config import build_config, editable_values, runtime_config_path, save_overrides
from .storage import Storage

log = get_logger("runner")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BotRunner:
    """Owns the engine thread, the control queue and the published snapshot."""

    def __init__(self, base_config: Config | None = None):
        self.base_config = base_config or load_config()
        self.config_path = runtime_config_path(self.base_config)
        self.config = build_config(self.config_path, self.base_config)

        self._cmd: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self.status = "stopped"          # stopped | running | paused
        self._last_ok: datetime | None = None
        self._last_error: str | None = None
        self._consecutive_empty = 0
        self._cycles = 0
        self._snapshot: dict[str, Any] = {"status": "stopped"}

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.status = "running"
        self._thread = threading.Thread(target=self._run, name="bot-runner", daemon=True)
        self._thread.start()
        log.info("Bot runner started (poll=%ss).", self.config.poll_interval_seconds)

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        log.info("Bot runner stopped.")

    # -- control (called from the web thread) -------------------------------
    def pause(self) -> None:
        self._cmd.put(("pause", None))

    def resume(self) -> None:
        self._cmd.put(("resume", None))

    def reset(self) -> None:
        self._cmd.put(("reset", None))

    def close_position(self, match: str) -> None:
        self._cmd.put(("close", match))

    def update_config(self, overrides: dict[str, Any]) -> None:
        self._cmd.put(("config", overrides))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snap = dict(self._snapshot)
        snap["logs"] = get_recent_logs(120)
        snap["server_time"] = iso(_utcnow())
        return snap

    def config_values(self) -> dict[str, Any]:
        return editable_values(self.config)

    # -- engine thread ------------------------------------------------------
    def _run(self) -> None:
        storage = Storage(self.config)
        client = PolymarketClient(self.config)
        engine = PaperEngine(self.config, storage, client)

        # Publish an initial snapshot immediately so the UI isn't blank.
        self._publish(storage, None)

        while not self._stop.is_set():
            engine, storage, client = self._drain_commands(engine, storage, client)

            if self.status == "paused":
                self._publish(storage, None)
                self._interruptible_sleep(2.0)
                continue

            summary: dict[str, Any] | None = None
            try:
                summary = engine.run_cycle()
                self._cycles += 1
                self._last_ok = _utcnow()
                self._last_error = None
                self._consecutive_empty = (
                    self._consecutive_empty + 1 if summary.get("tokens", 0) == 0 else 0
                )
            except Exception as exc:  # never let the loop die
                self._last_error = str(exc)
                log.exception("Cycle failed: %s", exc)

            self._publish(storage, summary)
            self._interruptible_sleep(float(self.config.poll_interval_seconds))

        storage.close()
        client.close()

    def _drain_commands(self, engine, storage, client):
        while True:
            try:
                name, payload = self._cmd.get_nowait()
            except queue.Empty:
                break
            try:
                if name == "pause":
                    self.status = "paused"
                    log.info("Bot paused by user.")
                elif name == "resume":
                    self.status = "running"
                    log.info("Bot resumed by user.")
                elif name == "reset":
                    storage.reset_paper()
                    log.info("Paper account reset by user.")
                elif name == "close":
                    n = engine.force_close(str(payload))
                    log.info("Manual close '%s': %d position(s).", payload, n)
                elif name == "config":
                    engine, storage, client = self._apply_config(payload, storage, client)
            except Exception as exc:  # commands must not kill the loop
                self._last_error = f"command {name} failed: {exc}"
                log.exception("Command %s failed: %s", name, exc)
        return engine, storage, client

    def _apply_config(self, overrides: dict[str, Any], storage, client):
        merged = {**editable_values(self.config), **overrides}
        save_overrides(self.config_path, merged)
        self.config = build_config(self.config_path, self.base_config)
        # Rebuild engine/storage/client so the new config takes effect.
        storage.close()
        client.close()
        storage = Storage(self.config)
        client = PolymarketClient(self.config)
        engine = PaperEngine(self.config, storage, client)
        log.info("Configuration updated by user.")
        return engine, storage, client

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in small steps so commands/stop are handled promptly."""
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            if self._stop.is_set() or not self._cmd.empty():
                return
            time.sleep(0.2)

    # -- snapshot building --------------------------------------------------
    def _publish(self, storage: Storage, summary: dict[str, Any] | None) -> None:
        snap = build_state(storage, self.config, self)
        with self._lock:
            self._snapshot = snap


# ---------------------------------------------------------------------------
# Snapshot builder (runs in the runner thread; safe DB access)
# ---------------------------------------------------------------------------
def build_state(storage: Storage, config: Config, runner: BotRunner | None) -> dict[str, Any]:
    portfolio = storage.load_portfolio()
    markets_df = storage.latest_market_snapshots()
    marks: dict[str, float] = {}
    if not markets_df.empty:
        marks = {str(t): m for t, m in zip(markets_df["token_id"], markets_df["midpoint"]) if m}
        portfolio.mark_to_market(marks)

    equity = portfolio.equity()
    invested = portfolio.positions_value()
    available = portfolio.cash
    total_pnl = equity - config.initial_balance
    weekly_pnl = storage.weekly_realized_pnl()
    daily_pnl = storage.daily_realized_pnl()
    peak = max(storage.peak_equity(), equity)
    drawdown = (peak - equity) / peak if peak > 0 else 0.0

    halted = RiskManager(config).trading_halted(weekly_pnl)
    status = runner.status if runner else "stopped"
    display_status = "halted" if (halted and status == "running") else status

    positions = [
        {
            "slug": p.slug, "outcome": p.outcome, "token_id": p.token_id,
            "shares": round(p.shares, 4), "entry_price": round(p.avg_price, 4),
            "current_price": round(p.mark_price, 4),
            "pnl": round(p.unrealized_pnl, 4), "return_pct": round(p.return_pct * 100, 2),
            "opened_at": iso(p.opened_at),
        }
        for p in portfolio.open_positions()
    ]
    closed: list[dict[str, Any]] = [
        {
            "slug": t.slug, "outcome": t.outcome, "shares": round(t.shares, 4),
            "avg_price": round(t.avg_price, 4), "exit_price": round(t.exit_price, 4),
            "pnl": round(t.realized_pnl, 4),
        }
        for t in portfolio.closed_trades
    ]
    ranked = sorted(closed, key=lambda t: float(t["pnl"]), reverse=True)
    best = ranked[:3]
    worst = [t for t in reversed(ranked) if float(t["pnl"]) <= 0][:3]

    markets: list[dict[str, Any]] = []
    if not markets_df.empty:
        for _, r in markets_df.head(40).iterrows():
            markets.append({
                "slug": r["slug"], "outcome": r["outcome"],
                "midpoint": round(float(r["midpoint"] or 0), 4),
                "spread": round(float(r["spread"] or 0), 4),
                "liquidity": round(float(r["liquidity"] or 0), 0),
                "signal": r["signal"] if "signal" in markets_df.columns else "",
            })

    alerts = _build_alerts(runner, config, portfolio, weekly_pnl, daily_pnl, halted)

    return {
        "status": display_status,
        "halted": halted,
        "last_ok": iso(runner._last_ok) if runner and runner._last_ok else None,
        "last_error": runner._last_error if runner else None,
        "cycles": runner._cycles if runner else 0,
        "stale": _is_stale(runner, config),
        "kpis": {
            "initial_balance": round(config.initial_balance, 2),
            "equity": round(equity, 2),
            "available": round(available, 2),
            "invested": round(invested, 2),
            "total_pnl": round(total_pnl, 2),
            "weekly_pnl": round(weekly_pnl, 2),
            "daily_pnl": round(daily_pnl, 2),
            "drawdown": round(drawdown * 100, 2),
            "win_rate": round(portfolio.win_rate() * 100, 1),
            "open_positions": portfolio.open_position_count(),
            "closed_trades": len(portfolio.closed_trades),
        },
        "equity_curve": _equity_curve(storage),
        "positions": positions,
        "closed": list(reversed(closed))[:25],
        "best": best,
        "worst": worst,
        "markets": markets,
        "alerts": alerts,
        "config": editable_values(config),
        "poll_interval": config.poll_interval_seconds,
    }


def _equity_curve(storage: Storage, limit: int = 500) -> list[dict[str, Any]]:
    df = storage.get_df("equity_snapshots")
    if df.empty:
        return []
    df = df.tail(limit)
    return [{"ts": str(r["ts"]), "equity": float(r["equity"])} for _, r in df.iterrows()]


def _is_stale(runner: BotRunner | None, config: Config) -> bool:
    if runner is None or runner.status != "running" or runner._last_ok is None:
        return False
    age = (_utcnow() - runner._last_ok).total_seconds()
    return age > max(3 * config.poll_interval_seconds, 90)


def _build_alerts(runner, config, portfolio, weekly_pnl, daily_pnl, halted) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    if halted:
        alerts.append({"level": "danger",
                       "text": f"Stop semanal alcanzado ({weekly_pnl:.2f} USDC): "
                               "no se abren nuevas posiciones hasta la próxima semana."})
    if daily_pnl <= -config.max_daily_loss:
        alerts.append({"level": "warning",
                       "text": f"Stop diario alcanzado ({daily_pnl:.2f} USDC)."})
    if runner and _is_stale(runner, config):
        alerts.append({"level": "danger",
                       "text": "Sin datos recientes de Polymarket: posible problema de conexión."})
    if runner and runner._consecutive_empty >= 3:
        alerts.append({"level": "warning",
                       "text": "Varios ciclos sin mercados: revisa la conectividad o los filtros."})
    if runner and runner._last_error:
        alerts.append({"level": "warning", "text": f"Último error: {runner._last_error}"})
    if portfolio.cash < -1e-6:
        alerts.append({"level": "danger", "text": "Cash ficticio negativo: revisar contabilidad."})
    return alerts
