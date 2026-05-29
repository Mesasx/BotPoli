"""Configuration loading.

All configuration is read from environment variables (optionally populated from a
``.env`` file via python-dotenv). The :class:`Config` object is immutable and is
passed explicitly to every component, which keeps the code easy to test (no hidden
global state).

SAFETY: ``LIVE_TRADING`` is intentionally inert. This codebase has no order-signing,
no private keys and no write access to Polymarket. Even if ``LIVE_TRADING=true`` were
set, :func:`load_config` raises, because real trading is out of scope by design.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional at runtime
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Config:
    """Immutable runtime configuration."""

    # Safety
    paper_trading: bool = True
    live_trading: bool = False

    # Account
    initial_balance: float = 1000.0

    # Strategy toggles
    allow_no: bool = False

    # Sizing / risk limits (USDC)
    max_trade_size: float = 5.0
    max_trade_pct: float = 0.02          # max fraction of equity per single trade
    max_position_size: float = 25.0
    max_daily_loss: float = 20.0
    max_weekly_loss: float = 50.0        # weekly circuit breaker (halts new buys)
    max_open_positions: int = 5
    max_market_exposure: float = 25.0
    max_total_exposure: float = 100.0

    # Market filters
    max_spread: float = 0.03
    min_liquidity: float = 500.0
    min_hours_to_close: float = 24.0
    exit_hours_before_close: float = 6.0

    # Entry / exit thresholds
    entry_price_max: float = 0.45
    take_profit_pct: float = 0.15
    stop_loss_pct: float = 0.10

    # Trend-based exit (close on a strong adverse move in the mark price)
    trend_window: int = 5                # nº of recent snapshots used to gauge trend
    trend_exit_pct: float = 0.25         # adverse mid move that forces an exit (0 = off)

    # Market resolution (settle 0/1) — always on; paper-only liquidation
    settle_resolved: bool = True

    # Simulation realism
    slippage_bps: float = 10.0
    fee_bps: float = 0.0

    # Runtime
    poll_interval_seconds: int = 30
    dashboard_refresh_seconds: int = 10
    scan_limit: int = 200

    # API
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"

    # Storage / logging / reports
    database_url: str = "sqlite:///data/polymarket_paper_bot.db"
    log_level: str = "INFO"
    reports_dir: str = "data/reports"

    @property
    def db_path(self) -> Path:
        """Resolve ``DATABASE_URL`` (sqlite only) to an absolute file path."""
        url = self.database_url
        prefix = "sqlite:///"
        raw = url[len(prefix):] if url.startswith(prefix) else url
        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    @property
    def reports_path(self) -> Path:
        """Resolve ``reports_dir`` to an absolute directory path."""
        path = Path(self.reports_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    @property
    def slippage_rate(self) -> float:
        return self.slippage_bps / 10_000.0

    @property
    def fee_rate(self) -> float:
        return self.fee_bps / 10_000.0


def load_config(env_file: str | os.PathLike | None = None) -> Config:
    """Build a :class:`Config` from the environment.

    Raises if live trading is requested - this project is paper-only by design.
    """
    if env_file is None:
        default_env = PROJECT_ROOT / ".env"
        if default_env.exists():
            load_dotenv(default_env)
    else:
        load_dotenv(env_file)

    cfg = Config(
        paper_trading=_get_bool("PAPER_TRADING", True),
        live_trading=_get_bool("LIVE_TRADING", False),
        initial_balance=_get_float("INITIAL_PAPER_BALANCE_USDC", 1000.0),
        allow_no=_get_bool("ALLOW_NO", False),
        max_trade_size=_get_float("MAX_TRADE_SIZE_USDC", 5.0),
        max_trade_pct=_get_float("MAX_TRADE_PCT", 0.02),
        max_position_size=_get_float("MAX_POSITION_SIZE_USDC", 25.0),
        max_daily_loss=_get_float("MAX_DAILY_LOSS_USDC", 20.0),
        max_weekly_loss=_get_float("MAX_WEEKLY_LOSS_USDC", 50.0),
        max_open_positions=_get_int("MAX_OPEN_POSITIONS", 5),
        max_market_exposure=_get_float("MAX_MARKET_EXPOSURE_USDC", 25.0),
        max_total_exposure=_get_float("MAX_TOTAL_EXPOSURE_USDC", 100.0),
        max_spread=_get_float("MAX_SPREAD", 0.03),
        min_liquidity=_get_float("MIN_LIQUIDITY", 500.0),
        min_hours_to_close=_get_float("MIN_HOURS_TO_CLOSE", 24.0),
        exit_hours_before_close=_get_float("EXIT_HOURS_BEFORE_CLOSE", 6.0),
        entry_price_max=_get_float("ENTRY_PRICE_MAX", 0.45),
        take_profit_pct=_get_float("TAKE_PROFIT_PCT", 0.15),
        stop_loss_pct=_get_float("STOP_LOSS_PCT", 0.10),
        trend_window=_get_int("TREND_WINDOW", 5),
        trend_exit_pct=_get_float("TREND_EXIT_PCT", 0.25),
        settle_resolved=_get_bool("SETTLE_RESOLVED", True),
        slippage_bps=_get_float("SLIPPAGE_BPS", 10.0),
        fee_bps=_get_float("FEE_BPS", 0.0),
        poll_interval_seconds=_get_int("POLL_INTERVAL_SECONDS", 30),
        dashboard_refresh_seconds=_get_int("DASHBOARD_REFRESH_SECONDS", 10),
        scan_limit=_get_int("SCAN_LIMIT", 200),
        gamma_api_url=os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com").rstrip("/"),
        clob_api_url=os.getenv("CLOB_API_URL", "https://clob.polymarket.com").rstrip("/"),
        database_url=os.getenv("DATABASE_URL", "sqlite:///data/polymarket_paper_bot.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        reports_dir=os.getenv("REPORTS_DIR", "data/reports"),
    )

    if cfg.live_trading or not cfg.paper_trading:
        raise RuntimeError(
            "Live trading is not supported. This project is PAPER-ONLY by design: "
            "there is no order signing, no wallet and no write access to Polymarket. "
            "Set PAPER_TRADING=true and LIVE_TRADING=false."
        )

    return cfg
