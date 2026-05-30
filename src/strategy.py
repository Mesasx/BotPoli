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
        # Trend exit: bail out on a strong adverse move even before the hard
        # stop-loss is hit. Skipped when trend is unknown or disabled.
        if (
            cfg.trend_exit_pct > 0
            and snap.trend is not None
            and snap.trend <= -cfg.trend_exit_pct
        ):
            return self._signal(
                snap, SELL, sell_price,
                f"trend exit (mid {snap.trend*100:.1f}% <= -{cfg.trend_exit_pct*100:.0f}%)",
            )

        return self._signal(
            snap, HOLD, sell_price,
            f"hold: return {ret*100:+.1f}% within [-{cfg.stop_loss_pct*100:.0f}%, "
            f"{cfg.take_profit_pct*100:.0f}%]",
        )


class ValueStrategy(SimpleThresholdStrategy):
    """A more sensible, "with criteria" entry rule (Level A intelligence).

    It deliberately avoids the naive "buy anything cheap" behaviour that lands the
    bot in absurd longshots. It only enters when **all** of these hold:

    * the outcome sits in a *sane probability band* (``MIN_ENTRY_PROB`` ..
      ``MAX_ENTRY_PROB``) — no lottery-ticket longshots, no near-certain favourites;
    * the market is *real and tradable*: enough liquidity AND volume, tight spread,
      enough time to close;
    * there is a measurable *edge*: the current ask is at a discount of at least
      ``MIN_EDGE`` versus the outcome's recent reference price (its average mid over
      the signal window) — i.e. we buy a genuine dip, not just a low absolute price;
    * the price is **not** in a strong downtrend (don't catch a falling knife).

    Honest note: this is a disciplined heuristic, not a true forecast of the
    outcome. Without external information the market price is already a strong
    estimate; this strategy just trades quality dips within a sane band and manages
    risk. It is not guaranteed to be profitable. Exits are inherited (TP / SL /
    pre-expiry / trend).
    """

    name = "value"

    def _entry_signal(self, snap: MarketSnapshot) -> Signal:
        cfg = self.config
        outcome = snap.outcome.strip().upper()

        if outcome == "NO" and not cfg.allow_no:
            return self._signal(snap, HOLD, snap.best_ask, "NO outcomes disabled (ALLOW_NO=false)")
        if outcome not in ("YES", "NO"):
            return self._signal(snap, HOLD, snap.best_ask, f"unsupported outcome '{snap.outcome}'")
        if snap.best_ask <= 0 or snap.best_ask >= 1:
            return self._signal(snap, HOLD, snap.best_ask, "no valid ask price")

        # 1) sane probability band (avoid longshots and near-certain favourites)
        if not (cfg.min_entry_prob <= snap.mid <= cfg.max_entry_prob):
            return self._signal(
                snap, HOLD, snap.best_ask,
                f"prob {snap.mid:.2f} outside band "
                f"[{cfg.min_entry_prob:.2f}, {cfg.max_entry_prob:.2f}]",
            )

        # 2) market quality
        if snap.spread > cfg.max_spread:
            return self._signal(snap, HOLD, snap.best_ask,
                                f"spread {snap.spread:.3f} > {cfg.max_spread:.3f}")
        if snap.liquidity < cfg.min_liquidity:
            return self._signal(snap, HOLD, snap.best_ask,
                                f"liquidity {snap.liquidity:.0f} < {cfg.min_liquidity:.0f}")
        if snap.volume < cfg.min_volume:
            return self._signal(snap, HOLD, snap.best_ask,
                                f"volume {snap.volume:.0f} < MIN_VOLUME {cfg.min_volume:.0f}")
        if snap.hours_to_close is not None and snap.hours_to_close < cfg.min_hours_to_close:
            return self._signal(snap, HOLD, snap.best_ask,
                                f"closes in {snap.hours_to_close:.1f}h < {cfg.min_hours_to_close:.0f}")

        # 3) need a reference to judge value; never trade blind on a cold start
        if snap.ref_price is None or snap.ref_price <= 0:
            return self._signal(snap, HOLD, snap.best_ask,
                                "sin referencia todavía (acumulando histórico)")
        edge = (snap.ref_price - snap.best_ask) / snap.ref_price
        if edge < cfg.min_edge:
            return self._signal(
                snap, HOLD, snap.best_ask,
                f"edge {edge*100:.1f}% < MIN_EDGE {cfg.min_edge*100:.0f}% "
                f"(ask {snap.best_ask:.3f} vs ref {snap.ref_price:.3f})",
            )

        # 4) don't buy into a strong downtrend
        if snap.trend is not None and snap.trend <= -cfg.stop_loss_pct:
            return self._signal(snap, HOLD, snap.best_ask,
                                f"downtrend {snap.trend*100:.1f}%; skip")

        return self._signal(
            snap, BUY, snap.best_ask,
            f"BUY {outcome}: edge {edge*100:.1f}% (ask {snap.best_ask:.3f} vs "
            f"ref {snap.ref_price:.3f}), prob {snap.mid:.2f}, vol {snap.volume:.0f}",
        )


def build_strategy(name: str, config: Config) -> Strategy:
    strategies: dict[str, type[Strategy]] = {
        SimpleThresholdStrategy.name: SimpleThresholdStrategy,
        ValueStrategy.name: ValueStrategy,
    }
    cls = strategies.get(name, ValueStrategy)
    return cls(config)
