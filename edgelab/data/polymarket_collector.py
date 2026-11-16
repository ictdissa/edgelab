"""Polymarket market discovery and CLOB order-book collection."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Mapping

from edgelab.config.settings import Settings
from edgelab.data.cache import CachedHttpClient, HttpGet, RollingRateLimiter, atomic_json
from edgelab.data.external_sources.mock_provider import MockProvider
from edgelab.data.external_sources.odds_provider import parse_timestamp
from edgelab.data.schemas import OrderBook, PredictionMarket, PriceLevel, ExternalAnchor
from edgelab.matching.market_classifier import classify_market

LOGGER = logging.getLogger(__name__)

SPORT_CATEGORIES = {
    "sports_moneyline",
    "sports_1x2",
    "sports_total",
    "sports_exact_score",
    "sports_outright",
}

CRYPTO_CATEGORIES = {"crypto_threshold"}

CATEGORY_SEARCH_QUERIES = {
    "sports": ("soccer", "football", "nba", "nhl", "mlb", "ufc"),
    "crypto": ("bitcoin", "btc", "ethereum", "eth"),
    "weather": ("temperature", "rain", "weather"),
}


class PolymarketCollector:
    def __init__(
        self,
        settings: Settings,
        mock: bool = True,
        http_get: HttpGet | None = None,
    ) -> None:
        self.settings = settings
        self.mock = mock
        limiter = RollingRateLimiter(
            settings.cache_dir / "rate_limits" / "polymarket.json",
            settings.polymarket_rate_limit_per_minute,
        )
        self.client = CachedHttpClient(settings.cache_dir, "polymarket", limiter, http_get)

    def fetch_markets(
        self,
        category: str | None = None,
        limit: int = 50,
        refresh: bool = False,
    ) -> list[PredictionMarket]:
        if self.mock:
            return _filter_category(MockProvider().markets(), category)[:limit]
        url = f"{self.settings.polymarket_gamma_base.rstrip('/')}/markets"
        response = self.client.get_json(
            url,
            params={"active": "true", "closed": "false", "limit": str(max(limit * 5, limit))},
            refresh=refresh,
        )
        payload = response.payload
        rows = _extract_market_rows(payload)
        rows = [
            dict(
                row,
                collected_at=response.fetched_at.isoformat(),
                metadata_cache_verified=response.verified,
            )
            for row in rows
            if isinstance(row, Mapping)
        ]
        markets: list[PredictionMarket] = []
        seen_market_ids: set[str] = set()
        self._append_parsed_markets(rows, category, limit, markets, seen_market_ids)
        if category and len(markets) < limit:
            search_rows = self._fetch_public_search_rows(category, limit, refresh)
            self._append_parsed_markets(search_rows, category, limit, markets, seen_market_ids)
        LOGGER.info("polymarket_markets_collected category=%s count=%d", category, len(markets))
        return markets

    def _append_parsed_markets(
        self,
        rows: list[Any] | list[Mapping[str, Any]],
        category: str | None,
        limit: int,
        markets: list[PredictionMarket],
        seen_market_ids: set[str],
    ) -> None:
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            market = parse_gamma_market(row)
            if not market:
                continue
            if market.market_id in seen_market_ids:
                continue
            if category and not _category_matches(market.category, category):
                continue
            seen_market_ids.add(market.market_id)
            markets.append(market)
            if len(markets) >= limit:
                break

    def fetch_orderbooks(
        self,
        markets: list[PredictionMarket] | None = None,
        limit: int = 50,
        refresh: bool = False,
    ) -> list[OrderBook]:
        if self.mock:
            return MockProvider().orderbooks()
        books: list[OrderBook] = []
        for market, yes_token in [
            (m, token) for m in (markets or [])[:limit] for token in m.token_ids
        ]:
            try:
                response = self.client.get_json(
                    f"{self.settings.polymarket_clob_base.rstrip('/')}/book",
                    params={"token_id": yes_token},
                    refresh=refresh,
                    timeout=10,
                )
            except Exception as exc:
                LOGGER.warning(
                    "polymarket_orderbook_fetch_failed market_id=%s token_id=%s error=%s",
                    market.market_id,
                    yes_token,
                    exc,
                )
                continue
            book = parse_clob_order_book(
                market.market_id, yes_token, response.payload, observed_at=response.fetched_at
            )
            if book:
                if not response.verified:
                    book.timestamp = datetime.fromtimestamp(0, UTC)
                books.append(book)
        LOGGER.info("polymarket_orderbooks_collected count=%d", len(books))
        return books

    def fetch_fixture_markets(
        self, anchors: list[ExternalAnchor], limit: int = 50, refresh: bool = False
    ) -> list[PredictionMarket]:
        """Search only participant pairs represented in the external fixture feed."""
        events: dict[str, list[ExternalAnchor]] = {}
        for anchor in anchors:
            if anchor.category in {"sports_1x2", "sports_moneyline"}:
                events.setdefault(anchor.event_id, []).append(anchor)
        markets: list[PredictionMarket] = []
        seen: set[str] = set()
        queries: list[dict[str, Any]] = []
        for event_id, rows in events.items():
            meta = rows[0].metadata
            teams = sorted(
                {
                    a.outcome
                    for a in rows
                    if a.outcome.casefold() != "draw"
                    and not a.outcome.lower().startswith(("lay", "depth"))
                }
            )
            home, away = meta.get("home"), meta.get("away")
            if not home or not away:
                if len(teams) != 2:
                    queries.append({"event_id": event_id, "status": "missing_participant_pair"})
                    continue
                home, away = teams
            for query in (f"{home} vs {away}", f"{away} vs {home}"):
                if len(markets) >= limit:
                    break
                audit: dict[str, Any] = {"event_id": event_id, "query": query}
                try:
                    response = self.client.get_json(
                        f"{self.settings.polymarket_gamma_base.rstrip('/')}/public-search",
                        params={
                            "q": query,
                            "limit_per_type": "25",
                            "keep_closed_markets": "0",
                            "search_profiles": "false",
                        },
                        refresh=refresh,
                    )
                    payload = response.payload
                    if _public_search_needs_hydration(payload):
                        payload = self._hydrate_public_search_payload(
                            payload, refresh, max_events=2
                        )
                    candidates = [
                        _observed_search_row(r, response.fetched_at, response.verified)
                        for r in _extract_public_search_market_rows(payload)
                    ]
                    self._append_parsed_markets(candidates, "sports", limit, markets, seen)
                    audit.update(
                        status="ok", candidates=len(candidates), cache_hit=response.cache_hit
                    )
                except (RuntimeError, ValueError) as exc:
                    audit.update(status="error", reason=str(exc)[:300])
                queries.append(audit)
        atomic_json(self.settings.live_data_dir / "sports_discovery_queries.json", queries)
        LOGGER.info(
            "fixture_discovery events=%d queries=%d candidates=%d",
            len(events),
            len(queries),
            len(markets),
        )
        return markets

    def _fetch_public_search_rows(
        self,
        category: str,
        limit: int,
        refresh: bool,
    ) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        for query in CATEGORY_SEARCH_QUERIES.get(category, ()):
            response = self.client.get_json(
                f"{self.settings.polymarket_gamma_base.rstrip('/')}/public-search",
                params={
                    "q": query,
                    "limit_per_type": str(max(25, min(limit, 100))),
                    "keep_closed_markets": "0",
                    "search_profiles": "false",
                    "optimized": "true",
                },
                refresh=refresh,
            )
            payload = response.payload
            if _public_search_needs_hydration(payload):
                payload = self._hydrate_public_search_payload(payload, refresh, max_events=4)
            rows.extend(
                _observed_search_row(row, response.fetched_at, response.verified)
                for row in _extract_public_search_market_rows(payload)
            )
        return rows

    def _hydrate_public_search_payload(self, payload: Any, refresh: bool, max_events: int) -> Any:
        if not isinstance(payload, Mapping):
            return payload
        hydrated_events: list[Any] = []
        hydrated_count = 0
        for event in payload.get("events", []) or []:
            if not isinstance(event, Mapping):
                hydrated_events.append(event)
                continue
            slug = str(event.get("slug") or "")
            if not slug or hydrated_count >= max_events:
                hydrated_events.append(event)
                continue
            try:
                hydration = self.client.get_json(
                    f"{self.settings.polymarket_gamma_base.rstrip('/')}/events/slug/{slug}",
                    refresh=refresh,
                )
                hydrated = hydration.payload
                if isinstance(hydrated, Mapping):
                    hydrated = dict(hydrated)
                    hydrated["markets"] = [
                        dict(
                            m,
                            _metadata_available_at=hydration.fetched_at.isoformat(),
                            _metadata_cache_verified=hydration.verified,
                        )
                        for m in hydrated.get("markets", [])
                        if isinstance(m, Mapping)
                    ]
            except Exception as exc:
                LOGGER.warning("polymarket_event_hydration_failed slug=%s error=%s", slug, exc)
                hydrated_events.append(event)
                continue
            hydrated_count += 1
            hydrated_events.append(hydrated if isinstance(hydrated, Mapping) else event)
        output = dict(payload)
        output["events"] = hydrated_events
        return output


def _observed_search_row(
    row: Mapping[str, Any], search_at: datetime, verified: bool
) -> dict[str, Any]:
    """Full hydrated metadata cannot have the earlier search response's receipt time."""
    hydrated_at = _parse_datetime(row.get("_metadata_available_at")) or search_at
    return dict(
        row,
        collected_at=max(search_at, hydrated_at).isoformat(),
        metadata_cache_verified=verified and row.get("_metadata_cache_verified", True),
    )


def parse_gamma_market(payload: Mapping[str, Any]) -> PredictionMarket | None:
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
    if not market_id or not outcomes:
        return None
    if token_ids and len(outcomes) != len(token_ids):
        return None
    event_raw = payload.get("event")
    event = event_raw if isinstance(event_raw, Mapping) else {}
    title = str(payload.get("question") or payload.get("title") or event.get("title") or "")
    description = str(payload.get("description") or "") or None
    category = classify_market(title, description, outcomes)
    event_id = str(
        payload.get("eventId") or payload.get("event_id") or event.get("id") or market_id
    )
    resolution_source = str(
        payload.get("resolutionSource") or payload.get("resolution_source") or ""
    )
    if not resolution_source:
        resolution_source = infer_resolution_source_from_text(description)
    return PredictionMarket(
        market_id=market_id,
        event_id=event_id,
        category=category,
        title=title or market_id,
        description=description,
        outcomes=outcomes,
        token_ids=token_ids,
        start_time=_parse_datetime(
            payload.get("gameStartTime") or payload.get("start_date") or event.get("startTime")
        ),
        end_time=_parse_datetime(payload.get("endDate") or payload.get("end_date")),
        resolution_source=resolution_source,
        status="closed"
        if payload.get("closed")
        else "active"
        if payload.get("active", True)
        else "inactive",
        raw_metadata={**dict(payload), "fixture": event.get("title", "")},
    )


def infer_resolution_source_from_text(description: str | None) -> str:
    if not description:
        return ""
    compact = re.sub(r"\s+", " ", description).strip()
    patterns = [
        r"(?:the )?resolution source(?: for [^.]*)? is ([^.]+)",
        r"(?:the )?primary resolution source(?: for [^.]*)? is ([^.]+)",
        r"(?:prices?|data) (?:from|available at) ([^.]+) will (?:be )?(?:used|considered)",
    ]
    matches: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, compact, flags=re.IGNORECASE):
            value = match.group(1).strip(" .")
            if value:
                matches.append(value)
    if matches:
        unique = list(dict.fromkeys(matches))
        return "; ".join(unique[:3])
    if "resolution source" in compact.lower():
        return "resolution source described in market text"
    return ""


def _extract_market_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("markets", "data", "results"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return rows
    return []


def _extract_public_search_market_rows(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    rows: list[Mapping[str, Any]] = []
    for row in payload.get("markets", []) or []:
        if isinstance(row, Mapping):
            rows.append(row)
    for event in payload.get("events", []) or []:
        if not isinstance(event, Mapping):
            continue
        for market in event.get("markets", []) or []:
            if not isinstance(market, Mapping):
                continue
            merged = dict(market)
            merged.setdefault("event", event)
            merged.setdefault("eventId", event.get("id"))
            rows.append(merged)
    return rows


def _public_search_needs_hydration(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    for event in payload.get("events", []) or []:
        if not isinstance(event, Mapping):
            continue
        for market in event.get("markets", []) or []:
            if not isinstance(market, Mapping):
                continue
            if market.get("conditionId") and market.get("clobTokenIds"):
                return False
            if market.get("slug"):
                return True
    return False


def parse_clob_order_book(
    market_id: str, token_id: str, payload: Mapping[str, Any], observed_at: datetime | None = None
) -> OrderBook | None:
    if not isinstance(payload, Mapping):
        return None
    bids = [
        _parse_level(level) for level in payload.get("bids", []) or payload.get("buy", []) or []
    ]
    asks = [
        _parse_level(level) for level in payload.get("asks", []) or payload.get("sell", []) or []
    ]
    timestamp = _parse_datetime(payload.get("timestamp")) or datetime.fromtimestamp(0, UTC)
    return OrderBook(
        market_id=market_id,
        token_id=token_id,
        timestamp=timestamp,
        available_at=observed_at,
        bids=[level for level in bids if level],
        asks=[level for level in asks if level],
    )


def _parse_level(level: Any) -> PriceLevel | None:
    try:
        if isinstance(level, Mapping):
            price = level.get("price") or level.get("p")
            size = level.get("size") or level.get("s")
        elif isinstance(level, list | tuple) and len(level) >= 2:
            price, size = level[0], level[1]
        else:
            return None
        if price is None or size is None:
            return None
        return PriceLevel(price=float(price), size=float(size))
    except (TypeError, ValueError):
        return None


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
    return parse_timestamp(value)


def _yes_token_id(market: PredictionMarket) -> str | None:
    for outcome, token_id in zip(market.outcomes, market.token_ids, strict=False):
        if outcome.lower() == "yes":
            return token_id
    return market.token_ids[0] if market.token_ids else None


def _category_matches(market_category: str, requested: str) -> bool:
    if requested == "sports":
        return market_category in SPORT_CATEGORIES
    if requested == "crypto":
        return market_category in CRYPTO_CATEGORIES
    if requested == "weather":
        return market_category == "weather_threshold"
    return market_category == requested


def _filter_category(
    markets: list[PredictionMarket], category: str | None
) -> list[PredictionMarket]:
    if not category:
        return markets
    return [market for market in markets if _category_matches(market.category, category)]
