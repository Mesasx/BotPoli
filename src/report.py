"""Automated weekly paper-trading report.

Distils a week of paper trading into a single human-readable digest so the bot
can be reviewed in ~15 minutes: balances, PnL, trade counts, win/loss, drawdown,
exposure by market and category, the best/worst trades, a plain-language
explanation of what drove the result, any anomalies, and a suggested tweak for
the next week.

The report is computed purely from what :mod:`storage` has persisted (orders,
equity snapshots, positions, market snapshots) — it never hits the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .config import Config
from .portfolio import Portfolio
from .storage import Storage, week_start

_BAR = "─" * 60


@dataclass
class TradeEvent:
    """A realised (SELL/settlement) trade with its PnL delta."""

    timestamp: datetime
    slug: str
    outcome: str
    shares: float
    exit_price: float
    realized_pnl: float
    reason: str
    settlement: bool


@dataclass
class WeeklyReport:
    week_start: datetime
    week_end: datetime
    starting_equity: float
    ending_equity: float
    initial_balance: float
    realized_pnl_week: float
    equity_change_week: float
    total_pnl: float
    n_buys: int
    n_sells: int
    wins: int
    losses: int
    win_rate: float
    max_drawdown: float
    total_exposure: float
    open_positions: int
    exposure_by_market: dict[str, float]
    exposure_by_category: dict[str, float]
    best_trades: list[TradeEvent]
    worst_trades: list[TradeEvent]
    settlements: list[TradeEvent]
    explanation: str
    anomalies: list[str]
    recommendation: str
    halted: bool = False
    notes: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        def money(x: float) -> str:
            return f"${x:,.2f}"

        ws = self.week_start.strftime("%Y-%m-%d")
        we = self.week_end.strftime("%Y-%m-%d")
        lines: list[str] = []
        lines.append(f"# 📊 Informe semanal de paper trading ({ws} → {we})")
        lines.append("")
        lines.append("> PAPER TRADING — dinero ficticio, sin órdenes reales.")
        lines.append("")
        lines.append("## Resumen")
        lines.append(f"- Saldo inicial (histórico): {money(self.initial_balance)}")
        lines.append(f"- Equity al empezar la semana: {money(self.starting_equity)}")
        lines.append(f"- Equity al cerrar la semana: {money(self.ending_equity)}")
        lines.append(f"- **PnL semanal (realizado): {money(self.realized_pnl_week)}**")
        lines.append(f"- PnL semanal (cambio de equity): {money(self.equity_change_week)}")
        lines.append(f"- **PnL total acumulado: {money(self.total_pnl)}**")
        lines.append("")
        lines.append("## Actividad")
        lines.append(f"- Compras: {self.n_buys} · Ventas/cierres: {self.n_sells}")
        lines.append(
            f"- Ganadoras: {self.wins} · Perdedoras: {self.losses} · "
            f"Win rate: {self.win_rate * 100:.1f}%"
        )
        lines.append(f"- Drawdown máximo (semana): {self.max_drawdown * 100:.1f}%")
        lines.append(f"- Posiciones abiertas: {self.open_positions}")
        lines.append(f"- Exposición total: {money(self.total_exposure)}")
        if self.halted:
            lines.append("- ⛔ **Trading detenido**: se alcanzó la pérdida máxima semanal.")
        lines.append("")

        if self.exposure_by_market:
            lines.append("## Exposición por mercado")
            for slug, val in sorted(self.exposure_by_market.items(), key=lambda x: -x[1]):
                lines.append(f"- {slug[:48]}: {money(val)}")
            lines.append("")
        if self.exposure_by_category:
            lines.append("## Exposición por categoría")
            for cat, val in sorted(self.exposure_by_category.items(), key=lambda x: -x[1]):
                lines.append(f"- {cat or 'sin categoría'}: {money(val)}")
            lines.append("")

        if self.best_trades:
            lines.append("## Mejores operaciones")
            for t in self.best_trades:
                lines.append(
                    f"- 🟢 {t.slug[:42]} [{t.outcome}] {money(t.realized_pnl)} "
                    f"({t.reason})"
                )
            lines.append("")
        if self.worst_trades:
            lines.append("## Peores operaciones")
            for t in self.worst_trades:
                lines.append(
                    f"- 🔴 {t.slug[:42]} [{t.outcome}] {money(t.realized_pnl)} "
                    f"({t.reason})"
                )
            lines.append("")
        if self.settlements:
            lines.append("## Mercados resueltos esta semana (settle 0/1)")
            for t in self.settlements:
                verdict = "ganó →1.0" if t.exit_price >= 0.99 else "perdió →0.0"
                lines.append(
                    f"- {t.slug[:42]} [{t.outcome}] {verdict} · {money(t.realized_pnl)}"
                )
            lines.append("")

        lines.append("## ¿Por qué ganó o perdió?")
        lines.append(self.explanation)
        lines.append("")
        lines.append("## Errores o comportamientos raros")
        if self.anomalies:
            for a in self.anomalies:
                lines.append(f"- ⚠️ {a}")
        else:
            lines.append("- Ninguno detectado.")
        lines.append("")
        lines.append("## Recomendación para la próxima semana")
        lines.append(self.recommendation)
        lines.append("")
        lines.append(_BAR)
        lines.append("Generado automáticamente. Revisa el dashboard para el detalle.")
        return "\n".join(lines)


def _trade_events(storage: Storage, since: datetime, until: datetime) -> list[TradeEvent]:
    """Replay all orders; emit a TradeEvent per SELL whose fill lands in window."""
    portfolio = Portfolio(storage.config.initial_balance)
    events: list[TradeEvent] = []
    for o in storage.load_orders():
        delta = portfolio.apply_order(o)
        if o.side != "SELL":
            continue
        ts = o.timestamp
        if ts is None or not (since <= ts < until):
            continue
        events.append(
            TradeEvent(
                timestamp=ts,
                slug=o.slug,
                outcome=o.outcome,
                shares=o.shares,
                exit_price=o.fill_price,
                realized_pnl=delta,
                reason=o.reason or "",
                settlement="settlement" in (o.reason or "").lower(),
            )
        )
    return events


def _build_explanation(rep_data: dict) -> str:
    realized = rep_data["realized_pnl_week"]
    wins = rep_data["wins"]
    losses = rep_data["losses"]
    settlements = rep_data["settlements"]
    if rep_data["n_sells"] == 0 and rep_data["n_buys"] == 0:
        return (
            "Semana sin actividad: no se cumplieron las condiciones de entrada "
            "(precio, spread, liquidez o tiempo a cierre) o el trading estaba detenido."
        )
    direction = "positivo" if realized >= 0 else "negativo"
    drivers = []
    if settlements:
        won = sum(1 for t in settlements if t.exit_price >= 0.99)
        lost = len(settlements) - won
        drivers.append(
            f"{len(settlements)} mercado(s) se resolvieron ({won} a favor, {lost} en contra)"
        )
    if wins or losses:
        drivers.append(f"{wins} cierre(s) ganador(es) y {losses} perdedor(es)")
    driver_txt = "; ".join(drivers) if drivers else "movimientos de precio sobre posiciones abiertas"
    return (
        f"El resultado semanal fue {direction} (${realized:,.2f} realizado). "
        f"Lo explican principalmente: {driver_txt}. "
        "El PnL realizado proviene de cierres por take-profit, stop-loss, "
        "cierre por tiempo/tendencia y liquidación de mercados resueltos; "
        "el resto del cambio de equity es PnL no realizado de posiciones abiertas."
    )


def _build_recommendation(rep_data: dict, cfg: Config) -> str:
    recs: list[str] = []
    if rep_data["halted"]:
        recs.append(
            "Se tocó el stop semanal: revisa si los umbrales de entrada son "
            "demasiado agresivos antes de reanudar."
        )
    if rep_data["max_drawdown"] > 0.15:
        recs.append(
            f"Drawdown elevado ({rep_data['max_drawdown']*100:.0f}%): considera bajar "
            f"MAX_TRADE_PCT (ahora {cfg.max_trade_pct*100:.1f}%) o MAX_TOTAL_EXPOSURE."
        )
    wr = rep_data["win_rate"]
    closed = rep_data["wins"] + rep_data["losses"]
    if closed >= 5 and wr < 0.4:
        recs.append(
            f"Win rate bajo ({wr*100:.0f}%): endurece ENTRY_PRICE_MAX o "
            "MAX_SPREAD para filtrar mejor las entradas."
        )
    if rep_data["n_buys"] == 0 and not rep_data["halted"]:
        recs.append(
            "Cero entradas esta semana: quizá los filtros (liquidez/spread/precio) "
            "son demasiado estrictos; aflójalos un poco si quieres más actividad."
        )
    if rep_data["realized_pnl_week"] > 0 and rep_data["max_drawdown"] < 0.1:
        recs.append("Semana sólida y estable: mantén la configuración actual.")
    if not recs:
        recs.append("Sin cambios sugeridos: la configuración actual parece equilibrada.")
    return " ".join(f"- {r}" for r in recs) if len(recs) == 1 else "\n".join(f"- {r}" for r in recs)


def generate_weekly_report(
    storage: Storage, config: Config, now: datetime | None = None
) -> WeeklyReport:
    now = now or datetime.now(UTC)
    ws = week_start(now)
    we = ws + timedelta(days=7)

    equity_df = storage.get_df("equity_snapshots")
    if not equity_df.empty:
        equity_df = equity_df.copy()
        equity_df["ts_parsed"] = equity_df["ts"].map(_safe_dt)

    # Starting equity = last snapshot strictly before the week (else initial balance).
    starting_equity = config.initial_balance
    ending_equity = config.initial_balance
    max_dd = 0.0
    if not equity_df.empty:
        before = equity_df[equity_df["ts_parsed"] < ws]
        within = equity_df[(equity_df["ts_parsed"] >= ws) & (equity_df["ts_parsed"] < we)]
        if not before.empty:
            starting_equity = float(before.iloc[-1]["equity"])
        elif not within.empty:
            starting_equity = float(within.iloc[0]["equity"])
        if not within.empty:
            ending_equity = float(within.iloc[-1]["equity"])
            max_dd = float(within["drawdown"].max())
        elif not before.empty:
            ending_equity = float(before.iloc[-1]["equity"])

    realized_week = storage.realized_pnl_since(ws)
    total_pnl = ending_equity - config.initial_balance

    events = _trade_events(storage, ws, we)
    wins = sum(1 for e in events if e.realized_pnl > 0)
    losses = sum(1 for e in events if e.realized_pnl <= 0)
    settlements = [e for e in events if e.settlement]
    n_sells = len(events)

    # Count buys this week from raw orders.
    n_buys = sum(
        1 for o in storage.load_orders()
        if o.side == "BUY" and o.timestamp is not None and ws <= o.timestamp < we
    )

    # Current open exposure by market and category.
    portfolio = storage.load_portfolio()
    markets_df = storage.latest_market_snapshots()
    if not markets_df.empty:
        marks = dict(zip(markets_df["token_id"], markets_df["midpoint"]))
        portfolio.mark_to_market({k: v for k, v in marks.items() if v})
    cat_map = storage.category_map()
    exposure_by_market: dict[str, float] = {}
    exposure_by_category: dict[str, float] = {}
    for pos in portfolio.open_positions():
        exposure_by_market[pos.slug] = exposure_by_market.get(pos.slug, 0.0) + pos.market_value
        cat = cat_map.get(pos.token_id, "") or ""
        exposure_by_category[cat] = exposure_by_category.get(cat, 0.0) + pos.market_value

    ranked = sorted(events, key=lambda e: e.realized_pnl, reverse=True)
    best = [e for e in ranked if e.realized_pnl > 0][:3]
    worst = [e for e in reversed(ranked) if e.realized_pnl <= 0][:3]

    halted = realized_week <= -config.max_weekly_loss
    win_rate = wins / n_sells if n_sells else 0.0

    data = {
        "realized_pnl_week": realized_week,
        "n_buys": n_buys,
        "n_sells": n_sells,
        "wins": wins,
        "losses": losses,
        "settlements": settlements,
        "max_drawdown": max_dd,
        "win_rate": win_rate,
        "halted": halted,
    }

    return WeeklyReport(
        week_start=ws,
        week_end=we,
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        initial_balance=config.initial_balance,
        realized_pnl_week=realized_week,
        equity_change_week=ending_equity - starting_equity,
        total_pnl=total_pnl,
        n_buys=n_buys,
        n_sells=n_sells,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        max_drawdown=max_dd,
        total_exposure=portfolio.total_exposure(),
        open_positions=portfolio.open_position_count(),
        exposure_by_market=exposure_by_market,
        exposure_by_category=exposure_by_category,
        best_trades=best,
        worst_trades=worst,
        settlements=settlements,
        explanation=_build_explanation(data),
        anomalies=_detect_anomalies(storage, ws, we),
        recommendation=_build_recommendation(data, config),
        halted=halted,
    )


def _detect_anomalies(storage: Storage, ws: datetime, we: datetime) -> list[str]:
    anomalies: list[str] = []
    # Rejected orders this week hint at execution problems.
    cur = storage.conn.execute(
        "SELECT reason, COUNT(*) AS n FROM orders WHERE status='REJECTED' "
        "AND ts >= ? AND ts < ? GROUP BY reason ORDER BY n DESC",
        (ws.isoformat(), we.isoformat()),
    )
    for r in cur.fetchall():
        anomalies.append(f"{r['n']} orden(es) rechazada(s) en ejecución: {r['reason']}")
    # Negative cash would indicate an accounting bug.
    portfolio = storage.load_portfolio()
    if portfolio.cash < -1e-6:
        anomalies.append(f"Cash negativo detectado (${portfolio.cash:.2f}); revisar contabilidad.")
    return anomalies


def save_report(report: WeeklyReport, config: Config) -> str:
    out_dir = config.reports_path
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.week_start.strftime("%Y-%m-%d")
    path = out_dir / f"weekly_report_{stamp}.md"
    path.write_text(report.to_markdown(), encoding="utf-8")
    return str(path)


def _safe_dt(value) -> datetime:
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=UTC)
