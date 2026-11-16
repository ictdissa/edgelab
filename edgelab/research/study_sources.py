"""Collection interfaces for registered studies; cached-only is the default."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol

from dotenv import dotenv_values

from edgelab.config.settings import Settings
from edgelab.data.cache import CachedHttpClient, CachedResponse, atomic_json
from edgelab.data.external_sources.odds_api_io_provider import OddsAPIIOProvider
from edgelab.data.polymarket_collector import PolymarketCollector
from edgelab.data.schemas import ExternalAnchor, OrderBook, PredictionMarket, PriceLevel
from edgelab.matching.event_resolver import resolve_events
from edgelab.research.study import EventDiscovery, StudyDesign, StudyRunner, StudySnapshot
from edgelab.data.external_sources.odds_provider import parse_timestamp


class FrozenBookProvider(OddsAPIIOProvider):
    """Never change the registered bookmaker universe to accommodate an API plan."""

    def _validated_bookmakers(self, refresh: bool = False) -> str:
        return self.settings.odds_api_io_bookmakers

    def _odds_json_with_bookmaker_fallback(
        self,
        url: str,
        params: Mapping[str, Any],
        refresh: bool,
    ) -> Any:
        return self._cached_payload(url, params, refresh)


class AuditedClient(CachedHttpClient):
    def __init__(
        self,
        original: CachedHttpClient,
        directory: Path,
        requests: list[dict[str, Any]],
        cache_only: bool,
        secret: str = "",
        network_request_limit: int | None = None,
    ) -> None:
        super().__init__(
            original.cache_dir.parent,
            original.provider,
            original.rate_limiter,
            original.http_get,
            cache_only=cache_only,
        )
        self.directory, self.requests, self.secret = directory, requests, secret
        self.network_request_limit = network_request_limit

    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        refresh: bool = False,
        timeout: float = 20,
    ) -> CachedResponse:
        audit: dict[str, Any] = {
            "provider": self.provider,
            "endpoint": url.split("?")[0],
            "refresh": refresh,
            "status": "error",
        }
        clean = {k: v for k, v in (params or {}).items() if v is not None and str(v).strip()}
        path = self._cache_path(url, clean)
        try:
            if not self.cache_only and (refresh or not path.exists()):
                attempts = sum(
                    r.get("network_attempt") is True and r.get("provider") == self.provider
                    for r in self.requests
                )
                if (
                    self.network_request_limit is not None
                    and attempts >= self.network_request_limit
                ):
                    raise RuntimeError("per-run research request budget exhausted")
                audit["network_attempt"] = True
            response = super().get_json(url, params, refresh, timeout)
            audit.update(
                status="ok",
                cache_hit=response.cache_hit,
                fetched_at=response.fetched_at.isoformat(),
                verified=response.verified,
            )
            return response
        except Exception as exc:
            audit["error_type"] = type(exc).__name__
            audit["http_status"] = getattr(exc, "status_code", None)
            audit["local_rate_limited"] = "local rate limit exceeded" in str(exc)
            # Do not let higher-level collectors log authentication-bearing exception URLs.
            raise RuntimeError(f"{self.provider} request failed: {type(exc).__name__}") from None
        finally:
            if path.exists():
                text = path.read_text()
                if self.secret:
                    text = text.replace(self.secret, "[REDACTED]")
                archive = self.directory / "raw" / f"{len(self.requests):04d}-{self.provider}.json"
                try:
                    envelope = json.loads(text)
                except ValueError:
                    envelope = {"unparseable_cached_response": text}
                atomic_json(archive, envelope)
                audit["consumed_response"] = str(archive.relative_to(self.directory))
            self.requests.append(audit)
            try:
                audit["remaining_local_budget"] = self.rate_limiter.status().remaining
                audit["budget_window_seconds"] = self.rate_limiter.window_seconds
            except RuntimeError:
                audit["remaining_local_budget"] = None
            atomic_json(self.directory / "request_audit.json", self.requests)


class StudyCollectionContext(Protocol):
    """Collection needs a frozen universe, not permission to execute trades."""

    design: StudyDesign
    settings: Settings
    start: datetime
    end: datetime

    def clock(self) -> datetime: ...


class LiveStudySource:
    """Only Odds-API.io football 1X2 is certified for this initial study runner.

    Credentials are read separately from the environment. Provider endpoints,
    universe, risk and costs come from frozen settings, not mutable .env values.
    """

    def __init__(
        self,
        runner: StudyCollectionContext,
        refresh: bool = False,
        odds_request_limit: int | None = None,
        seed_events: list[dict[str, Any]] | None = None,
    ) -> None:
        if runner.design.provider != "odds_api_io":
            raise ValueError("synthetic registration cannot collect prospective live evidence")
        self.runner, self.refresh = runner, refresh
        if odds_request_limit is not None and odds_request_limit < 1:
            raise ValueError("per-run odds request limit must be positive")
        self.odds_request_limit = odds_request_limit
        self.seed_events = seed_events
        private = dotenv_values(".env")
        self.key = os.environ.get("ODDS_API_IO_KEY") or private.get("ODDS_API_IO_KEY") or ""
        if refresh and not self.key:
            raise ValueError("refresh requires ODDS_API_IO_KEY; cache-only needs no credential")

    def discover_events(
        self,
        provider: FrozenBookProvider,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], EventDiscovery, list[dict[str, str]]]:
        """Apply the league/date query before selection, with bounded audited pagination."""
        runner, design = self.runner, self.runner.design
        to_time = runner.end + timedelta(hours=design.event_window_hours)
        seen: dict[str, dict[str, Any]] = {}
        eligible: dict[str, dict[str, Any]] = {}
        rejects: list[dict[str, str]] = []
        names = {s.strip().casefold() for s in [design.league, *design.league_aliases]}
        audit = EventDiscovery(
            status="capped", reason="page_budget_exhausted", league_slug=design.league_slug
        )
        for page in range(design.max_event_pages):
            try:
                rows = (
                    self.seed_events[
                        page * design.event_page_size : (page + 1) * design.event_page_size
                    ]
                    if self.seed_events is not None
                    else provider.list_events(
                        design.sport,
                        design.event_page_size,
                        refresh=self.refresh,
                        league=design.league_slug,
                        skip=page * design.event_page_size,
                        from_time=runner.start,
                        to_time=to_time,
                        status="pending",
                    )
                )
            except (RuntimeError, ValueError) as exc:
                audit.status, audit.reason = "failed", f"provider_error:{type(exc).__name__}"
                break
            audit.pages += 1
            audit.returned_rows += len(rows)
            new_ids = 0
            for event in rows:
                event_id = str(event.get("id") or "")
                if not event_id:
                    rejects.append(
                        {"event_id": "", "source": "events", "reason": "missing_event_id"}
                    )
                    continue
                if event_id in seen:
                    audit.duplicate_rows += 1
                    if any(
                        seen[event_id].get(k) != event.get(k)
                        for k in ("home", "away", "date", "startTime", "league")
                    ):
                        audit.status, audit.reason = (
                            "failed",
                            "conflicting_event_identity_across_pages",
                        )
                        break
                    continue
                seen[event_id] = event
                new_ids += 1
                league = event.get("league", "")
                slug = league.get("slug") if isinstance(league, dict) else None
                name = league.get("name", "") if isinstance(league, dict) else str(league)
                in_league = (
                    slug == design.league_slug if slug else str(name).strip().casefold() in names
                )
                date = parse_timestamp(event.get("date") or event.get("startTime"))
                reason = None
                if not in_league:
                    reason = "outside_registered_league"
                elif date is None:
                    reason = "missing_or_invalid_fixture_time"
                elif not runner.start <= date <= to_time:
                    reason = "outside_registered_fixture_window"
                if reason:
                    rejects.append({"event_id": event_id, "source": "events", "reason": reason})
                else:
                    eligible[event_id] = event
            if audit.status == "failed":
                break
            if len(eligible) > design.max_events:
                audit.reason = "selected_event_cap_reached"
                break
            if rows and not new_ids:
                audit.reason = "repeated_page_no_progress"
                break
            if len(rows) < design.event_page_size:
                audit.status, audit.complete, audit.reason = "complete", True, "end_of_results"
                break
            if len(eligible) == design.max_events:
                audit.reason = "selected_event_cap_may_truncate"
                break
        selected = sorted(
            eligible.values(),
            key=lambda e: (
                parse_timestamp(e.get("date") or e.get("startTime")) or runner.end,
                str(e["id"]),
            ),
        )[: design.max_events]
        audit.unique_events, audit.selected_events = len(seen), len(selected)
        if self.seed_events is not None and audit.status != "failed":
            audit.status = "reused"
            audit.reason = "recorded_fixture_snapshot; " + audit.reason
        return list(seen.values()), selected, audit, rejects

    def collect(
        self,
        previous: StudySnapshot | None,
        started_at: datetime,
        directory: Path,
        refresh_odds: bool,
        entries_open: bool,
    ) -> StudySnapshot:
        runner, design = self.runner, self.runner.design
        requests: list[dict[str, Any]] = []
        errors: list[str] = []
        settings = runner.settings.model_copy(deep=True)
        settings.live_data_dir = directory
        settings.odds_api_io_key = self.key or "CACHE_ONLY_NO_CREDENTIAL"
        provider = FrozenBookProvider(settings)
        provider.client = AuditedClient(
            provider.client,
            directory,
            requests,
            not self.refresh,
            self.key,
            network_request_limit=self.odds_request_limit,
        )
        collector = PolymarketCollector(settings, mock=False)
        collector.client = AuditedClient(collector.client, directory, requests, not self.refresh)
        anchors = previous.anchors if previous else []
        events = previous.odds_events if previous else []
        rejections: list[dict[str, str]] = []
        discovery = (
            previous.event_discovery.model_copy(update={"status": "reused"})
            if previous and previous.event_discovery
            else None
        )
        if refresh_odds:
            try:
                events, eligible_events, discovery, event_rejections = self.discover_events(
                    provider
                )
                rejections.extend(event_rejections)
                if discovery.status == "failed":
                    raise RuntimeError("event discovery failed; no new odds requested")
                payloads = (
                    provider.fetch_odds_payloads(
                        [str(e["id"]) for e in eligible_events if e.get("id") is not None],
                        refresh=self.refresh,
                    )
                    if eligible_events
                    else []
                )
                by_id = {str(e.get("id")): e for e in events}
                payloads = [
                    {**by_id.get(str(p.get("id") or p.get("eventId")), {}), **p} for p in payloads
                ]
                anchors = provider.normalize_to_external_odds(payloads)
            except (ValueError, RuntimeError) as exc:
                if discovery is None:
                    discovery = EventDiscovery(
                        status="failed",
                        reason=f"provider_error:{type(exc).__name__}",
                        league_slug=design.league_slug,
                    )
                errors.append(
                    f"odds_collection_failed:{type(exc).__name__}; prior vintage retained"
                )
        selected = []
        leagues = {v.strip().casefold() for v in [design.league, *design.league_aliases]}
        books = {v.casefold() for v in design.bookmakers}
        for a in anchors:
            reason = None
            slug = a.metadata.get("league_slug")
            if (
                slug != design.league_slug
                if slug
                else str(a.metadata.get("league", "")).strip().casefold() not in leagues
            ):
                reason = "outside_registered_league"
            elif a.source.casefold() not in books:
                reason = "outside_registered_bookmakers"
            elif a.category != "sports_1x2":
                reason = "not_fixture_1x2"
            if reason:
                rejections.append({"event_id": a.event_id, "source": a.source, "reason": reason})
            else:
                selected.append(a)
        markets = {m.market_id: m for m in previous.markets} if previous else {}
        if entries_open and (refresh_odds or not markets):
            discovered = collector.fetch_fixture_markets(
                selected, design.max_markets, refresh=self.refresh
            )
            markets.update({m.market_id: m for m in discovered})
        now = runner.clock()
        matches = resolve_events(list(markets.values()), selected, design.min_match_score, now)
        matched = {m.market_id for m in matches if m.decision == "MATCH"}
        # Include prior matched contracts for exact-token follow-ups even if odds disappear.
        previously_booked = {b.market_id for b in previous.books} if previous else set()
        book_markets = [m for m in markets.values() if m.market_id in matched | previously_booked]
        books_out = collector.fetch_orderbooks(
            book_markets, design.max_markets, refresh=self.refresh
        )
        queries_path = directory / "sports_discovery_queries.json"
        queries = json.loads(queries_path.read_text()) if queries_path.exists() else []
        if self.key:
            events = json.loads(json.dumps(events).replace(self.key, "[REDACTED]"))
        return StudySnapshot(
            started_at=started_at,
            as_of=runner.clock(),
            markets=list(markets.values()),
            anchors=selected,
            books=books_out,
            odds_events=events,
            scope_rejections=rejections,
            requests=requests,
            errors=errors,
            discovery_queries=queries,
            event_discovery=discovery,
        )


class SyntheticStudySource:
    """Deterministic lifecycle fixture. Manufactured prices are NOT empirical alpha."""

    def __init__(self, runner: StudyRunner) -> None:
        if runner.design.provider != "synthetic":
            raise ValueError("mock source requires a synthetic registration")
        self.runner = runner

    def collect(
        self,
        previous: StudySnapshot | None,
        started_at: datetime,
        directory: Path,
        refresh_odds: bool,
        entries_open: bool,
    ) -> StudySnapshot:
        now, design = started_at, self.runner.design
        kickoff = self.runner.end + timedelta(hours=3)
        market = PredictionMarket(
            market_id="synthetic-ars-che",
            event_id="synthetic-polymarket",
            category="sports_1x2",
            title="Arsenal vs Chelsea match winner",
            outcomes=["Arsenal", "Draw", "Chelsea"],
            token_ids=["synthetic-ars", "synthetic-draw", "synthetic-che"],
            start_time=kickoff,
            end_time=kickoff + timedelta(hours=2),
            resolution_source="SYNTHETIC",
            raw_metadata={"league": design.league, "collected_at": now.isoformat()},
        )
        anchors = [
            ExternalAnchor(
                source_id=f"synthetic:{outcome}",
                event_id="synthetic-odds",
                source=design.bookmakers[0],
                category="sports_1x2",
                outcome=outcome,
                decimal_odds=odds,
                timestamp=now,
                available_at=now,
                metadata={
                    "league": design.league,
                    "home": "Arsenal",
                    "away": "Chelsea",
                    "start_time": kickoff.isoformat(),
                    "provider": "synthetic",
                },
            )
            for outcome, odds in [("Arsenal", 2.0), ("Draw", 3.5), ("Chelsea", 4.0)]
        ]
        age = (now - self.runner.start).total_seconds()
        prices = [(0.29, 0.31) if age < 300 else (0.32, 0.34), (0.27, 0.29), (0.23, 0.25)]
        books = [
            OrderBook(
                market_id=market.market_id,
                token_id=token,
                timestamp=now,
                available_at=now,
                bids=[PriceLevel(price=bid, size=100)],
                asks=[PriceLevel(price=ask, size=100)],
            )
            for token, (bid, ask) in zip(market.token_ids, prices, strict=True)
        ]
        return StudySnapshot(
            started_at=now,
            as_of=now,
            markets=[market],
            anchors=anchors,
            books=books,
            odds_events=[{"id": "synthetic-odds", "home": "Arsenal", "away": "Chelsea"}],
            event_discovery=EventDiscovery(
                status="synthetic",
                complete=True,
                reason="constructed_fixture_not_live_coverage",
                unique_events=1,
                selected_events=1,
                returned_rows=1,
            ),
        )
