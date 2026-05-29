"""Scan and normalise Polymarket markets into :class:`MarketSnapshot` objects.

Pipeline:
1. Pull active/open markets from the Gamma API.
2. For each binary market, expand its two outcomes (YES / NO) into tradable tokens.
3. Enrich each token with live CLOB pricing (best bid/ask, midpoint, spread),
   falling back to the Gamma snapshot fields when CLOB is unreachable.
4. Optionally apply tradability filters (liquidity, spread, time-to-close).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .config import Config
from .logger import get_logger
from .models import MarketSnapshot, utcnow
from .polymarket_client import PolymarketClient, _to_float, parse_json_field

log = get_logger("scanner")


def _parse_end_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def _hours_to_close(end_date: datetime | None, now: datetime | None = None) -> float | None:
    if end_date is None:
        return None
    now = now or utcnow()
    return (end_date - now).total_seconds() / 3600.0


class MarketScanner:
    def __init__(self, config: Config, client: PolymarketClient):
        self.config = config
        self.client = client

    def scan(self, *, enrich: bool = True, apply_filters: bool = False) -> list[MarketSnapshot]:
        """Return outcome-level snapshots for currently active markets."""
        raw_markets = self.client.get_markets(
            active=True,
            closed=False,
            limit=self.config.scan_limit,
            order="volume24hr",
            ascending=False,
            liquidity_num_min=self.config.min_liquidity,
        )
        log.info("Fetched %d markets from Gamma", len(raw_markets))

        snapshots: list[MarketSnapshot] = []
        for market in raw_markets:
            snapshots.extend(self._expand_market(market, enrich=enrich))

        if apply_filters:
            snapshots = [s for s in snapshots if self.is_tradable(s)]
        log.info("Built %d outcome snapshots (filters=%s)", len(snapshots), apply_filters)
        return snapshots

    def _expand_market(self, market: dict[str, Any], *, enrich: bool) -> list[MarketSnapshot]:
        outcomes = parse_json_field(market.get("outcomes"))
        token_ids = parse_json_field(market.get("clobTokenIds"))
        prices = parse_json_field(market.get("outcomePrices"))

        if not outcomes or not token_ids or len(outcomes) != len(token_ids):
            return []

        end_date = _parse_end_date(market.get("endDate"))
        htc = _hours_to_close(end_date)
        volume = _to_float(market.get("volumeNum") or market.get("volume")) or 0.0
        liquidity = _to_float(market.get("liquidityNum") or market.get("liquidity")) or 0.0
        category = market.get("category") or ""
        slug = market.get("slug") or ""
        question = market.get("question") or ""
        market_id = str(market.get("id") or market.get("conditionId") or slug)

        # Gamma-level fallbacks (single best bid/ask for the market's YES token)
        gamma_bid = _to_float(market.get("bestBid"))
        gamma_ask = _to_float(market.get("bestAsk"))
        gamma_spread = _to_float(market.get("spread"))

        result: list[MarketSnapshot] = []
        for idx, (outcome, token_id) in enumerate(zip(outcomes, token_ids)):
            token_id = str(token_id)
            best_bid = best_ask = midpoint = spread = None

            if enrich:
                quote = self.client.get_quote(token_id)
                best_bid = quote["best_bid"]
                best_ask = quote["best_ask"]
                midpoint = quote["midpoint"]
                spread = quote["spread"]

            # Fall back to Gamma fields / outcomePrices when CLOB is unavailable.
            if best_bid is None or best_ask is None:
                implied = _to_float(prices[idx]) if idx < len(prices) else None
                if idx == 0 and gamma_bid is not None and gamma_ask is not None:
                    best_bid = best_bid if best_bid is not None else gamma_bid
                    best_ask = best_ask if best_ask is not None else gamma_ask
                elif implied is not None:
                    # Use implied price as both sides (no live book available).
                    best_bid = best_bid if best_bid is not None else implied
                    best_ask = best_ask if best_ask is not None else implied

            best_bid = best_bid or 0.0
            best_ask = best_ask or 0.0
            if midpoint is None:
                midpoint = (best_bid + best_ask) / 2.0 if (best_bid or best_ask) else 0.0
            if spread is None:
                spread = max(0.0, best_ask - best_bid)
            if spread == 0.0 and gamma_spread is not None and idx == 0:
                spread = gamma_spread

            result.append(
                MarketSnapshot(
                    market_id=market_id,
                    slug=slug,
                    question=question,
                    category=category,
                    outcome=str(outcome),
                    token_id=token_id,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    midpoint=midpoint,
                    spread=spread,
                    volume=volume,
                    liquidity=liquidity,
                    end_date=end_date,
                    hours_to_close=htc,
                )
            )
        return result

    def is_tradable(self, snap: MarketSnapshot) -> bool:
        """Pre-strategy market-level filter (does not consider price thresholds)."""
        if snap.liquidity < self.config.min_liquidity:
            return False
        if snap.spread > self.config.max_spread:
            return False
        if snap.hours_to_close is not None and snap.hours_to_close < self.config.min_hours_to_close:
            return False
        if snap.best_ask <= 0 or snap.best_ask >= 1:
            return False
        return True
