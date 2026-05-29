"""Risk management: approve/reject signals and size approved trades.

The risk manager is the single gatekeeper between a strategy's intent and a
simulated fill. It never *generates* trades; it only constrains them. Selling
(reducing risk) is always allowed as long as a position exists; buying is subject
to every configured limit.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .models import BUY, HOLD, SELL, MarketSnapshot, Signal
from .portfolio import Portfolio


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    size_usdc: float = 0.0


class RiskManager:
    def __init__(self, config: Config):
        self.config = config

    def evaluate(
        self,
        signal: Signal,
        snap: MarketSnapshot,
        portfolio: Portfolio,
        daily_realized_pnl: float = 0.0,
        weekly_realized_pnl: float = 0.0,
    ) -> RiskDecision:
        if signal.signal_type == HOLD:
            return RiskDecision(False, "hold signal - no action")
        if signal.signal_type == SELL:
            return self._evaluate_sell(signal, portfolio)
        if signal.signal_type == BUY:
            return self._evaluate_buy(
                signal, snap, portfolio, daily_realized_pnl, weekly_realized_pnl
            )
        return RiskDecision(False, f"unknown signal type {signal.signal_type}")

    def trading_halted(self, weekly_realized_pnl: float) -> bool:
        """Weekly circuit breaker: once the weekly loss cap is hit, no new buys
        are allowed until the next ISO week resets the realised PnL window."""
        return weekly_realized_pnl <= -self.config.max_weekly_loss

    # -- sell ---------------------------------------------------------------
    def _evaluate_sell(self, signal: Signal, portfolio: Portfolio) -> RiskDecision:
        pos = portfolio.get_position(signal.token_id)
        if pos is None or pos.shares <= 1e-9:
            return RiskDecision(False, "no open position to sell")
        # Closing trades are always permitted; size = full position value.
        size = pos.shares * max(signal.price, 1e-9)
        return RiskDecision(True, "exit approved", size_usdc=size)

    # -- buy ----------------------------------------------------------------
    def _evaluate_buy(
        self,
        signal: Signal,
        snap: MarketSnapshot,
        portfolio: Portfolio,
        daily_realized_pnl: float,
        weekly_realized_pnl: float = 0.0,
    ) -> RiskDecision:
        cfg = self.config

        # Weekly loss circuit breaker (hard halt until the week rolls over)
        if self.trading_halted(weekly_realized_pnl):
            return RiskDecision(
                False,
                f"weekly loss limit hit ({weekly_realized_pnl:.2f} <= "
                f"-{cfg.max_weekly_loss:.2f}); trading halted this week",
            )

        # Daily loss circuit breaker
        if daily_realized_pnl <= -cfg.max_daily_loss:
            return RiskDecision(
                False,
                f"daily loss limit hit ({daily_realized_pnl:.2f} <= -{cfg.max_daily_loss:.2f})",
            )

        existing = portfolio.get_position(signal.token_id)
        is_new_position = existing is None or existing.shares <= 1e-9

        # Max open positions (only blocks brand-new positions)
        if is_new_position and portfolio.open_position_count() >= cfg.max_open_positions:
            return RiskDecision(
                False,
                f"max open positions reached ({cfg.max_open_positions})",
            )

        price = signal.price
        if price <= 0:
            return RiskDecision(False, "invalid entry price")

        # Start from the per-trade cap, then shrink to satisfy every other cap.
        size = cfg.max_trade_size

        # Per-trade cap as a fraction of current equity (0 disables it)
        if cfg.max_trade_pct > 0:
            size = min(size, cfg.max_trade_pct * portfolio.equity())

        # Cash available
        size = min(size, portfolio.cash)

        # Per-position cap (existing cost basis + new)
        existing_value = existing.cost_basis if existing else 0.0
        remaining_position = cfg.max_position_size - existing_value
        size = min(size, remaining_position)

        # Per-market exposure cap
        market_exposure = portfolio.market_exposure(signal.market_id)
        remaining_market = cfg.max_market_exposure - market_exposure
        size = min(size, remaining_market)

        # Total exposure cap
        total_exposure = portfolio.total_exposure()
        remaining_total = cfg.max_total_exposure - total_exposure
        size = min(size, remaining_total)

        if size <= 0.01:
            return RiskDecision(
                False,
                "no budget after risk limits "
                f"(cash {portfolio.cash:.2f}, pos_left {remaining_position:.2f}, "
                f"mkt_left {remaining_market:.2f}, total_left {remaining_total:.2f})",
            )

        return RiskDecision(True, f"approved size {size:.2f} USDC", size_usdc=round(size, 2))
