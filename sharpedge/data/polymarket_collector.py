"""Polymarket metadata and order-book collection.

The collector uses Gamma for market metadata and CLOB for executable order-book
state when configured for real collection. Mock mode is fully offline and is the
default path for tests and demos.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sharpedge.config.settings import Settings
from sharpedge.data.schemas import Market, OrderBook, PriceLevel
from sharpedge.data.storage import Storage

LOGGER = logging.getLogger(__name__)
SAMPLES_DIR = Path(__file__).resolve().parent / "samples"

SPORT_KEYWORDS = (
    "ufc",
    "mma",
    "mixed martial arts",
    "soccer",
    "football",
    "premier league",
    "champions league",
    "europa league",
    "la liga",
    "serie a",
    "bundesliga",
    "ligue 1",
    "mls",
    "world cup",
)


def _safe_json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).astimezone(UTC)
        except ValueError:
            return None
    return None


def _looks_like_target_sport(payload: dict[str, Any], keywords: Iterable[str]) -> bool:
    text = " ".join(
        str(payload.get(key) or "")
        for key in (
            "question",
            "title",
            "slug",
            "description",
            "category",
            "tags",
            "eventSlug",
            "seriesSlug",
        )
    ).lower()
    return any(keyword.lower() in text for keyword in keywords)


def parse_gamma_market(payload: dict[str, Any]) -> Market | None:
    """Defensively parse a Gamma market payload into the internal schema."""

    market_id = str(
        payload.get("conditionId")
        or payload.get("condition_id")
        or payload.get("id")
        or payload.get("market_id")
        or ""
    )
    outcomes = _safe_json_list(payload.get("outcomes") or payload.get("outcomeNames"))
    token_ids = _safe_json_list(
        payload.get("clobTokenIds")
        or payload.get("clob_token_ids")
        or payload.get("tokenIds")
        or payload.get("token_ids")
    )
    if not market_id or not outcomes or not token_ids:
        LOGGER.debug("Skipping Gamma market without ids/outcomes/tokens: %s", payload)
        return None
    if len(outcomes) != len(token_ids):
        count = min(len(outcomes), len(token_ids))
        outcomes = outcomes[:count]
        token_ids = token_ids[:count]
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    event_id = str(
        payload.get("eventId")
        or payload.get("event_id")
        or event.get("id")
        or payload.get("slug")
        or market_id
    )
    title = str(
        payload.get("question")
        or payload.get("title")
        or event.get("title")
        or event.get("slug")
        or market_id
    )
    sport = infer_sport(payload)
    return Market(
        market_id=market_id,
        event_id=event_id,
        sport=sport,
        league=str(payload.get("league") or payload.get("seriesSlug") or "") or None,
        title=title,
        description=payload.get("description"),
        outcomes=outcomes,
        token_ids=token_ids,
        start_time=_parse_datetime(
            payload.get("startDate")
            or payload.get("start_date")
            or payload.get("gameStartTime")
            or event.get("startTime")
        ),
        end_time=_parse_datetime(payload.get("endDate") or payload.get("end_date")),
        resolution_source=payload.get("resolutionSource")
        or payload.get("resolution_source"),
        status=str(
            payload.get("status")
            or ("closed" if payload.get("closed") else "active")
            if "closed" in payload
            else "unknown"
        ),
        raw_metadata=payload,
    )


def infer_sport(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, default=str).lower()
    if any(word in text for word in ("ufc", "mma", "fighter", "fight")):
        return "MMA"
    if any(
        word in text
        for word in (
            "soccer",
            "football",
            "premier league",
            "champions league",
            "la liga",
            "serie a",
            "bundesliga",
        )
    ):
        return "Soccer"
    return str(payload.get("sport") or payload.get("category") or "Sports")


class PolymarketCollector:
    """Collect Polymarket sports market metadata and CLOB books."""

    def __init__(
        self,
        settings: Settings,
        storage: Storage | None = None,
        mock: bool = False,
        sample_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.mock = mock
        self.sample_dir = sample_dir or SAMPLES_DIR

    def fetch_active_sports_markets(
        self, keywords: Iterable[str] = SPORT_KEYWORDS, limit: int = 250
    ) -> list[Market]:
        if self.mock:
            return self._load_mock_markets(keywords)
        payloads = self._get_gamma_markets(limit=limit)
        markets: list[Market] = []
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            if not _looks_like_target_sport(payload, keywords):
                continue
            market = parse_gamma_market(payload)
            if market:
                markets.append(market)
        return markets

    def fetch_order_book(self, market_id: str, token_id: str) -> OrderBook | None:
        if self.mock:
            for book in self._load_mock_orderbooks():
                if book.market_id == market_id and book.token_id == token_id:
                    return book
            return None
        try:
            payload = self._request_json(
                f"{self.settings.polymarket_clob_base.rstrip('/')}/book",
                params={"token_id": token_id},
            )
        except Exception as exc:
            LOGGER.warning("Failed to fetch CLOB book for %s: %s", token_id, exc)
            return None
        return parse_clob_order_book(market_id=market_id, token_id=token_id, payload=payload)

    def collect_snapshot(self) -> tuple[list[Market], list[OrderBook]]:
        markets = self.fetch_active_sports_markets()
        orderbooks: list[OrderBook] = []
        for market in markets:
            for token_id in market.token_ids:
                book = self.fetch_order_book(market.market_id, token_id)
                if book:
                    orderbooks.append(book)
        if self.storage:
            self.storage.insert_markets(markets)
            self.storage.insert_orderbooks(orderbooks)
        return markets, orderbooks

    def _get_gamma_markets(self, limit: int) -> list[dict[str, Any]]:
        url = f"{self.settings.polymarket_gamma_base.rstrip('/')}/markets"
        params = {"active": "true", "closed": "false", "limit": str(limit)}
        payload = self._request_json(url, params=params)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("markets", "data", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def _request_json(
        self, url: str, params: dict[str, str] | None = None, attempts: int = 3
    ) -> Any:
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx is required for real Polymarket collection") from exc

        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = httpx.get(url, params=params, timeout=20)
                if response.status_code in {429, 500, 502, 503, 504}:
                    time.sleep(0.5 * attempt)
                    continue
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_exc = exc
                time.sleep(0.5 * attempt)
        raise RuntimeError(f"Request failed after {attempts} attempts: {url}") from last_exc

    def _load_mock_markets(self, keywords: Iterable[str]) -> list[Market]:
        with (self.sample_dir / "polymarket_markets.json").open(encoding="utf-8") as handle:
            payload = json.load(handle)
        markets_payload = payload.get("markets", payload) if isinstance(payload, dict) else payload
        markets: list[Market] = []
        for row in markets_payload:
            if not isinstance(row, dict):
                continue
            if not _looks_like_target_sport(row, keywords):
                continue
            try:
                markets.append(Market(**row))
            except Exception:
                parsed = parse_gamma_market(row)
                if parsed:
                    markets.append(parsed)
        return markets

    def _load_mock_orderbooks(self) -> list[OrderBook]:
        with (self.sample_dir / "orderbooks.json").open(encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload.get("orderbooks", payload) if isinstance(payload, dict) else payload
        books: list[OrderBook] = []
        for row in rows:
            try:
                books.append(OrderBook(**row))
            except Exception as exc:
                LOGGER.warning("Skipping invalid mock orderbook: %s", exc)
        return books


def parse_clob_order_book(
    market_id: str, token_id: str, payload: dict[str, Any]
) -> OrderBook | None:
    """Parse the CLOB book response, accepting multiple known field shapes."""

    if not isinstance(payload, dict):
        return None
    bids_payload = payload.get("bids") or payload.get("buy") or []
    asks_payload = payload.get("asks") or payload.get("sell") or []
    bids = [_parse_level(level) for level in bids_payload if _parse_level(level)]
    asks = [_parse_level(level) for level in asks_payload if _parse_level(level)]
    timestamp = _parse_datetime(payload.get("timestamp")) or datetime.now(UTC)
    return OrderBook(
        market_id=market_id,
        token_id=token_id,
        timestamp=timestamp,
        bids=bids,
        asks=asks,
    )


def _parse_level(level: Any) -> PriceLevel | None:
    try:
        if isinstance(level, dict):
            price = level.get("price") or level.get("p")
            size = level.get("size") or level.get("s")
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price, size = level[0], level[1]
        else:
            return None
        return PriceLevel(price=float(price), size=float(size))
    except Exception:
        return None
