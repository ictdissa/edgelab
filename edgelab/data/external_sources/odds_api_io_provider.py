"""Odds-API.io sports odds adapter."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from edgelab.config.settings import Settings
from edgelab.data.cache import CachedHttpClient, HttpGet, RollingRateLimiter
from edgelab.data.external_sources.odds_provider import (
    ProviderDiagnosis,
    SportsOddsProvider,
    classify_provider_failure,
    clean_params,
    credentials_for_provider,
    is_success_status,
    key_fingerprint,
    parse_timestamp,
    payload_count,
    recommendation_for_failure,
    redact_secret_text,
    validate_decimal_odds,
)
from edgelab.data.schemas import ExternalAnchor, MarketCategory, ensure_utc

LOGGER = logging.getLogger(__name__)


class OddsAPIIOProvider(SportsOddsProvider):
    """Odds-API.io adapter for https://api.odds-api.io/v3."""

    provider_name = "odds_api_io"

    def __init__(
        self,
        settings: Settings,
        http_get: HttpGet | None = None,
        refresh: bool = False,
    ) -> None:
        self.settings = settings
        self.credentials = credentials_for_provider(settings, self.provider_name)
        self.api_key = self.credentials.api_key
        self.base_url = settings.odds_api_io_base.rstrip("/")
        self.refresh = refresh
        limiter = RollingRateLimiter(
            settings.odds_api_io_rate_limit_state,
            settings.odds_api_io_max_requests_per_hour,
            window_seconds=3600,
        )
        self.client = CachedHttpClient(settings.cache_dir, self.provider_name, limiter, http_get)

    def diagnose(self, sport: str, limit: int = 3) -> ProviderDiagnosis:
        path = "/events"
        if not self.credentials.has_key:
            return self._diagnosis_without_call(
                sport=sport,
                path=path,
                failure_reason="key missing",
            )
        params = {
            "apiKey": self.api_key,
            "sport": sport,
            "status": "pending,live",
            "limit": str(limit),
        }
        return self._diagnose_request(path=path, params=params, sport=sport)

    def list_sports(self, refresh: bool = False) -> list[dict[str, Any]]:
        _ = refresh
        return [
            {"key": "football", "title": "Football / Soccer"},
            {"key": "mma", "title": "Mixed Martial Arts"},
        ]

    def list_bookmakers(self, refresh: bool = False) -> list[dict[str, Any]]:
        if not self.credentials.has_key:
            raise ValueError("ODDS_API_IO_KEY is required for Odds-API.io")
        payload = self.client.get_json(
            f"{self.base_url}/bookmakers",
            params={"apiKey": self.api_key},
            refresh=refresh or self.refresh,
        ).payload
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def list_events(
        self,
        sport: str,
        limit: int = 20,
        refresh: bool = False,
        *,
        league: str | None = None,
        skip: int = 0,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        status: str = "pending,live",
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 5000 or skip < 0:
            raise ValueError("events limit must be 1..5000 and skip nonnegative")
        if not self.credentials.has_key:
            raise ValueError("ODDS_API_IO_KEY is required for Odds-API.io")
        payload = self.client.get_json(
            f"{self.base_url}/events",
            params={
                "apiKey": self.api_key,
                "sport": sport,
                "status": status,
                "limit": str(limit),
                "league": league,
                "skip": str(skip) if skip else None,
                "from": ensure_utc(from_time).isoformat() if from_time else None,
                "to": ensure_utc(to_time).isoformat() if to_time else None,
            },
            refresh=refresh or self.refresh,
        ).payload
        if not isinstance(payload, list) or any(not isinstance(event, dict) for event in payload):
            raise ValueError("unexpected events response schema; coverage cannot be established")
        return payload[:limit]

    def fetch_odds(
        self,
        sport: str | None = None,
        limit: int | None = None,
        refresh: bool = False,
    ) -> list[ExternalAnchor]:
        selected_sport = sport or "football"
        max_events = limit or max(1, self.settings.odds_provider_event_limit)
        events = self.list_events(selected_sport, limit=max_events, refresh=refresh)
        event_ids = [str(event.get("id")) for event in events if event.get("id") is not None]
        payloads = self.fetch_odds_payloads(event_ids, refresh=refresh)
        event_metadata = {str(e.get("id")): e for e in events}
        payloads = [
            {**event_metadata.get(str(p.get("id") or p.get("eventId")), {}), **p} for p in payloads
        ]
        anchors = self.normalize_to_external_odds(payloads)
        LOGGER.info(
            "odds_api_io_anchors_collected sport=%s events=%d anchors=%d",
            selected_sport,
            len(events),
            len(anchors),
        )
        return anchors

    def fetch_odds_payloads(
        self, event_ids: Sequence[str], refresh: bool = False
    ) -> list[dict[str, Any]]:
        if not self.credentials.has_key:
            raise ValueError("ODDS_API_IO_KEY is required for Odds-API.io")
        bookmakers = self._validated_bookmakers(refresh=refresh)
        payloads: list[dict[str, Any]] = []
        ids = [str(event_id) for event_id in event_ids if str(event_id).strip()]
        for chunk in _chunks(ids, 10):
            if len(chunk) == 1:
                payload = self._odds_json_with_bookmaker_fallback(
                    f"{self.base_url}/odds",
                    params={
                        "apiKey": self.api_key,
                        "eventId": chunk[0],
                        "bookmakers": bookmakers,
                    },
                    refresh=refresh or self.refresh,
                )
            else:
                payload = self._odds_json_with_bookmaker_fallback(
                    f"{self.base_url}/odds/multi",
                    params={
                        "apiKey": self.api_key,
                        "eventIds": ",".join(chunk),
                        "bookmakers": bookmakers,
                    },
                    refresh=refresh or self.refresh,
                )
            if isinstance(payload, list):
                payloads.extend(item for item in payload if isinstance(item, dict))
            elif isinstance(payload, dict):
                payloads.append(payload)
        return payloads

    def normalize_to_external_odds(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> list[ExternalAnchor]:
        anchors: list[ExternalAnchor] = []
        fallback_timestamp = datetime.fromtimestamp(0, UTC)
        for event in payloads:
            event_id = str(event.get("id") or event.get("eventId") or event.get("eventid") or "")
            if not event_id:
                continue
            home = _string_or_none(event.get("home") or event.get("homeTeam"))
            away = _string_or_none(event.get("away") or event.get("awayTeam"))
            sport = _event_sport(event)
            for book, markets in _iter_bookmakers(event.get("bookmakers")):
                for market in markets:
                    market_name = str(
                        market.get("name") or market.get("market") or market.get("key") or ""
                    )
                    odds_entries = market.get("odds") or market.get("prices") or []
                    if isinstance(odds_entries, Mapping):
                        odds_entries = [odds_entries]
                    if not isinstance(odds_entries, list):
                        continue
                    category = _anchor_category(market_name, sport, odds_entries)
                    updated = parse_timestamp(market.get("updatedAt")) or fallback_timestamp
                    for odds_entry in odds_entries:
                        if not isinstance(odds_entry, Mapping):
                            continue
                        line = (
                            odds_entry.get("hdp")
                            or odds_entry.get("line")
                            or odds_entry.get("marketLine")
                        )
                        for outcome_key, raw_odds in odds_entry.items():
                            if _is_metadata_odds_key(str(outcome_key)):
                                continue
                            try:
                                decimal_odds = validate_decimal_odds(float(raw_odds))
                            except (TypeError, ValueError):
                                continue
                            outcome = _canonical_outcome_name(
                                str(outcome_key),
                                home=home,
                                away=away,
                                line=line,
                            )
                            anchors.append(
                                ExternalAnchor(
                                    source_id=(
                                        f"odds-api-io:{event_id}:{book}:{market_name}:{outcome}"
                                    ),
                                    event_id=event_id,
                                    source=book,
                                    category=category,
                                    outcome=outcome,
                                    decimal_odds=decimal_odds,
                                    timestamp=updated,
                                    available_at=parse_timestamp(event.get("_available_at")),
                                    metadata={
                                        "provider": self.provider_name,
                                        "cache_verified": event.get("_cache_verified", True),
                                        "sport_key": sport,
                                        "home": home or "",
                                        "away": away or "",
                                        "event_title": _event_title(home, away),
                                        "start_time": event.get("date") or event.get("startTime"),
                                        "league": event.get("league", {}).get("name")
                                        if isinstance(event.get("league"), dict)
                                        else event.get("league"),
                                        "league_slug": event.get("league", {}).get("slug")
                                        if isinstance(event.get("league"), dict)
                                        else None,
                                        "market_name": market_name,
                                    },
                                )
                            )
        return anchors

    def _validated_bookmakers(self, refresh: bool = False) -> str:
        requested = [
            item.strip() for item in self.settings.odds_api_io_bookmakers.split(",") if item.strip()
        ]
        bookmakers = self.list_bookmakers(refresh=refresh)
        active_names = [
            str(row.get("name") or "")
            for row in bookmakers
            if row.get("active", True) and str(row.get("name") or "").strip()
        ]
        names_by_lower = {name.lower(): name for name in active_names}
        selected: list[str] = []
        dropped: list[str] = []
        for name in requested:
            canonical = names_by_lower.get(name.lower())
            if canonical:
                selected.append(canonical)
            else:
                dropped.append(name)
        if dropped:
            LOGGER.warning(
                "odds_api_io_dropped_invalid_bookmakers requested=%s",
                ",".join(dropped),
            )
        if not selected:
            preferred = ["Bet365", "Unibet"]
            selected = [name for name in preferred if name.lower() in names_by_lower]
        if not selected:
            selected = active_names[:2]
        if not selected:
            raise RuntimeError("Odds-API.io /bookmakers returned no active bookmaker names")
        return ",".join(dict.fromkeys(selected))

    def _odds_json_with_bookmaker_fallback(
        self,
        url: str,
        params: Mapping[str, Any],
        refresh: bool,
    ) -> Any:
        try:
            return self._cached_payload(url, params, refresh)
        except RuntimeError as exc:
            text = str(exc)
            if params.get("bookmakers") and "allowed:" in text.lower():
                allowed = _allowed_bookmakers_from_error(text)
                if allowed:
                    LOGGER.warning(
                        "odds_api_io_bookmaker_plan_limit retrying_allowed_bookmakers=%s",
                        ",".join(allowed),
                    )
                    retry_params = dict(params)
                    retry_params["bookmakers"] = ",".join(allowed)
                    return self._cached_payload(url, retry_params, refresh)
            if params.get("bookmakers") and "not a valid bookmaker" in text.lower():
                LOGGER.warning(
                    "odds_api_io_invalid_bookmaker_filter retrying_without_filter error=%s",
                    redact_secret_text(text, secrets=[self.api_key]),
                )
                retry_params = dict(params)
                retry_params.pop("bookmakers", None)
                return self._cached_payload(url, retry_params, refresh)
            raise

    def _cached_payload(self, url: str, params: Mapping[str, Any], refresh: bool) -> Any:
        response = self.client.get_json(url, params=params, refresh=refresh)
        if isinstance(response.payload, list):
            return [
                dict(
                    row,
                    _available_at=response.fetched_at.isoformat(),
                    _cache_verified=response.verified,
                )
                if isinstance(row, dict)
                else row
                for row in response.payload
            ]
        if isinstance(response.payload, dict):
            return dict(
                response.payload,
                _available_at=response.fetched_at.isoformat(),
                _cache_verified=response.verified,
            )
        return response.payload

    def _diagnosis_without_call(
        self,
        sport: str,
        path: str,
        failure_reason: str,
    ) -> ProviderDiagnosis:
        return ProviderDiagnosis(
            selected_provider=self.provider_name,
            base_url=self.base_url,
            key_present=False,
            key_fingerprint="missing",
            key_source=self.credentials.key_source,
            endpoint_path=path,
            http_status=None,
            provider_error_body=None,
            auth_succeeded=False,
            sport_key_valid=None,
            odds_or_events_returned=False,
            returned_count=0,
            failure_reason=failure_reason,
            recommended_next_action=recommendation_for_failure(
                failure_reason,
                self.provider_name,
                self.credentials.key_source,
                sport,
            ),
        )

    def _diagnose_request(
        self,
        path: str,
        params: Mapping[str, Any],
        sport: str,
    ) -> ProviderDiagnosis:
        status_code: int | None = None
        body: str | None = None
        quota: dict[str, str] = {}
        payload: Any = None
        expected_shape_ok = False
        try:
            response = self.client.get_json(
                f"{self.base_url}{path}",
                params=clean_params(params),
                timeout=20,
                refresh=self.refresh,
            )
            status_code = 200
            quota = response.headers
            if is_success_status(status_code):
                payload = response.payload
                expected_shape_ok = isinstance(payload, list)
            else:
                body = redact_secret_text(
                    getattr(response, "text", ""),
                    secrets=[self.api_key],
                )
        except Exception as exc:
            body = redact_secret_text(str(exc), secrets=[self.api_key])
            status_code = getattr(exc, "status_code", None)
            quota = getattr(exc, "headers", {})
            failure_reason = classify_provider_failure(
                provider=self.provider_name,
                key_source=self.credentials.key_source,
                status_code=status_code,
                body=body,
                expected_shape_ok=False,
                sport_key_valid=None,
            )
            return ProviderDiagnosis(
                selected_provider=self.provider_name,
                base_url=self.base_url,
                key_present=self.credentials.has_key,
                key_fingerprint=key_fingerprint(self.api_key),
                key_source=self.credentials.key_source,
                endpoint_path=path,
                http_status=status_code,
                provider_error_body=body,
                auth_succeeded=False,
                sport_key_valid=None,
                odds_or_events_returned=False,
                returned_count=0,
                quota=quota,
                failure_reason=failure_reason,
                recommended_next_action=recommendation_for_failure(
                    failure_reason,
                    self.provider_name,
                    self.credentials.key_source,
                    sport,
                ),
            )
        count = payload_count(payload)
        sport_valid = True if is_success_status(status_code) else None
        failure_reason = classify_provider_failure(
            provider=self.provider_name,
            key_source=self.credentials.key_source,
            status_code=status_code,
            body=body,
            expected_shape_ok=expected_shape_ok,
            sport_key_valid=sport_valid,
        )
        return ProviderDiagnosis(
            selected_provider=self.provider_name,
            base_url=self.base_url,
            key_present=self.credentials.has_key,
            key_fingerprint=key_fingerprint(self.api_key),
            key_source=self.credentials.key_source,
            endpoint_path=path,
            http_status=status_code,
            provider_error_body=body,
            auth_succeeded=is_success_status(status_code),
            sport_key_valid=sport_valid,
            odds_or_events_returned=count > 0,
            returned_count=count,
            quota=quota,
            failure_reason=failure_reason,
            recommended_next_action=recommendation_for_failure(
                failure_reason,
                self.provider_name,
                self.credentials.key_source,
                sport,
            ),
        )


def _chunks(values: Sequence[str], size: int) -> list[list[str]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


def _event_sport(event: Mapping[str, Any]) -> str:
    sport = event.get("sport")
    if isinstance(sport, Mapping):
        slug = str(sport.get("slug") or sport.get("name") or "")
    else:
        slug = str(sport or "")
    normalized = slug.lower().strip()
    if normalized in {"football", "soccer"}:
        return "football"
    if normalized in {"mixed-martial-arts", "mma", "ufc"}:
        return "mma"
    return normalized or "unknown"


def _allowed_bookmakers_from_error(text: str) -> list[str]:
    match = re.search(r"Allowed:\s*([^.\"]+)", text)
    if not match:
        return []
    return [name.strip() for name in match.group(1).split(",") if name.strip()]


def _anchor_category(
    market_name: str,
    sport: str,
    odds_entries: Sequence[Any],
) -> MarketCategory:
    normalized = market_name.lower().replace("_", " ").replace("-", " ").strip()
    if normalized in {"ml", "moneyline", "match winner", "match result", "h2h", "1x2"}:
        has_draw = any(isinstance(entry, Mapping) and "draw" in entry for entry in odds_entries)
        return "sports_1x2" if sport == "football" or has_draw else "sports_moneyline"
    if "total" in normalized:
        return "sports_total"
    return "unknown"


def _canonical_outcome_name(
    outcome_key: str,
    home: str | None,
    away: str | None,
    line: Any,
) -> str:
    normalized = outcome_key.lower().strip()
    if normalized == "home" and home:
        return home
    if normalized == "away" and away:
        return away
    if normalized == "draw":
        return "Draw"
    if normalized == "over":
        return f"Over {line}" if line is not None else "Over"
    if normalized == "under":
        return f"Under {line}" if line is not None else "Under"
    if normalized in {"yes", "no"}:
        return normalized.title()
    return outcome_key


def _iter_bookmakers(bookmakers: Any) -> list[tuple[str, list[Mapping[str, Any]]]]:
    if isinstance(bookmakers, Mapping):
        parsed: list[tuple[str, list[Mapping[str, Any]]]] = []
        for book, markets in bookmakers.items():
            if isinstance(markets, list):
                parsed.append((str(book), [m for m in markets if isinstance(m, Mapping)]))
        return parsed
    if isinstance(bookmakers, list):
        parsed = []
        for bookmaker in bookmakers:
            if not isinstance(bookmaker, Mapping):
                continue
            book = str(
                bookmaker.get("name")
                or bookmaker.get("title")
                or bookmaker.get("key")
                or bookmaker.get("bookie")
                or "unknown"
            )
            markets = bookmaker.get("markets") or bookmaker.get("odds") or []
            if isinstance(markets, list):
                parsed.append((book, [m for m in markets if isinstance(m, Mapping)]))
        return parsed
    return []


def _is_metadata_odds_key(key: str) -> bool:
    normalized = key.lower()
    return normalized in {
        "hdp",
        "line",
        "marketline",
        "max",
        "min",
        "label",
        "name",
        "timestamp",
        "updatedat",
    } or normalized.endswith("directlink")


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _event_title(home: str | None, away: str | None) -> str:
    if home and away:
        return f"{home} vs {away}"
    return home or away or ""
