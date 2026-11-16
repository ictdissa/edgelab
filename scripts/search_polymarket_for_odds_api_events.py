#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import _bootstrap  # noqa: F401

from sharpedge.config.settings import Settings
from sharpedge.data.event_resolver import (
    extract_entities_from_title,
    name_similarity,
    parse_datetime,
)


@dataclass(frozen=True)
class SearchResult:
    odds_event_id: str
    query: str
    odds_home: str
    odds_away: str
    odds_league: str
    odds_date: str
    best_polymarket_event_id: str
    best_polymarket_slug: str
    best_polymarket_title: str
    best_polymarket_closed: bool | None
    best_polymarket_active: bool | None
    best_polymarket_start: str
    confidence: float
    entity_score: float
    title_score: float
    time_score: float
    markets_count: int
    open_markets_count: int
    accepting_orders_count: int
    decision: str
    reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search Polymarket for the exact previously saved Odds-API.io events"
    )
    parser.add_argument(
        "--odds-events",
        type=Path,
        default=Path("data/raw/odds_api_io_events.json"),
        help="Previously saved Odds-API.io events JSON. This script does not call Odds-API.io.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/polymarket_search_for_odds_api_events.csv"),
    )
    parser.add_argument("--limit-per-type", type=int, default=25)
    parser.add_argument("--min-confidence", type=float, default=0.82)
    parser.add_argument("--sleep-seconds", type=float, default=0.05)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    events = _load_odds_events(args.odds_events)
    settings = Settings()
    results: list[SearchResult] = []
    for event in events:
        result = _search_one_event(
            gamma_base=settings.polymarket_gamma_base,
            event=event,
            limit_per_type=args.limit_per_type,
            min_confidence=args.min_confidence,
        )
        results.append(result)
        time.sleep(args.sleep_seconds)
    _write_results(args.output, results)
    matched_active = sum(1 for row in results if row.decision == "matched_active")
    matched_closed = sum(1 for row in results if row.decision == "matched_closed")
    no_match = sum(1 for row in results if row.decision == "no_match")
    print(
        "Searched Polymarket for saved Odds-API.io events: "
        f"events={len(results)}, matched_active={matched_active}, "
        f"matched_closed={matched_closed}, no_match={no_match}"
    )
    print(f"Wrote {args.output}")
    for row in sorted(results, key=lambda item: item.confidence, reverse=True)[:8]:
        print(
            f"{row.confidence:.3f} {row.decision}: {row.query} -> "
            f"{row.best_polymarket_title or 'NO CANDIDATE'}"
        )


def _load_odds_events(path: Path) -> list[Mapping[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a JSON list")
    return [event for event in payload if isinstance(event, Mapping)]


def _search_one_event(
    gamma_base: str,
    event: Mapping[str, Any],
    limit_per_type: int,
    min_confidence: float,
) -> SearchResult:
    home = _string(event.get("home") or event.get("homeTeam") or event.get("home_team"))
    away = _string(event.get("away") or event.get("awayTeam") or event.get("away_team"))
    query = f"{home} vs {away}".strip()
    if query == "vs":
        query = _string(event.get("title") or event.get("name") or event.get("id"))
    odds_date = _string(event.get("date") or event.get("startTime") or event.get("commence_time"))
    odds_start = parse_datetime(odds_date)
    odds_league = _league(event)

    payload = _polymarket_public_search(gamma_base, query, limit_per_type)
    candidates = payload.get("events", []) if isinstance(payload, Mapping) else []
    scored = [
        _score_candidate(event, candidate, query, home, away, odds_start)
        for candidate in candidates
        if isinstance(candidate, Mapping)
    ]
    scored.sort(key=lambda item: item.confidence, reverse=True)
    if not scored:
        return SearchResult(
            odds_event_id=_string(event.get("id")),
            query=query,
            odds_home=home,
            odds_away=away,
            odds_league=odds_league,
            odds_date=odds_date,
            best_polymarket_event_id="",
            best_polymarket_slug="",
            best_polymarket_title="",
            best_polymarket_closed=None,
            best_polymarket_active=None,
            best_polymarket_start="",
            confidence=0.0,
            entity_score=0.0,
            title_score=0.0,
            time_score=0.0,
            markets_count=0,
            open_markets_count=0,
            accepting_orders_count=0,
            decision="no_match",
            reason="public-search returned no event candidates",
        )

    best = scored[0]
    if best.confidence < min_confidence:
        decision = "no_match"
        reason = "best candidate below confidence threshold"
    elif best.best_polymarket_closed:
        decision = "matched_closed"
        reason = "high-confidence candidate exists but Polymarket marks event closed"
    elif best.open_markets_count == 0:
        decision = "matched_closed"
        reason = "high-confidence candidate exists but no open markets were found"
    else:
        decision = "matched_active"
        reason = "high-confidence active candidate found"
    return SearchResult(
        odds_event_id=_string(event.get("id")),
        query=query,
        odds_home=home,
        odds_away=away,
        odds_league=odds_league,
        odds_date=odds_date,
        best_polymarket_event_id=best.best_polymarket_event_id,
        best_polymarket_slug=best.best_polymarket_slug,
        best_polymarket_title=best.best_polymarket_title,
        best_polymarket_closed=best.best_polymarket_closed,
        best_polymarket_active=best.best_polymarket_active,
        best_polymarket_start=best.best_polymarket_start,
        confidence=best.confidence,
        entity_score=best.entity_score,
        title_score=best.title_score,
        time_score=best.time_score,
        markets_count=best.markets_count,
        open_markets_count=best.open_markets_count,
        accepting_orders_count=best.accepting_orders_count,
        decision=decision,
        reason=reason,
    )


def _polymarket_public_search(
    gamma_base: str,
    query: str,
    limit_per_type: int,
) -> Mapping[str, Any]:
    import httpx

    response = httpx.get(
        f"{gamma_base.rstrip('/')}/public-search",
        params={
            "q": query,
            "limit_per_type": limit_per_type,
            "keep_closed_markets": 1,
            "search_profiles": "false",
            "optimized": "true",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, Mapping) else {}


def _score_candidate(
    odds_event: Mapping[str, Any],
    polymarket_event: Mapping[str, Any],
    query: str,
    home: str,
    away: str,
    odds_start: datetime | None,
) -> SearchResult:
    title = _string(polymarket_event.get("title") or polymarket_event.get("slug"))
    entities = extract_entities_from_title(title)
    entity_score = _entity_pair_score(home, away, entities, title)
    title_score = name_similarity(query, title)
    pm_start = _polymarket_start(polymarket_event)
    time_score = _time_score(odds_start, pm_start)
    confidence = 0.58 * entity_score + 0.24 * title_score + 0.18 * time_score
    markets = [
        market
        for market in polymarket_event.get("markets", []) or []
        if isinstance(market, Mapping)
    ]
    open_markets = [
        market
        for market in markets
        if not bool(market.get("closed")) and bool(market.get("active", True))
    ]
    accepting_orders = [
        market for market in open_markets if bool(market.get("acceptingOrders"))
    ]
    return SearchResult(
        odds_event_id=_string(odds_event.get("id")),
        query=query,
        odds_home=home,
        odds_away=away,
        odds_league=_league(odds_event),
        odds_date=_string(odds_event.get("date")),
        best_polymarket_event_id=_string(polymarket_event.get("id")),
        best_polymarket_slug=_string(polymarket_event.get("slug")),
        best_polymarket_title=title,
        best_polymarket_closed=bool(polymarket_event.get("closed")),
        best_polymarket_active=bool(polymarket_event.get("active")),
        best_polymarket_start=pm_start.isoformat() if pm_start else "",
        confidence=max(0.0, min(1.0, confidence)),
        entity_score=entity_score,
        title_score=title_score,
        time_score=time_score,
        markets_count=len(markets),
        open_markets_count=len(open_markets),
        accepting_orders_count=len(accepting_orders),
        decision="candidate",
        reason="candidate",
    )


def _entity_pair_score(home: str, away: str, entities: list[str], title: str) -> float:
    if len(entities) >= 2:
        forward = (name_similarity(home, entities[0]) + name_similarity(away, entities[1])) / 2
        backward = (name_similarity(home, entities[1]) + name_similarity(away, entities[0])) / 2
        return max(forward, backward)
    return (name_similarity(home, title) + name_similarity(away, title)) / 2


def _time_score(left: datetime | None, right: datetime | None) -> float:
    if not left or not right:
        return 0.50
    seconds = abs((left - right).total_seconds())
    if seconds <= 2 * 3600:
        return 1.0
    if seconds <= 12 * 3600:
        return 0.80
    if seconds <= 36 * 3600:
        return 0.55
    if seconds <= 7 * 24 * 3600:
        return 0.20
    return 0.0


def _polymarket_start(event: Mapping[str, Any]) -> datetime | None:
    markets = [market for market in event.get("markets", []) or [] if isinstance(market, Mapping)]
    for market in markets:
        for key in ("gameStartTime", "eventStartTime", "endDate", "startDate"):
            parsed = parse_datetime(market.get(key))
            if parsed:
                return parsed
    for key in ("eventStartTime", "endDate", "startDate"):
        parsed = parse_datetime(event.get(key))
        if parsed:
            return parsed
    return None


def _league(event: Mapping[str, Any]) -> str:
    value = event.get("league")
    if isinstance(value, Mapping):
        return _string(value.get("name") or value.get("slug"))
    return _string(value)


def _string(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _write_results(path: Path, rows: list[SearchResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(SearchResult.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            payload = row.__dict__.copy()
            payload["confidence"] = f"{row.confidence:.4f}"
            payload["entity_score"] = f"{row.entity_score:.4f}"
            payload["title_score"] = f"{row.title_score:.4f}"
            payload["time_score"] = f"{row.time_score:.4f}"
            writer.writerow(payload)


if __name__ == "__main__":
    main()
