"""External odds provider abstractions and odds conversion helpers."""

from __future__ import annotations

import csv
import json
import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from sharpedge.data.rate_limiter import RollingWindowRateLimiter
from sharpedge.data.schemas import ExternalOdds

LOGGER = logging.getLogger(__name__)
SAMPLES_DIR = Path(__file__).resolve().parent / "samples"


def american_to_decimal(odds: int | float) -> float:
    """Convert American odds to decimal odds."""

    if odds == 0:
        raise ValueError("American odds cannot be zero")
    if odds > 0:
        return 1.0 + float(odds) / 100.0
    return 1.0 + 100.0 / abs(float(odds))


def fractional_to_decimal(odds: str) -> float:
    """Convert fractional odds like '5/2' to decimal odds."""

    try:
        fraction = Fraction(odds)
    except ValueError as exc:
        raise ValueError(f"Invalid fractional odds: {odds}") from exc
    return 1.0 + float(fraction)


def validate_decimal_odds(decimal_odds: float) -> float:
    if decimal_odds <= 1.0:
        raise ValueError(f"Decimal odds must be > 1.0, got {decimal_odds}")
    return float(decimal_odds)


def parse_odds_value(value: Any, odds_format: str = "decimal") -> float:
    """Parse common odds formats into decimal odds."""

    normalized = odds_format.lower()
    if normalized == "decimal":
        return validate_decimal_odds(float(value))
    if normalized == "american":
        return validate_decimal_odds(american_to_decimal(float(value)))
    if normalized == "fractional":
        return validate_decimal_odds(fractional_to_decimal(str(value)))
    raise ValueError(f"Unsupported odds format: {odds_format}")


class BaseOddsProvider(ABC):
    """Interface for external odds providers."""

    @abstractmethod
    def fetch_odds(
        self,
        sport: str | None = None,
        market_type: str | None = None,
        as_of: datetime | None = None,
    ) -> list[ExternalOdds]:
        """Fetch odds rows from the provider."""


class MockOddsProvider(BaseOddsProvider):
    """Offline odds provider backed by sample JSON or CSV."""

    def __init__(self, sample_path: Path | None = None) -> None:
        self.sample_path = sample_path or SAMPLES_DIR / "external_odds.json"

    def fetch_odds(
        self,
        sport: str | None = None,
        market_type: str | None = None,
        as_of: datetime | None = None,
    ) -> list[ExternalOdds]:
        rows = self._load_rows()
        odds: list[ExternalOdds] = []
        cutoff = as_of.astimezone(UTC) if as_of else None
        for row in rows:
            try:
                parsed = ExternalOdds(**row)
            except Exception as exc:
                LOGGER.warning("Skipping invalid mock odds row: %s", exc)
                continue
            if sport and parsed.sport.lower() != sport.lower():
                continue
            if market_type and parsed.market_type.lower() != market_type.lower():
                continue
            if cutoff and parsed.timestamp > cutoff:
                continue
            odds.append(parsed)
        return odds

    def _load_rows(self) -> list[dict[str, Any]]:
        if self.sample_path.suffix.lower() == ".csv":
            with self.sample_path.open(newline="", encoding="utf-8") as handle:
                return list(csv.DictReader(handle))
        with self.sample_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            payload = payload.get("odds", [])
        if not isinstance(payload, list):
            raise ValueError("Mock odds sample must be a list or contain an odds list")
        return payload


class OddsAPIProvider(BaseOddsProvider):
    """Legacy The Odds API adapter.

    Responses are converted to the predecessor ExternalOdds schema. New collection
    should use EdgeLab's provider-specific adapters and receipt-time checks.
    """

    def __init__(self, api_key: str | None, base_url: str | None) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "https://api.the-odds-api.com/v4").rstrip("/")

    def fetch_odds(
        self,
        sport: str | None = None,
        market_type: str | None = None,
        as_of: datetime | None = None,
    ) -> list[ExternalOdds]:
        if not self.api_key:
            raise ValueError("ODDS_API_KEY is required for OddsAPIProvider")
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx is required for OddsAPIProvider") from exc

        sport_key = sport or "upcoming"
        markets = market_type or "h2h"
        url = f"{self.base_url}/sports/{sport_key}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": "us,uk,eu",
            "markets": markets,
            "oddsFormat": "decimal",
        }
        response = httpx.get(url, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        timestamp = datetime.now(UTC)
        rows: list[ExternalOdds] = []
        for event in payload if isinstance(payload, list) else []:
            event_id = str(event.get("id") or event.get("commence_time") or "")
            sport_title = str(event.get("sport_key") or sport_key)
            for bookmaker in event.get("bookmakers", []) or []:
                book = str(bookmaker.get("key") or bookmaker.get("title") or "unknown")
                for market in bookmaker.get("markets", []) or []:
                    mtype = str(market.get("key") or markets)
                    for outcome in market.get("outcomes", []) or []:
                        try:
                            rows.append(
                                ExternalOdds(
                                    event_id=event_id,
                                    sport=sport_title,
                                    book=book,
                                    market_type=mtype,
                                    outcome=str(outcome.get("name")),
                                    decimal_odds=validate_decimal_odds(
                                        float(outcome.get("price"))
                                    ),
                                    timestamp=timestamp,
                                )
                            )
                        except Exception as exc:
                            LOGGER.warning("Skipping malformed Odds API row: %s", exc)
        cutoff = as_of.astimezone(UTC) if as_of else None
        if cutoff:
            rows = [row for row in rows if row.timestamp <= cutoff]
        return rows


class OddsAPIIOProvider(BaseOddsProvider):
    """Odds-API.io REST adapter with local fail-closed rate limiting.

    The vendor returns event IDs that do not automatically match Polymarket event IDs.
    This provider is for collecting and normalizing sharp external odds; event matching
    remains an explicit research step.
    """

    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://api.odds-api.io/v3",
        bookmakers: str | Sequence[str] = "Pinnacle,Bet365,Betfair,Unibet",
        rate_limiter: RollingWindowRateLimiter | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.bookmakers = (
            ",".join(bookmakers)
            if isinstance(bookmakers, Sequence) and not isinstance(bookmakers, str)
            else str(bookmakers)
        )
        self.rate_limiter = rate_limiter

    def fetch_events(
        self,
        sport: str = "football",
        league: str | None = None,
        status: str | None = "pending,live",
        limit: int | None = 20,
        bookmaker: str | None = None,
        live: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch Odds-API.io events defensively as raw dictionaries."""

        path = "/events/live" if live else "/events"
        params: dict[str, Any] = {"sport": sport}
        if not live:
            params.update(
                {
                    "league": league,
                    "status": status,
                    "limit": limit,
                    "bookmaker": bookmaker,
                }
            )
        payload = self._request(path, params)
        if not isinstance(payload, list):
            LOGGER.warning("Odds-API.io events response was not a list")
            return []
        return [event for event in payload if isinstance(event, dict)]

    def fetch_odds_payloads(
        self,
        event_ids: Sequence[str | int],
        bookmakers: str | Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch event odds, batching up to 10 event IDs per request with /odds/multi."""

        ids = [str(event_id) for event_id in event_ids if str(event_id).strip()]
        if not ids:
            return []
        book_param = self._bookmaker_param(bookmakers)
        payloads: list[dict[str, Any]] = []
        for chunk in _chunks(ids, 10):
            if len(chunk) == 1:
                payload = self._request(
                    "/odds",
                    {"eventId": chunk[0], "bookmakers": book_param},
                )
                if isinstance(payload, dict):
                    payloads.append(payload)
                continue
            payload = self._request(
                "/odds/multi",
                {"eventIds": ",".join(chunk), "bookmakers": book_param},
            )
            if isinstance(payload, list):
                payloads.extend(item for item in payload if isinstance(item, dict))
            elif isinstance(payload, dict):
                payloads.append(payload)
        return payloads

    def fetch_snapshot(
        self,
        sport: str = "football",
        league: str | None = None,
        status: str | None = "pending,live",
        limit: int | None = 20,
        bookmakers: str | Sequence[str] | None = None,
        live: bool = False,
        bookmaker_filter: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[ExternalOdds]]:
        """Fetch events plus odds and return raw payloads with normalized rows."""

        events = self.fetch_events(
            sport=sport,
            league=league,
            status=status,
            limit=limit,
            bookmaker=bookmaker_filter,
            live=live,
        )
        if limit is not None:
            events = events[:limit]
        event_ids = [event.get("id") for event in events if event.get("id") is not None]
        odds_payloads = self.fetch_odds_payloads(event_ids, bookmakers=bookmakers)
        return events, odds_payloads, self.normalize_odds_payloads(odds_payloads)

    def fetch_odds(
        self,
        sport: str | None = None,
        market_type: str | None = None,
        as_of: datetime | None = None,
    ) -> list[ExternalOdds]:
        events, odds_payloads, rows = self.fetch_snapshot(sport=sport or "football")
        _ = events, odds_payloads
        if market_type:
            rows = [row for row in rows if row.market_type.lower() == market_type.lower()]
        cutoff = as_of.astimezone(UTC) if as_of else None
        if cutoff:
            rows = [row for row in rows if row.timestamp <= cutoff]
        return rows

    def normalize_odds_payloads(
        self,
        payloads: Sequence[Mapping[str, Any]],
        fallback_timestamp: datetime | None = None,
    ) -> list[ExternalOdds]:
        """Convert Odds-API.io event odds payloads to the project schema."""

        timestamp = fallback_timestamp or datetime.now(UTC)
        rows: list[ExternalOdds] = []
        for event in payloads:
            rows.extend(_normalize_odds_api_io_event(event, timestamp))
        return rows

    def _request(self, path: str, params: Mapping[str, Any]) -> Any:
        if not self.api_key:
            raise ValueError("ODDS_API_IO_KEY is required for OddsAPIIOProvider")
        if self.rate_limiter:
            decision = self.rate_limiter.reserve()
            if not decision.allowed:
                minutes = decision.retry_after_seconds / 60.0
                raise RuntimeError(
                    "Odds-API.io local rate limit would be exceeded: "
                    f"remaining={decision.remaining}, retry_after={minutes:.1f}m"
                )
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx is required for OddsAPIIOProvider") from exc

        clean_params = {
            key: value
            for key, value in params.items()
            if value is not None and str(value).strip() != ""
        }
        clean_params["apiKey"] = self.api_key
        try:
            response = httpx.get(
                f"{self.base_url}{path}",
                params=clean_params,
                timeout=20,
            )
        except httpx.RequestError as exc:
            raise RuntimeError(f"Odds-API.io request failed for {path}") from exc
        if response.status_code >= 400:
            raise RuntimeError(
                f"Odds-API.io {path} failed with HTTP {response.status_code}: "
                f"{_safe_error_text(response.text)}"
            )
        return response.json()

    def _bookmaker_param(self, bookmakers: str | Sequence[str] | None) -> str:
        if bookmakers is None:
            return self.bookmakers
        if isinstance(bookmakers, str):
            return bookmakers
        return ",".join(str(bookmaker) for bookmaker in bookmakers)


def _normalize_odds_api_io_event(
    event: Mapping[str, Any],
    fallback_timestamp: datetime,
) -> list[ExternalOdds]:
    event_id = str(event.get("id") or event.get("eventId") or event.get("eventid") or "")
    if not event_id:
        return []
    sport = _event_sport(event)
    home = _string_or_none(event.get("home") or event.get("homeTeam"))
    away = _string_or_none(event.get("away") or event.get("awayTeam"))
    bookmakers = event.get("bookmakers")
    rows: list[ExternalOdds] = []
    for book, markets in _iter_bookmakers(bookmakers):
        for market in markets:
            market_name = str(market.get("name") or market.get("market") or market.get("key") or "")
            odds_entries = market.get("odds") or market.get("prices") or []
            if isinstance(odds_entries, Mapping):
                odds_entries = [odds_entries]
            if not isinstance(odds_entries, list):
                continue
            market_type = _canonical_market_type(
                market_name,
                sport=sport,
                odds_entries=odds_entries,
            )
            updated_at = _parse_timestamp(market.get("updatedAt"), fallback_timestamp)
            for odds_entry in odds_entries:
                if not isinstance(odds_entry, Mapping):
                    continue
                line = odds_entry.get("hdp") or odds_entry.get("line") or odds_entry.get("marketLine")
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
                    try:
                        rows.append(
                            ExternalOdds(
                                event_id=event_id,
                                sport=sport,
                                book=book,
                                market_type=market_type,
                                outcome=outcome,
                                decimal_odds=decimal_odds,
                                timestamp=updated_at,
                            )
                        )
                    except Exception as exc:
                        LOGGER.warning("Skipping malformed Odds-API.io row: %s", exc)
    return rows


def _iter_bookmakers(bookmakers: Any) -> list[tuple[str, list[Mapping[str, Any]]]]:
    if isinstance(bookmakers, Mapping):
        parsed: list[tuple[str, list[Mapping[str, Any]]]] = []
        for book, markets in bookmakers.items():
            if isinstance(markets, list):
                parsed.append(
                    (
                        str(book),
                        [market for market in markets if isinstance(market, Mapping)],
                    )
                )
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
                parsed.append(
                    (
                        book,
                        [market for market in markets if isinstance(market, Mapping)],
                    )
                )
        return parsed
    return []


def _event_sport(event: Mapping[str, Any]) -> str:
    sport = event.get("sport")
    if isinstance(sport, Mapping):
        slug = str(sport.get("slug") or sport.get("name") or "")
    else:
        slug = str(sport or "")
    normalized = slug.lower().strip()
    if normalized in {"football", "soccer"}:
        return "Soccer"
    if normalized in {"mixed-martial-arts", "mma", "ufc"}:
        return "MMA"
    return slug or "unknown"


def _canonical_market_type(
    market_name: str,
    sport: str,
    odds_entries: Sequence[Any],
) -> str:
    normalized = market_name.lower().replace("_", " ").replace("-", " ").strip()
    if normalized in {"ml", "moneyline", "match winner", "match result"}:
        has_draw = any(isinstance(entry, Mapping) and "draw" in entry for entry in odds_entries)
        return "1x2" if sport.lower() == "soccer" or has_draw else "moneyline"
    if "correct score" in normalized or "exact score" in normalized:
        return "exact_score"
    if "total" in normalized:
        return "totals"
    if "go distance" in normalized or "goes distance" in normalized:
        return "goes_distance"
    if "method" in normalized:
        return "method_of_victory"
    return normalized.replace(" ", "_") or "unknown"


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


def _parse_timestamp(value: Any, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        numeric = float(value) / 1000.0 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(numeric, UTC)
    text = str(value).strip()
    if not text:
        return fallback
    try:
        if text.isdigit():
            numeric = float(text) / 1000.0 if len(text) > 10 else float(text)
            return datetime.fromtimestamp(numeric, UTC)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return fallback


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _chunks(values: Sequence[str], size: int) -> list[list[str]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


def _safe_error_text(text: str, limit: int = 300) -> str:
    return text.replace("\n", " ").strip()[:limit]
