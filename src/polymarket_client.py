"""Read-only Polymarket API client (Gamma + CLOB).

This client ONLY performs public, unauthenticated GET requests:

* Gamma API  (``/markets``)        -> market discovery & metadata
* CLOB  API  (``/price``,
              ``/midpoint``,
              ``/spread``,
              ``/book``)           -> live order-book pricing

There is deliberately no authentication, no order placement and no wallet
interaction. The client cannot trade. All write/trade endpoints are simply
not implemented.

References (see README "Investigación"):
* Polymarket docs:        https://docs.polymarket.com/
* py-clob-client:         https://github.com/Polymarket/py-clob-client
* Gamma fields confirmed against the live API and community guides.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .config import Config
from .logger import get_logger

log = get_logger("client")


class PolymarketClient:
    def __init__(self, config: Config, client: httpx.Client | None = None):
        self.config = config
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "polymarket-paper-bot/1.0 (educational)"},
        )

    # -- low level ----------------------------------------------------------
    def _get(self, base: str, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{base}{path}"
        try:
            resp = self._client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            log.warning("GET %s failed: %s", url, exc)
            return None

    # -- Gamma: market discovery -------------------------------------------
    def get_markets(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        limit: int = 200,
        offset: int = 0,
        order: str = "volume24hr",
        ascending: bool = False,
        liquidity_num_min: float | None = None,
        volume_num_min: float | None = None,
        end_date_min: str | None = None,
        tag_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch markets from the Gamma API.

        Returns the raw list of market dicts (possibly empty on failure).
        """
        params: dict[str, Any] = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if liquidity_num_min is not None:
            params["liquidity_num_min"] = liquidity_num_min
        if volume_num_min is not None:
            params["volume_num_min"] = volume_num_min
        if end_date_min is not None:
            params["end_date_min"] = end_date_min
        if tag_id is not None:
            params["tag_id"] = tag_id

        data = self._get(self.config.gamma_api_url, "/markets", params)
        if data is None:
            return []
        # Gamma may return a bare list or {"data": [...]}
        if isinstance(data, dict):
            data = data.get("data", [])
        return data if isinstance(data, list) else []

    def get_market(self, market_id: str) -> dict[str, Any] | None:
        """Fetch a single market by its Gamma id (used to check resolution).

        Tries ``/markets/{id}`` first, then falls back to ``/markets?id=`` and a
        slug lookup. Returns ``None`` when the market can't be found. This is the
        path used to detect that a market we hold has *resolved* (closed + final
        outcome prices), so the position can be settled to 0/1.
        """
        if not market_id:
            return None
        data = self._get(self.config.gamma_api_url, f"/markets/{market_id}")
        if isinstance(data, dict) and (data.get("id") or data.get("conditionId")):
            return data
        # Fallback: query endpoints that accept id / slug filters.
        for key in ("id", "slug"):
            data = self._get(self.config.gamma_api_url, "/markets", {key: market_id})
            if isinstance(data, dict):
                data = data.get("data", [])
            if isinstance(data, list) and data:
                return data[0]
        return None

    # -- CLOB: live pricing -------------------------------------------------
    def get_price(self, token_id: str, side: str = "buy") -> float | None:
        """Best price for a side. ``side='buy'`` -> best ask, ``side='sell'`` -> best bid."""
        data = self._get(
            self.config.clob_api_url, "/price", {"token_id": token_id, "side": side}
        )
        if isinstance(data, dict) and "price" in data:
            return _to_float(data["price"])
        return None

    def get_midpoint(self, token_id: str) -> float | None:
        data = self._get(self.config.clob_api_url, "/midpoint", {"token_id": token_id})
        if isinstance(data, dict) and "mid" in data:
            return _to_float(data["mid"])
        return None

    def get_spread(self, token_id: str) -> float | None:
        data = self._get(self.config.clob_api_url, "/spread", {"token_id": token_id})
        if isinstance(data, dict) and "spread" in data:
            return _to_float(data["spread"])
        return None

    def get_book(self, token_id: str) -> dict[str, Any] | None:
        """Full order book: {'bids': [...], 'asks': [...]}."""
        data = self._get(self.config.clob_api_url, "/book", {"token_id": token_id})
        return data if isinstance(data, dict) else None

    def get_quote(self, token_id: str) -> dict[str, float | None]:
        """Combined best_bid / best_ask / midpoint / spread for a token.

        Tries the order book first (one call gives both sides) and falls back to
        the dedicated endpoints. Returns ``None`` values when unavailable.
        """
        best_bid = best_ask = None
        book = self.get_book(token_id)
        if book:
            best_bid, best_ask = _best_of_book(book)

        if best_ask is None:
            best_ask = self.get_price(token_id, "buy")
        if best_bid is None:
            best_bid = self.get_price(token_id, "sell")

        midpoint = self.get_midpoint(token_id)
        if midpoint is None and best_bid is not None and best_ask is not None:
            midpoint = (best_bid + best_ask) / 2.0

        spread = self.get_spread(token_id)
        if spread is None and best_bid is not None and best_ask is not None:
            spread = max(0.0, best_ask - best_bid)

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "midpoint": midpoint,
            "spread": spread,
        }

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PolymarketClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


# -- helpers ----------------------------------------------------------------
def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _best_of_book(book: dict[str, Any]) -> tuple[float | None, float | None]:
    """Extract best bid / best ask from a CLOB order book.

    Book levels look like {"price": "0.52", "size": "100"}. Polymarket returns
    bids and asks each sorted with the best price last; we just take min/max to be
    robust to ordering.
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []

    bid_prices: list[float] = [p for p in (_to_float(lvl.get("price")) for lvl in bids) if p is not None]
    ask_prices: list[float] = [p for p in (_to_float(lvl.get("price")) for lvl in asks) if p is not None]

    best_bid = max(bid_prices) if bid_prices else None
    best_ask = min(ask_prices) if ask_prices else None
    return best_bid, best_ask


def parse_json_field(value: Any) -> list[Any]:
    """Gamma encodes ``outcomes``, ``outcomePrices`` and ``clobTokenIds`` as
    JSON-encoded strings (e.g. ``'["Yes", "No"]'``). Decode robustly."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []
