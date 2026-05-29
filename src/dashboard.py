"""Streamlit dashboard for the Polymarket paper-trading bot.

Run with:
    streamlit run src/dashboard.py
or:
    python -m src.main dashboard

The dashboard is read-only: it visualises data persisted by the paper engine and
never places trades.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

# Allow `streamlit run src/dashboard.py` (script run) to import the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.report import generate_weekly_report  # noqa: E402
from src.risk_manager import RiskManager  # noqa: E402
from src.storage import Storage  # noqa: E402

st.set_page_config(page_title="Polymarket Paper Bot", layout="wide", page_icon="📈")


@st.cache_resource
def _config():
    return load_config()


def _storage(cfg) -> Storage:
    # New connection per run (sqlite connections are not thread-safe to share).
    return Storage(cfg)


def _money(x: float) -> str:
    return f"${x:,.2f}"


def main() -> None:
    cfg = _config()
    storage = _storage(cfg)

    st.title("📈 Polymarket Paper Trading Bot")
    st.caption(
        "PAPER TRADING ONLY — no real orders, no wallet, no live execution. "
        "Prices are real (Polymarket public API); fills are simulated."
    )

    equity_df = storage.get_df("equity_snapshots")
    orders_df = storage.get_df("orders")
    signals_df = storage.get_df("signals")
    positions_df = storage.get_df("positions")
    markets_df = storage.latest_market_snapshots()

    for df in (equity_df, orders_df, signals_df):
        if "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)

    # -- Sidebar filters ----------------------------------------------------
    st.sidebar.header("Filtros")
    if st.sidebar.button("🔄 Recargar"):
        st.rerun()

    outcome_filter = st.sidebar.multiselect(
        "Outcome", options=_unique(orders_df, "outcome"), default=[]
    )
    status_filter = st.sidebar.multiselect(
        "Estado orden", options=_unique(orders_df, "status"), default=[]
    )
    market_filter = st.sidebar.text_input("Mercado (slug contiene)", "")

    # -- Summary ------------------------------------------------------------
    portfolio = storage.load_portfolio()
    if not markets_df.empty:
        marks = dict(zip(markets_df["token_id"], markets_df["midpoint"]))
        portfolio.mark_to_market({k: v for k, v in marks.items() if v})

    equity = portfolio.equity()
    total_pnl = equity - cfg.initial_balance
    daily_pnl = storage.daily_realized_pnl()
    weekly_pnl = storage.weekly_realized_pnl()
    max_dd = equity_df["drawdown"].max() if not equity_df.empty else 0.0

    # Weekly circuit-breaker banner.
    halted = RiskManager(cfg).trading_halted(weekly_pnl)
    if halted:
        st.error(
            f"⛔ Trading DETENIDO esta semana: la pérdida semanal "
            f"({_money(weekly_pnl)}) alcanzó el límite de {_money(-cfg.max_weekly_loss)}. "
            "Se reanudará automáticamente la próxima semana ISO."
        )

    st.subheader("Resumen")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Balance inicial", _money(cfg.initial_balance))
    c2.metric("Equity actual", _money(equity), _money(total_pnl))
    c3.metric("PnL total", _money(total_pnl))
    c4.metric("PnL realizado", _money(portfolio.realized_pnl))

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("PnL diario (hoy)", _money(daily_pnl))
    c6.metric("PnL semanal", _money(weekly_pnl))
    c7.metric("Drawdown máx.", f"{max_dd*100:.1f}%")
    c8.metric("Win rate", f"{portfolio.win_rate()*100:.1f}%")

    c9, c10, c11, c12 = st.columns(4)
    c9.metric("Posiciones abiertas", f"{portfolio.open_position_count()}")
    c10.metric("Exposición total", _money(portfolio.total_exposure()))
    c11.metric("Cash", _money(portfolio.cash))
    sharpe = _sharpe(equity_df)
    c12.metric("Sharpe (diario, aprox.)", f"{sharpe:.2f}" if sharpe is not None else "n/a")
    n_filled = len(orders_df[orders_df.get("status", "") == "FILLED"]) if not orders_df.empty else 0
    st.caption(
        f"Operaciones simuladas (fills): {n_filled} · "
        f"Operaciones cerradas: {len(portfolio.closed_trades)}"
    )

    # -- Weekly report ------------------------------------------------------
    with st.expander("📊 Informe semanal", expanded=False):
        try:
            report = generate_weekly_report(storage, cfg)
            st.markdown(report.to_markdown())
            if report.exposure_by_category:
                cat_df = pd.DataFrame(
                    {"categoría": list(report.exposure_by_category.keys()),
                     "exposición": list(report.exposure_by_category.values())}
                )
                st.plotly_chart(
                    px.pie(cat_df, names="categoría", values="exposición",
                           title="Exposición por categoría"),
                    use_container_width=True,
                )
        except Exception as exc:  # pragma: no cover - defensive UI guard
            st.warning(f"No se pudo generar el informe: {exc}")

    # -- Charts -------------------------------------------------------------
    st.subheader("Gráficos")
    g1, g2 = st.columns(2)
    with g1:
        if not equity_df.empty:
            fig = px.line(equity_df, x="ts", y="equity", title="Equity curve")
            fig.add_hline(y=cfg.initial_balance, line_dash="dash", line_color="gray")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Sin datos de equity todavía. Ejecuta `python -m src.main paper`.")
    with g2:
        if not equity_df.empty:
            cum = equity_df.copy()
            cum["pnl_acumulado"] = cum["equity"] - cfg.initial_balance
            st.plotly_chart(
                px.area(cum, x="ts", y="pnl_acumulado", title="PnL acumulado"),
                use_container_width=True,
            )

    g3, g4 = st.columns(2)
    with g3:
        if not equity_df.empty:
            st.plotly_chart(
                px.area(equity_df, x="ts", y="drawdown", title="Drawdown")
                .update_traces(line_color="#d62728"),
                use_container_width=True,
            )
    with g4:
        if not equity_df.empty:
            day = equity_df.set_index("ts")["realized_pnl"].resample("1D").last().dropna()
            daily = day.diff().fillna(day)
            st.plotly_chart(
                px.bar(x=daily.index, y=daily.values, title="PnL diario (realizado)")
                .update_layout(xaxis_title="día", yaxis_title="PnL"),
                use_container_width=True,
            )

    g5, g6 = st.columns(2)
    with g5:
        open_pos = positions_df[positions_df["status"] == "open"] if not positions_df.empty else pd.DataFrame()
        if not open_pos.empty:
            open_pos = open_pos.copy()
            open_pos["exposure"] = open_pos["shares"] * open_pos["mark_price"]
            st.plotly_chart(
                px.bar(open_pos, x="slug", y="exposure", color="outcome",
                       title="Exposición por mercado"),
                use_container_width=True,
            )
        else:
            st.info("Sin posiciones abiertas.")
    with g6:
        if not positions_df.empty and (positions_df["status"] == "closed").any():
            closed = positions_df[positions_df["status"] == "closed"].copy()
            wins = int((closed["realized_pnl"] > 0).sum())
            losses = int((closed["realized_pnl"] <= 0).sum())
            st.plotly_chart(
                px.pie(names=["Ganadoras", "Perdedoras"], values=[wins, losses],
                       title="Operaciones ganadoras vs perdedoras",
                       color_discrete_sequence=["#2ca02c", "#d62728"]),
                use_container_width=True,
            )
        else:
            st.info("Aún no hay operaciones cerradas.")

    if not signals_df.empty:
        sig = signals_df.copy()
        sig["day"] = sig["ts"].dt.date
        per_day = sig.groupby(["day", "signal_type"]).size().reset_index(name="count")
        st.plotly_chart(
            px.bar(per_day, x="day", y="count", color="signal_type", title="Señales por día"),
            use_container_width=True,
        )

    # -- Tables -------------------------------------------------------------
    st.subheader("Posiciones")
    if not positions_df.empty:
        pos = positions_df.copy()
        pos["unrealized_pnl"] = (pos["mark_price"] - pos["avg_price"]) * pos["shares"]
        pos["return_pct"] = ((pos["mark_price"] - pos["avg_price"]) /
                             pos["avg_price"].replace(0, pd.NA) * 100).round(2)
        tab_open, tab_closed = st.tabs(["Abiertas", "Cerradas"])
        with tab_open:
            st.dataframe(pos[pos["status"] == "open"], use_container_width=True)
        with tab_closed:
            st.dataframe(pos[pos["status"] == "closed"], use_container_width=True)
    else:
        st.info("Sin posiciones.")

    st.subheader("Operaciones simuladas")
    od = _apply_filters(orders_df, outcome_filter, status_filter, market_filter)
    st.dataframe(od.sort_values("ts", ascending=False) if not od.empty else od,
                 use_container_width=True)

    st.subheader("Señales")
    sd = signals_df.copy()
    if outcome_filter and not sd.empty:
        sd = sd[sd["outcome"].isin(outcome_filter)]
    if market_filter and not sd.empty:
        sd = sd[sd["slug"].str.contains(market_filter, case=False, na=False)]
    st.dataframe(sd.sort_values("ts", ascending=False) if not sd.empty else sd,
                 use_container_width=True)

    st.subheader("Mercados analizados")
    md = markets_df.copy()
    if market_filter and not md.empty:
        md = md[md["slug"].str.contains(market_filter, case=False, na=False)]
    if outcome_filter and not md.empty:
        md = md[md["outcome"].isin(outcome_filter)]
    cols = ["slug", "outcome", "volume", "liquidity", "best_bid", "best_ask",
            "midpoint", "spread", "hours_to_close", "signal"]
    if not md.empty:
        st.dataframe(md[[c for c in cols if c in md.columns]], use_container_width=True)
    else:
        st.info("Sin mercados escaneados. Ejecuta `python -m src.main scan`.")

    storage.close()


def _sharpe(equity_df: pd.DataFrame) -> float | None:
    """Rough annualised Sharpe from the equity curve's per-snapshot returns.

    Educational approximation: not a substitute for a proper risk-adjusted
    metric, but a useful at-a-glance number on the dashboard.
    """
    if equity_df.empty or "equity" not in equity_df.columns or len(equity_df) < 3:
        return None
    rets = equity_df["equity"].astype(float).pct_change().dropna()
    if rets.empty or rets.std() == 0:
        return None
    return float((rets.mean() / rets.std()) * (252 ** 0.5))


def _unique(df: pd.DataFrame, col: str) -> list:
    if df.empty or col not in df.columns:
        return []
    return sorted(df[col].dropna().unique().tolist())


def _apply_filters(df: pd.DataFrame, outcomes, statuses, market) -> pd.DataFrame:
    if df.empty:
        return df
    out = df
    if outcomes:
        out = out[out["outcome"].isin(outcomes)]
    if statuses:
        out = out[out["status"].isin(statuses)]
    if market:
        out = out[out["slug"].str.contains(market, case=False, na=False)]
    return out


if __name__ == "__main__":
    main()
