"""Match a typed Polymarket event title to cached Odds-API.io events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from sharpedge.data.event_resolver import (
    extract_entities_from_title,
    infer_sport,
    name_similarity,
    parse_datetime,
)
from sharpedge.data.market_type import classify_market_type


@dataclass(frozen=True)
class OddsEventCandidate:
    odds_event_id: str
    query: str
    odds_title: str
    odds_home: str
    odds_away: str
    league: str
    sport: str
    start_time: datetime | None
    score: float
    decision: str
    reasons: list[str]
    rejection_reason: str | None
    raw_event: Mapping[str, Any]


def match_polymarket_text_to_odds_events(
    polymarket_event: str,
    odds_events: Sequence[Mapping[str, Any]],
    min_match_score: float = 0.82,
    review_band: float = 0.10,
) -> list[OddsEventCandidate]:
    """Score cached Odds-API.io events against a typed Polymarket event title."""

    pm_market_type = classify_market_type(polymarket_event, "Soccer")
    pm_entities = extract_entities_from_title(polymarket_event)
    pm_sport = _infer_query_sport(polymarket_event)
    candidates = [
        _score_event(
            polymarket_event=polymarket_event,
            pm_market_type=pm_market_type,
            pm_entities=pm_entities,
            pm_sport=pm_sport,
            odds_event=event,
            min_match_score=min_match_score,
            review_band=review_band,
        )
        for event in odds_events
    ]
    return sorted(candidates, key=lambda item: item.score, reverse=True)


def _score_event(
    polymarket_event: str,
    pm_market_type: str,
    pm_entities: list[str],
    pm_sport: str,
    odds_event: Mapping[str, Any],
    min_match_score: float,
    review_band: float,
) -> OddsEventCandidate:
    home = _event_team(odds_event, "home")
    away = _event_team(odds_event, "away")
    odds_title = f"{home} vs {away}" if home and away else _string(odds_event.get("title"))
    league = _league(odds_event)
    sport = infer_sport(f"{_sport(odds_event)} {league}")
    start_time = parse_datetime(
        odds_event.get("date")
        or odds_event.get("startTime")
        or odds_event.get("commence_time")
    )
    title_score = name_similarity(polymarket_event, odds_title)
    sport_score = _sport_score(pm_sport, sport)
    market_score = 1.0 if pm_market_type in {"1x2", "moneyline", "unknown"} else 0.0
    if len(pm_entities) >= 2 and home and away:
        forward = (name_similarity(pm_entities[0], home) + name_similarity(pm_entities[1], away)) / 2
        backward = (name_similarity(pm_entities[0], away) + name_similarity(pm_entities[1], home)) / 2
        team_score = max(forward, backward)
        entity_reason = f"typed_entities={' vs '.join(pm_entities[:2])}"
    else:
        team_score = min(name_similarity(polymarket_event, home), name_similarity(polymarket_event, away))
        entity_reason = "typed_event_does_not_contain_two_fixture_teams"

    score = 0.58 * team_score + 0.22 * title_score + 0.10 * sport_score + 0.10 * market_score
    rejection_reason = None
    if pm_market_type in {"outright", "top_4"}:
        rejection_reason = "typed_polymarket_event_is_futures_or_outright_not_fixture"
        score = min(score, 0.45)
    elif len(pm_entities) < 2:
        rejection_reason = "typed_polymarket_event_missing_two_teams"
        score = min(score, 0.60)

    if rejection_reason:
        decision = "REJECT"
    elif score >= min_match_score:
        decision = "MATCH"
    elif score >= min_match_score - review_band:
        decision = "REVIEW"
    else:
        decision = "REJECT"
        rejection_reason = "below_match_threshold"

    return OddsEventCandidate(
        odds_event_id=str(odds_event.get("id") or ""),
        query=polymarket_event,
        odds_title=odds_title,
        odds_home=home,
        odds_away=away,
        league=league,
        sport=sport,
        start_time=start_time,
        score=max(0.0, min(1.0, score)),
        decision=decision,
        reasons=[
            f"team_score={team_score:.3f}",
            f"title_score={title_score:.3f}",
            f"sport_score={sport_score:.3f}",
            f"market_type={pm_market_type}",
            entity_reason,
        ],
        rejection_reason=rejection_reason,
        raw_event=odds_event,
    )


def _event_team(event: Mapping[str, Any], side: str) -> str:
    return _string(event.get(side) or event.get(f"{side}Team") or event.get(f"{side}_team"))


def _league(event: Mapping[str, Any]) -> str:
    value = event.get("league")
    if isinstance(value, Mapping):
        return _string(value.get("name") or value.get("slug"))
    return _string(value)


def _sport(event: Mapping[str, Any]) -> str:
    value = event.get("sport")
    if isinstance(value, Mapping):
        return _string(value.get("name") or value.get("slug"))
    return _string(value)


def _sport_score(left: str, right: str) -> float:
    left_norm = infer_sport(left)
    right_norm = infer_sport(right)
    if "unknown" in {left_norm.lower(), right_norm.lower()}:
        return 0.70
    return 1.0 if left_norm.lower() == right_norm.lower() else 0.0


def _infer_query_sport(value: str) -> str:
    inferred = infer_sport(value)
    if inferred.lower() in {"mma", "soccer", "unknown"}:
        return inferred
    return "unknown"


def _string(value: Any) -> str:
    return "" if value is None else str(value).strip()
