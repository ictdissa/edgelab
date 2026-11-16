"""The Odds API v4 sports odds adapter."""

from __future__ import annotations

import logging
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
    split_csv,
    validate_decimal_odds,
)
from edgelab.data.schemas import ExternalAnchor, MarketCategory

LOGGER = logging.getLogger(__name__)


class TheOddsAPIProvider(SportsOddsProvider):
    """Adapter for https://api.the-odds-api.com/v4."""

    provider_name = "the_odds_api"

    def __init__(
        self,
        settings: Settings,
        http_get: HttpGet | None = None,
        refresh: bool = False,
    ) -> None:
        self.settings = settings
        self.credentials = credentials_for_provider(settings, self.provider_name)
        self.api_key = self.credentials.api_key
        self.base_url = (settings.the_odds_api_base or settings.odds_api_base).rstrip("/")
        self.refresh = refresh
        limiter = RollingRateLimiter(
            settings.cache_dir / "rate_limits" / "the_odds_api.json",
            settings.odds_rate_limit_per_minute,
        )
        self.client = CachedHttpClient(settings.cache_dir, self.provider_name, limiter, http_get)

    def diagnose(self, sport: str, limit: int = 3) -> ProviderDiagnosis:
        path = f"/sports/{sport}/odds"
        if not self.credentials.has_key:
            return self._diagnosis_without_call(
                sport=sport,
                path=path,
                failure_reason="key missing",
            )
        params = {
            "apiKey": self.api_key,
            "regions": self.settings.the_odds_api_regions,
            "markets": "h2h",
            "oddsFormat": "decimal",
        }
        return self._diagnose_request(path=path, params=params, sport=sport, limit=limit)

    def list_sports(self, refresh: bool = False) -> list[dict[str, Any]]:
        if not self.credentials.has_key:
            raise ValueError("THE_ODDS_API_KEY is required for The Odds API")
        payload = self.client.get_json(
            f"{self.base_url}/sports",
            params={"apiKey": self.api_key},
            refresh=refresh or self.refresh,
        ).payload
        if not isinstance(payload, list):
            return []
        return [sport for sport in payload if isinstance(sport, dict)]

    def list_events(
        self,
        sport: str,
        limit: int = 20,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        if not self.credentials.has_key:
            raise ValueError("THE_ODDS_API_KEY is required for The Odds API")
        response = self.client.get_json(
            f"{self.base_url}/sports/{sport}/odds",
            params={
                "apiKey": self.api_key,
                "regions": self.settings.the_odds_api_regions,
                "markets": "h2h",
                "oddsFormat": "decimal",
            },
            refresh=refresh or self.refresh,
        )
        payload = response.payload
        if not isinstance(payload, list):
            return []
        return [
            dict(
                event,
                _available_at=response.fetched_at.isoformat(),
                _cache_verified=response.verified,
            )
            for event in payload
            if isinstance(event, dict)
        ][:limit]

    def fetch_odds(
        self,
        sport: str | None = None,
        limit: int | None = None,
        refresh: bool = False,
    ) -> list[ExternalAnchor]:
        if not self.credentials.has_key:
            LOGGER.warning("odds_provider_disabled provider=the_odds_api reason=missing_key")
            return []
        sport_keys = (
            [sport]
            if sport
            else split_csv(
                self.settings.the_odds_api_sport_keys or self.settings.odds_api_sport_keys
            )
        )
        anchors: list[ExternalAnchor] = []
        for sport_key in sport_keys:
            events = self.list_events(
                sport_key,
                limit=limit or self.settings.odds_provider_event_limit,
                refresh=refresh,
            )
            anchors.extend(self.normalize_to_external_odds(events))
        LOGGER.info(
            "the_odds_api_anchors_collected sports=%s anchors=%d",
            ",".join(sport_keys),
            len(anchors),
        )
        return anchors

    def normalize_to_external_odds(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> list[ExternalAnchor]:
        anchors: list[ExternalAnchor] = []
        for event in payloads:
            event_id = str(event.get("id") or "")
            if not event_id:
                continue
            timestamp = datetime.fromtimestamp(0, UTC)
            home = str(event.get("home_team") or "")
            away = str(event.get("away_team") or "")
            sport_key = str(event.get("sport_key") or "")
            for bookmaker in event.get("bookmakers", []) or []:
                if not isinstance(bookmaker, Mapping):
                    continue
                book_name = str(bookmaker.get("title") or bookmaker.get("key") or "the_odds_api")
                for market in bookmaker.get("markets", []) or []:
                    if not isinstance(market, Mapping) or market.get("key") != "h2h":
                        continue
                    updated = parse_timestamp(market.get("last_update")) or timestamp
                    outcomes = [
                        outcome
                        for outcome in market.get("outcomes", []) or []
                        if isinstance(outcome, Mapping)
                    ]
                    category: MarketCategory = (
                        "sports_1x2"
                        if any(
                            str(outcome.get("name") or "").lower() == "draw" for outcome in outcomes
                        )
                        else "sports_moneyline"
                    )
                    for outcome in outcomes:
                        try:
                            decimal_odds = validate_decimal_odds(float(outcome["price"]))
                        except (TypeError, ValueError):
                            continue
                        name = str(outcome.get("name") or "")
                        anchors.append(
                            ExternalAnchor(
                                source_id=f"the-odds-api:{event_id}:{book_name}:{name}",
                                event_id=event_id,
                                source=book_name,
                                category=category,
                                outcome=name,
                                decimal_odds=decimal_odds,
                                timestamp=updated,
                                available_at=parse_timestamp(event.get("_available_at")),
                                metadata={
                                    "provider": self.provider_name,
                                    "cache_verified": event.get("_cache_verified", True),
                                    "sport_key": sport_key,
                                    "home": home,
                                    "away": away,
                                    "start_time": event.get("commence_time"),
                                    "event_title": f"{home} vs {away}".strip(" vs "),
                                },
                            )
                        )
        return anchors

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
        limit: int,
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
                if isinstance(payload, list) and limit > 0:
                    payload = payload[:limit]
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
        sport_valid = None
        if is_success_status(status_code):
            sport_valid = True
        elif status_code in {400, 404, 422}:
            sport_valid = False
        if is_success_status(status_code) and not expected_shape_ok:
            body = redact_secret_text(payload, secrets=[self.api_key])
        failure_reason = classify_provider_failure(
            provider=self.provider_name,
            key_source=self.credentials.key_source,
            status_code=status_code,
            body=body,
            expected_shape_ok=expected_shape_ok,
            sport_key_valid=sport_valid,
        )
        count = payload_count(payload)
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
