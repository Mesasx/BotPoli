"""Signal generation.

A :class:`Strategy` turns a market snapshot (and any open position) into a
:class:`Signal`. The default :class:`SimpleThresholdStrategy` implements the
brief's rules; new strategies just subclass :class:`Strategy` and override
:meth:`generate`.

Design note: strategies are *pure* w.r.t. risk/sizing. They only say "I want to
BUY/SELL this and here's why". The :mod:`risk_manager` decides whether the trade
is allowed and how large it may be. This separation keeps strategies simple and
independently testable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .config import Config
from .models import BUY, HOLD, SELL, MarketSnapshot, Position, Signal


class Strategy(ABC):
    name = "base"

    def __init__(self, config: Config):
        self.config = config

    @abstractmethod
    def generate(self, snap: MarketSnapshot, position: Position | None) -> Signal:
        ...

    def _signal(self, snap: MarketSnapshot, signal_type: str, price: float, reason: str) -> Signal:
        return Signal(
            market_id=snap.market_id,
            slug=snap.slug,
            outcome=snap.outcome,
            token_id=snap.token_id,
            signal_type=signal_type,
            price=price,
            reason=reason,
        )


class SimpleThresholdStrategy(Strategy):
    """Buy cheap outcomes with a tight spread; exit on TP / SL / pre-expiry.

    Entry (BUY):
      * no existing position in this token
      * outcome is YES (or NO when ALLOW_NO=true)
      * best_ask <= ENTRY_PRICE_MAX
      * spread <= MAX_SPREAD
      * liquidity >= MIN_LIQUIDITY
      * hours_to_close >= MIN_HOURS_TO_CLOSE

    Exit (SELL) when holding:
      * return >= TAKE_PROFIT_PCT            -> take profit
      * return <= -STOP_LOSS_PCT             -> stop loss
      * hours_to_close <= EXIT_HOURS_BEFORE_CLOSE -> close before expiry
    """

    name = "simple_threshold"

    def generate(self, snap: MarketSnapshot, position: Position | None) -> Signal:
        cfg = self.config
        holding = position is not None and position.shares > 1e-9

        if holding:
            return self._exit_signal(snap, position)  # type: ignore[arg-type]

        return self._entry_signal(snap)

    # -- entry --------------------------------------------------------------
    def _entry_signal(self, snap: MarketSnapshot) -> Signal:
        cfg = self.config
        outcome = snap.outcome.strip().upper()

        if outcome == "NO" and not cfg.allow_no:
            return self._signal(snap, HOLD, snap.best_ask, "NO outcomes disabled (ALLOW_NO=false)")
        if outcome not in ("YES", "NO"):
            return self._signal(snap, HOLD, snap.best_ask, f"unsupported outcome '{snap.outcome}'")

        if snap.best_ask <= 0 or snap.best_ask >= 1:
            return self._signal(snap, HOLD, snap.best_ask, "no valid ask price")
        if snap.best_ask > cfg.entry_price_max:
            return self._signal(
                snap, HOLD, snap.best_ask,
                f"ask {snap.best_ask:.3f} > ENTRY_PRICE_MAX {cfg.entry_price_max:.3f}",
            )
        if snap.spread > cfg.max_spread:
            return self._signal(
                snap, HOLD, snap.best_ask,
                f"spread {snap.spread:.3f} > MAX_SPREAD {cfg.max_spread:.3f}",
            )
        if snap.liquidity < cfg.min_liquidity:
            return self._signal(
                snap, HOLD, snap.best_ask,
                f"liquidity {snap.liquidity:.0f} < MIN_LIQUIDITY {cfg.min_liquidity:.0f}",
            )
        if snap.hours_to_close is not None and snap.hours_to_close < cfg.min_hours_to_close:
            return self._signal(
                snap, HOLD, snap.best_ask,
                f"closes in {snap.hours_to_close:.1f}h < MIN_HOURS_TO_CLOSE {cfg.min_hours_to_close:.0f}",
            )

        return self._signal(
            snap, BUY, snap.best_ask,
            f"BUY {outcome}: ask {snap.best_ask:.3f} <= {cfg.entry_price_max:.3f}, "
            f"spread {snap.spread:.3f}, liq {snap.liquidity:.0f}",
        )

    # -- exit ---------------------------------------------------------------
    def _exit_signal(self, snap: MarketSnapshot, position: Position) -> Signal:
        cfg = self.config
        # Mark to the price we could sell at (best bid).
        sell_price = snap.best_bid or snap.mid
        ret = 0.0
        if position.avg_price > 0:
            ret = (sell_price - position.avg_price) / position.avg_price

        if snap.hours_to_close is not None and snap.hours_to_close <= cfg.exit_hours_before_close:
            return self._signal(
                snap, SELL, sell_price,
                f"close before expiry ({snap.hours_to_close:.1f}h <= {cfg.exit_hours_before_close:.0f}h)",
            )
        if ret >= cfg.take_profit_pct:
            return self._signal(
                snap, SELL, sell_price,
                f"take profit (+{ret*100:.1f}% >= {cfg.take_profit_pct*100:.0f}%)",
            )
        if ret <= -cfg.stop_loss_pct:
            return self._signal(
                snap, SELL, sell_price,
                f"stop loss ({ret*100:.1f}% <= -{cfg.stop_loss_pct*100:.0f}%)",
            )

        return self._signal(
            snap, HOLD, sell_price,
            f"hold: return {ret*100:+.1f}% within [-{cfg.stop_loss_pct*100:.0f}%, "
            f"{cfg.take_profit_pct*100:.0f}%]",
        )


def build_strategy(name: str, config: Config) -> Strategy:
    strategies: dict[str, type[Strategy]] = {
        SimpleThresholdStrategy.name: SimpleThresholdStrategy,
    }
    cls = strategies.get(name, SimpleThresholdStrategy)
    return cls(config)
