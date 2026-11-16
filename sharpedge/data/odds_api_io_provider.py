"""Cached Odds-API.io provider for live research collection."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sharpedge.config.settings import Settings
from sharpedge.data.odds_collector import OddsAPIIOProvider as _Normalizer
from sharpedge.data.rate_limiter import RollingWindowRateLimiter
from sharpedge.data.schemas import ExternalOdds


HttpGet = Callable[..., Any]


@dataclass(frozen=True)
class OddsAPIIOBundle:
    events: list[dict[str, Any]]
    odds_payloads: list[dict[str, Any]]
    normalized_odds: list[ExternalOdds]
    requests_made: int
    cache_hits: int
    remaining_budget: int


class OddsAPIIOProvider:
    """Odds-API.io adapter with persistent cache and a hard 90/hour local cap."""

    def __init__(
        self,
        settings: Settings,
        cache_dir: Path | None = None,
        rate_limiter: RollingWindowRateLimiter | None = None,
        http_get: HttpGet | None = None,
        write_outputs: bool = True,
    ) -> None:
        self.settings = settings
        self.base_url = settings.odds_api_io_base.rstrip("/")
        self.cache_dir = cache_dir or settings.odds_api_io_cache_dir
        max_requests = min(settings.odds_api_io_max_requests_per_hour, 90)
        self.rate_limiter = rate_limiter or RollingWindowRateLimiter(
            settings.odds_api_io_rate_limit_state,
            max_requests=max_requests,
        )
        self.http_get = http_get
        self.write_outputs = write_outputs
        self.requests_made = 0
        self.cache_hits = 0
        self._normalizer = _Normalizer(api_key=None)

    @property
    def remaining_budget(self) -> int:
        return self.rate_limiter.status().remaining

    def fetch_snapshot(
        self,
        sport: str,
        bookmakers: str,
        max_events: int,
        league: str | None = None,
        use_cache: bool = False,
        refresh: bool = False,
        status: str = "pending,live",
    ) -> OddsAPIIOBundle:
        events = self.fetch_events(
            sport=sport,
            max_events=max_events,
            league=league,
            status=status,
            use_cache=use_cache,
            refresh=refresh,
        )
        event_ids = [str(event.get("id")) for event in events if event.get("id") is not None]
        odds_payloads = self.fetch_odds_for_events(
            event_ids=event_ids,
            bookmakers=bookmakers,
            use_cache=use_cache,
            refresh=refresh,
        )
        normalized = self.normalize_odds_payloads(odds_payloads)
        if self.write_outputs:
            write_json(Path("data/raw/odds_api_io_events.json"), events)
            write_json(Path("data/raw/odds_api_io_odds.json"), odds_payloads)
            write_external_odds_csv(Path("data/raw/odds_api_io_normalized.csv"), normalized)
        return OddsAPIIOBundle(
            events=events,
            odds_payloads=odds_payloads,
            normalized_odds=normalized,
            requests_made=self.requests_made,
            cache_hits=self.cache_hits,
            remaining_budget=self.remaining_budget,
        )

    def fetch_events(
        self,
        sport: str,
        max_events: int,
        league: str | None = None,
        status: str = "pending,live",
        use_cache: bool = False,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        params = {
            "sport": sport,
            "league": league,
            "status": status,
            "limit": str(max_events),
        }
        payload = self._request_json(
            "events",
            "/events",
            params,
            use_cache=use_cache,
            refresh=refresh,
        )
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)][:max_events]

    def fetch_odds_for_events(
        self,
        event_ids: Sequence[str],
        bookmakers: str,
        use_cache: bool = False,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        ids = [str(event_id) for event_id in event_ids if str(event_id).strip()]
        for chunk in _chunks(ids, 10):
            if len(chunk) == 1:
                payload = self._request_json(
                    "odds",
                    "/odds",
                    {"eventId": chunk[0], "bookmakers": bookmakers},
                    use_cache=use_cache,
                    refresh=refresh,
                )
            else:
                payload = self._request_json(
                    "odds_multi",
                    "/odds/multi",
                    {"eventIds": ",".join(chunk), "bookmakers": bookmakers},
                    use_cache=use_cache,
                    refresh=refresh,
                )
            if isinstance(payload, list):
                output.extend(item for item in payload if isinstance(item, dict))
            elif isinstance(payload, dict):
                output.append(payload)
        return output

    def normalize_odds_payloads(self, payloads: Sequence[Mapping[str, Any]]) -> list[ExternalOdds]:
        return self._normalizer.normalize_odds_payloads(payloads)

    def _request_json(
        self,
        namespace: str,
        path: str,
        params: Mapping[str, Any],
        use_cache: bool,
        refresh: bool,
    ) -> Any:
        cache_path = self._cache_path(namespace, path, params)
        if cache_path.exists() and (use_cache or not refresh):
            self.cache_hits += 1
            return json.loads(cache_path.read_text(encoding="utf-8"))
        if use_cache:
            raise FileNotFoundError(f"Cache miss for Odds-API.io request: {cache_path}")
        if not self.settings.odds_api_io_key:
            raise ValueError("ODDS_API_IO_KEY is required for Odds-API.io refresh")

        decision = self.rate_limiter.reserve()
        if not decision.allowed:
            raise RuntimeError(
                "Odds-API.io local rate limit exhausted: "
                f"remaining={decision.remaining}, retry_after={decision.retry_after_seconds:.0f}s"
            )
        clean_params = {
            key: value
            for key, value in params.items()
            if value is not None and str(value).strip()
        }
        clean_params["apiKey"] = self.settings.odds_api_io_key
        response = self._http_get(f"{self.base_url}{path}", params=clean_params, timeout=20)
        status_code = int(getattr(response, "status_code", 200))
        if status_code >= 400:
            text = str(getattr(response, "text", ""))[:500]
            raise RuntimeError(f"Odds-API.io {path} failed with HTTP {status_code}: {text}")
        payload = response.json()
        self.requests_made += 1
        write_json(cache_path, payload)
        return payload

    def _http_get(self, url: str, **kwargs: Any) -> Any:
        if self.http_get is not None:
            return self.http_get(url, **kwargs)
        import httpx

        return httpx.get(url, **kwargs)

    def _cache_path(self, namespace: str, path: str, params: Mapping[str, Any]) -> Path:
        cache_params = {
            key: str(value)
            for key, value in sorted(params.items())
            if key.lower() != "apikey" and value is not None
        }
        digest = hashlib.sha256(
            json.dumps({"path": path, "params": cache_params}, sort_keys=True).encode()
        ).hexdigest()[:20]
        return self.cache_dir / namespace / f"{digest}.json"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_external_odds_csv(path: Path, rows: Sequence[ExternalOdds]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "event_id",
        "sport",
        "book",
        "market_type",
        "outcome",
        "decimal_odds",
        "timestamp",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "event_id": row.event_id,
                    "sport": row.sport,
                    "book": row.book,
                    "market_type": row.market_type,
                    "outcome": row.outcome,
                    "decimal_odds": f"{row.decimal_odds:.8f}",
                    "timestamp": row.timestamp.isoformat(),
                }
            )


def read_external_odds_csv(path: Path) -> list[ExternalOdds]:
    if not path.exists():
        return []
    rows: list[ExternalOdds] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append(ExternalOdds(**row))
    return rows


def _chunks(values: Sequence[str], size: int) -> list[list[str]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]
