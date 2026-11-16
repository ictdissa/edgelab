"""Resolve external odds events to Polymarket sports markets."""

from __future__ import annotations

import csv
import json
import re
import statistics
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Mapping, Sequence

from sharpedge.data.market_type import classify_market_type, market_types_compatible
from sharpedge.data.schemas import ExternalOdds, Market
from sharpedge.research.fair_value_model import infer_market_type

STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "by",
    "for",
    "in",
    "of",
    "on",
    "the",
    "to",
    "will",
    "win",
    "winner",
    "winning",
    "market",
    "match",
    "game",
    "fight",
    "bout",
    "event",
}

ENTITY_SUFFIXES = {
    "fc",
    "cf",
    "sc",
    "afc",
    "club",
    "team",
    "men",
    "women",
}

GENERIC_OUTCOMES = {
    "draw",
    "yes",
    "no",
    "over",
    "under",
    "other",
    "other score",
}

ALIASES = {
    "man utd": "manchester united",
    "man united": "manchester united",
    "man city": "manchester city",
    "tottenham": "tottenham hotspur",
    "spurs": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "west ham": "west ham united",
    "newcastle": "newcastle united",
    "brighton": "brighton and hove albion",
    "psg": "paris saint germain",
    "inter": "internazionale",
    "inter milan": "internazionale",
    "ac milan": "milan",
    "bayern": "bayern munich",
    "usa": "united states",
    "u s a": "united states",
    "usmnt": "united states",
    "turkiye": "turkey",
    "cote divoire": "ivory coast",
    "cote d ivoire": "ivory coast",
}

MARKET_DESCRIPTOR_RE = re.compile(
    r"\b("
    r"moneyline|match winner|1x2|exact score|correct score|method of victory|"
    r"goes the distance|go distance|fight goes distance|winner|title winner|"
    r"league winner|top 4|top four|finish top four|total goals?|totals?|"
    r"outright|to win"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProviderEvent:
    provider_event_id: str
    title: str
    sport: str
    league: str | None = None
    start_time: datetime | None = None
    entities: tuple[str, ...] = ()
    outcomes: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EventMatch:
    market_id: str
    polymarket_event_id: str
    polymarket_title: str
    polymarket_market_type: str
    provider_event_id: str | None
    provider_title: str
    confidence: float
    second_confidence: float
    gap: float
    decision: str
    reason: str
    entity_score: float
    title_score: float
    time_score: float
    sport_score: float
    league_score: float
    polymarket_entities: tuple[str, ...]
    provider_entities: tuple[str, ...]


@dataclass(frozen=True)
class PolymarketCandidate:
    odds_event_id: str
    event_id: str
    market_id: str
    title: str
    sport: str
    league: str | None
    start_time: datetime | None
    outcomes: tuple[str, ...]
    token_ids: tuple[str, ...]
    status: str
    closed: bool
    active: bool
    accepting_orders: bool
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def market_type(self) -> str:
        return classify_market_type(self.title, self.sport, self.outcomes)


@dataclass(frozen=True)
class ResolvedEventMatch:
    odds_event_id: str
    polymarket_event_id: str | None
    polymarket_market_id: str | None
    odds_home: str
    odds_away: str
    polymarket_title: str
    league: str
    sport: str
    start_time_odds: datetime | None
    start_time_polymarket: datetime | None
    match_score: float
    match_reasons: list[str]
    market_type: str
    outcome_mapping: dict[str, str]
    decision: str
    rejection_reason: str | None = None


@dataclass(frozen=True)
class EventResolverResult:
    matched: list[ResolvedEventMatch]
    review: list[ResolvedEventMatch]
    rejected: list[ResolvedEventMatch]

    @property
    def all(self) -> list[ResolvedEventMatch]:
        return [*self.matched, *self.review, *self.rejected]


class EventResolver:
    """Match fixture odds events to Polymarket match markets with audit reasons."""

    def __init__(self, min_match_score: float = 0.82, review_band: float = 0.10) -> None:
        self.min_match_score = min_match_score
        self.review_band = review_band

    def match_odds_events_to_polymarket(
        self,
        odds_events: Sequence[Mapping[str, Any]],
        polymarket_candidate_markets: Sequence[PolymarketCandidate | Mapping[str, Any]],
    ) -> EventResolverResult:
        candidates_by_odds_id: dict[str, list[PolymarketCandidate]] = defaultdict(list)
        for candidate in polymarket_candidate_markets:
            parsed = (
                candidate
                if isinstance(candidate, PolymarketCandidate)
                else polymarket_candidate_from_mapping(candidate)
            )
            candidates_by_odds_id[parsed.odds_event_id].append(parsed)

        matched: list[ResolvedEventMatch] = []
        review: list[ResolvedEventMatch] = []
        rejected: list[ResolvedEventMatch] = []

        for event in odds_events:
            odds_event_id = str(event.get("id") or event.get("eventId") or "")
            candidates = candidates_by_odds_id.get(odds_event_id, [])
            resolution = self._resolve_one(event, candidates)
            if resolution.decision == "MATCH":
                matched.append(resolution)
            elif resolution.decision == "REVIEW":
                review.append(resolution)
            else:
                rejected.append(resolution)
        return EventResolverResult(matched=matched, review=review, rejected=rejected)

    def _resolve_one(
        self,
        odds_event: Mapping[str, Any],
        candidates: Sequence[PolymarketCandidate],
    ) -> ResolvedEventMatch:
        odds_event_id = str(odds_event.get("id") or odds_event.get("eventId") or "")
        home = _event_team(odds_event, "home")
        away = _event_team(odds_event, "away")
        league = _league(odds_event) or ""
        sport = infer_sport(
            " ".join(
                part
                for part in [
                    _nested_str(odds_event, "sport", "sportName", "sport_name") or "",
                    league,
                    home,
                    away,
                ]
                if part
            )
        )
        odds_start = _event_start_time(odds_event)
        if not candidates:
            return _resolved_reject(
                odds_event_id=odds_event_id,
                home=home,
                away=away,
                league=league,
                sport=sport,
                odds_start=odds_start,
                reason="no Polymarket candidates returned for targeted queries",
            )

        scored = [self._score_candidate(odds_event, candidate) for candidate in candidates]
        scored.sort(key=lambda item: item.match_score, reverse=True)
        best = scored[0]
        second_score = scored[1].match_score if len(scored) > 1 else 0.0
        if best.rejection_reason:
            return best
        if best.match_score >= self.min_match_score and best.match_score - second_score >= 0.03:
            return _replace_decision(best, "MATCH", None)
        if best.match_score >= self.min_match_score - self.review_band:
            return _replace_decision(
                best,
                "REVIEW",
                "ambiguous or below high-confidence threshold",
            )
        return _replace_decision(best, "REJECT", "best candidate below match threshold")

    def _score_candidate(
        self,
        odds_event: Mapping[str, Any],
        candidate: PolymarketCandidate,
    ) -> ResolvedEventMatch:
        odds_event_id = str(odds_event.get("id") or odds_event.get("eventId") or "")
        home = _event_team(odds_event, "home")
        away = _event_team(odds_event, "away")
        league = _league(odds_event) or ""
        sport = infer_sport(f"{_nested_str(odds_event, 'sport') or ''} {league}")
        odds_start = _event_start_time(odds_event)
        team_score, team_reasons = _fixture_team_score(home, away, candidate.title, candidate.outcomes)
        league_score = (
            name_similarity(league, candidate.league)
            if league and candidate.league
            else 0.65
        )
        time_score = _time_score(odds_start, candidate.start_time)
        sport_score = _sport_score(sport, candidate.sport)
        market_type = candidate.market_type
        market_score = 1.0 if market_types_compatible("1x2", market_type, odds_is_fixture=True) else 0.0
        score = (
            0.45 * team_score
            + 0.15 * league_score
            + 0.15 * time_score
            + 0.10 * sport_score
            + 0.15 * market_score
        )
        reasons = [
            f"team_score={team_score:.3f}",
            f"league_score={league_score:.3f}",
            f"time_score={time_score:.3f}",
            f"sport_score={sport_score:.3f}",
            f"market_type={market_type}",
            *team_reasons,
        ]
        rejection = _hard_rejection_reason(
            home=home,
            away=away,
            candidate=candidate,
            market_score=market_score,
            team_score=team_score,
        )
        mapping = _outcome_mapping(home, away, candidate.outcomes)
        return ResolvedEventMatch(
            odds_event_id=odds_event_id,
            polymarket_event_id=candidate.event_id,
            polymarket_market_id=candidate.market_id,
            odds_home=home,
            odds_away=away,
            polymarket_title=candidate.title,
            league=league or candidate.league or "",
            sport=sport,
            start_time_odds=odds_start,
            start_time_polymarket=candidate.start_time,
            match_score=max(0.0, min(1.0, score)),
            match_reasons=reasons,
            market_type=market_type,
            outcome_mapping=mapping,
            decision="REJECT" if rejection else "CANDIDATE",
            rejection_reason=rejection,
        )


def polymarket_candidate_from_mapping(payload: Mapping[str, Any]) -> PolymarketCandidate:
    return PolymarketCandidate(
        odds_event_id=str(payload.get("odds_event_id") or payload.get("oddsEventId") or ""),
        event_id=str(payload.get("event_id") or payload.get("eventId") or payload.get("id") or ""),
        market_id=str(
            payload.get("market_id")
            or payload.get("conditionId")
            or payload.get("condition_id")
            or payload.get("marketId")
            or ""
        ),
        title=str(payload.get("title") or payload.get("question") or ""),
        sport=infer_sport(str(payload.get("sport") or payload.get("category") or "")),
        league=str(payload.get("league") or "") or None,
        start_time=parse_datetime(
            payload.get("start_time")
            or payload.get("gameStartTime")
            or payload.get("eventStartTime")
            or payload.get("endDate")
            or payload.get("startDate")
        ),
        outcomes=tuple(_safe_string_list(payload.get("outcomes"))),
        token_ids=tuple(
            _safe_string_list(
                payload.get("token_ids")
                or payload.get("clobTokenIds")
                or payload.get("clob_token_ids")
            )
        ),
        status=str(payload.get("status") or ("closed" if payload.get("closed") else "active")),
        closed=bool(payload.get("closed")),
        active=bool(payload.get("active", True)),
        accepting_orders=bool(payload.get("acceptingOrders", payload.get("accepting_orders", True))),
        raw=payload,
    )


def candidates_from_public_search_payload(
    odds_event: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> list[PolymarketCandidate]:
    odds_event_id = str(odds_event.get("id") or "")
    output: list[PolymarketCandidate] = []
    for event in payload.get("events", []) or []:
        if not isinstance(event, Mapping):
            continue
        event_id = str(event.get("id") or "")
        event_title = str(event.get("title") or "")
        event_league = _league(event)
        event_sport = infer_sport(f"{event_title} {event_league or ''}")
        for market in event.get("markets", []) or []:
            if not isinstance(market, Mapping):
                continue
            row = dict(market)
            row["odds_event_id"] = odds_event_id
            row["event_id"] = event_id
            row["title"] = str(market.get("question") or event_title)
            row["league"] = event_league or ""
            row["sport"] = event_sport
            output.append(polymarket_candidate_from_mapping(row))
    return output


def write_event_match_audit(path: Path, result: EventResolverResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "odds_event",
        "polymarket_title",
        "score",
        "decision",
        "reasons",
        "rejection_reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in result.all:
            writer.writerow(
                {
                    "odds_event": f"{row.odds_home} vs {row.odds_away}",
                    "polymarket_title": row.polymarket_title,
                    "score": f"{row.match_score:.4f}",
                    "decision": row.decision,
                    "reasons": "; ".join(row.match_reasons),
                    "rejection_reason": row.rejection_reason or "",
                }
            )


def _resolved_reject(
    odds_event_id: str,
    home: str,
    away: str,
    league: str,
    sport: str,
    odds_start: datetime | None,
    reason: str,
) -> ResolvedEventMatch:
    return ResolvedEventMatch(
        odds_event_id=odds_event_id,
        polymarket_event_id=None,
        polymarket_market_id=None,
        odds_home=home,
        odds_away=away,
        polymarket_title="",
        league=league,
        sport=sport,
        start_time_odds=odds_start,
        start_time_polymarket=None,
        match_score=0.0,
        match_reasons=[],
        market_type="unknown",
        outcome_mapping={},
        decision="REJECT",
        rejection_reason=reason,
    )


def _replace_decision(
    match: ResolvedEventMatch,
    decision: str,
    rejection_reason: str | None,
) -> ResolvedEventMatch:
    return ResolvedEventMatch(
        odds_event_id=match.odds_event_id,
        polymarket_event_id=match.polymarket_event_id,
        polymarket_market_id=match.polymarket_market_id,
        odds_home=match.odds_home,
        odds_away=match.odds_away,
        polymarket_title=match.polymarket_title,
        league=match.league,
        sport=match.sport,
        start_time_odds=match.start_time_odds,
        start_time_polymarket=match.start_time_polymarket,
        match_score=match.match_score,
        match_reasons=match.match_reasons,
        market_type=match.market_type,
        outcome_mapping=match.outcome_mapping,
        decision=decision,
        rejection_reason=rejection_reason,
    )


def _fixture_team_score(
    home: str,
    away: str,
    title: str,
    outcomes: Sequence[str],
) -> tuple[float, list[str]]:
    entities = extract_entities_from_title(title)
    if len(entities) < 2:
        entities = _entity_like_outcomes(outcomes)
    if len(entities) >= 2:
        forward = (name_similarity(home, entities[0]) + name_similarity(away, entities[1])) / 2
        backward = (name_similarity(home, entities[1]) + name_similarity(away, entities[0])) / 2
        score = max(forward, backward)
        return score, [f"candidate_entities={' vs '.join(entities[:2])}"]
    home_in_title = name_similarity(home, title)
    away_in_title = name_similarity(away, title)
    score = min(home_in_title, away_in_title)
    return score, ["candidate_has_fewer_than_two_detected_teams"]


def _hard_rejection_reason(
    home: str,
    away: str,
    candidate: PolymarketCandidate,
    market_score: float,
    team_score: float,
) -> str | None:
    if candidate.closed or not candidate.active:
        return "past_or_resolved_polymarket_market"
    if candidate.market_type in {"outright", "top_4"}:
        return "fixture_odds_cannot_match_futures_or_outright_market"
    if market_score <= 0.0:
        return f"market_type_mismatch:{candidate.market_type}"
    entities = extract_entities_from_title(candidate.title)
    if len(entities) < 2 and team_score < 0.80:
        return "fixture_requires_two_team_names_but_candidate_does_not_have_both"
    if max(name_similarity(home, candidate.title), name_similarity(away, candidate.title)) < 0.35:
        return "candidate_title_does_not_contain_fixture_teams"
    return None


def _outcome_mapping(home: str, away: str, outcomes: Sequence[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    normalized_outcomes = {normalize_text(outcome): outcome for outcome in outcomes}
    if {"yes", "no"} <= set(normalized_outcomes):
        return mapping
    for outcome in outcomes:
        normalized = normalize_text(outcome)
        if normalized == "draw":
            mapping["Draw"] = outcome
        if name_similarity(home, outcome) >= 0.72:
            mapping[home] = outcome
        if name_similarity(away, outcome) >= 0.72:
            mapping[away] = outcome
    return mapping


def _event_team(event: Mapping[str, Any], side: str) -> str:
    keys = (side, f"{side}Team", f"{side}_team")
    return _nested_str(event, *keys) or ""


def _safe_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


def load_polymarket_markets_csv(path: Path) -> list[Market]:
    """Load the CSV emitted by list_current_polymarket_sports.py."""

    grouped: dict[str, list[dict[str, str]]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            grouped.setdefault(row["market_id"], []).append(row)

    markets: list[Market] = []
    for rows in grouped.values():
        first = rows[0]
        markets.append(
            Market(
                market_id=first["market_id"],
                event_id=first["event_id"],
                sport=first.get("sport") or infer_sport(first.get("title", "")),
                league=first.get("league") or None,
                title=first["title"],
                description=None,
                outcomes=[row["outcome"] for row in rows],
                token_ids=[row.get("token_id", "") for row in rows],
                start_time=parse_datetime(first.get("start_time")),
                end_time=None,
                resolution_source=None,
                status=first.get("status") or "unknown",
                raw_metadata={"source": str(path)},
            )
        )
    return markets


def load_external_odds_csv(path: Path) -> list[ExternalOdds]:
    """Load normalized external odds while ignoring resolver metadata columns."""

    fields = set(ExternalOdds.model_fields)
    rows: list[ExternalOdds] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            payload = {key: row[key] for key in fields if key in row}
            rows.append(ExternalOdds(**payload))
    return rows


def load_provider_events_json(
    events_path: Path | None,
    odds_payloads_path: Path | None,
    external_odds: Sequence[ExternalOdds],
) -> list[ProviderEvent]:
    """Build provider event records from raw event and odds payload JSON."""

    raw_events: list[Mapping[str, Any]] = []
    for path in (events_path, odds_payloads_path):
        if path and path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                raw_events.extend(item for item in payload if isinstance(item, Mapping))
            elif isinstance(payload, Mapping):
                raw_events.append(payload)

    outcomes_by_event: dict[str, set[str]] = {}
    odds_by_event: dict[str, list[ExternalOdds]] = {}
    for row in external_odds:
        outcomes_by_event.setdefault(row.event_id, set()).add(row.outcome)
        odds_by_event.setdefault(row.event_id, []).append(row)

    provider_by_id: dict[str, ProviderEvent] = {}
    for raw in raw_events:
        event_id = _event_id(raw)
        if not event_id:
            continue
        outcomes = tuple(sorted(outcomes_by_event.get(event_id, set())))
        parsed = provider_event_from_payload(raw, outcomes=outcomes)
        provider_by_id[event_id] = parsed

    for event_id, rows in odds_by_event.items():
        if event_id in provider_by_id:
            continue
        sport = _mode([row.sport for row in rows]) or "unknown"
        outcomes = tuple(sorted(outcomes_by_event.get(event_id, set())))
        provider_by_id[event_id] = ProviderEvent(
            provider_event_id=event_id,
            title=" / ".join(outcomes[:6]) or event_id,
            sport=sport,
            league=None,
            start_time=None,
            entities=tuple(_entity_like_outcomes(outcomes)),
            outcomes=outcomes,
            raw={},
        )
    return list(provider_by_id.values())


def provider_event_from_payload(
    payload: Mapping[str, Any],
    outcomes: Sequence[str] = (),
) -> ProviderEvent:
    event_id = _event_id(payload)
    home = _nested_str(payload, "home", "homeTeam", "home_team")
    away = _nested_str(payload, "away", "awayTeam", "away_team")
    participants = _participants(payload)
    entities = [home, away] if home and away else participants
    title = (
        _nested_str(payload, "title", "name", "eventName", "event_name")
        or _join_non_empty([home, away], " vs ")
        or event_id
    )
    league = _league(payload)
    sport = infer_sport(
        " ".join(
            str(part)
            for part in (
                _nested_str(payload, "sport", "sportName", "sport_name"),
                league,
                title,
            )
            if part
        )
    )
    if not entities:
        entities = extract_entities_from_title(title)
    outcome_entities = _entity_like_outcomes(outcomes)
    all_entities = _dedupe_entities([*entities, *outcome_entities])
    return ProviderEvent(
        provider_event_id=event_id,
        title=title,
        sport=sport,
        league=league,
        start_time=_event_start_time(payload),
        entities=tuple(all_entities),
        outcomes=tuple(outcomes),
        raw=payload,
    )


def resolve_market_event_matches(
    markets: Sequence[Market],
    provider_events: Sequence[ProviderEvent],
    external_odds: Sequence[ExternalOdds],
    min_confidence: float = 0.72,
    min_gap: float = 0.04,
) -> list[EventMatch]:
    """Return the best provider-event candidate for each Polymarket market."""

    outcomes_by_event: dict[str, tuple[str, ...]] = {}
    for event in provider_events:
        event_outcomes = set(event.outcomes)
        for row in external_odds:
            if row.event_id == event.provider_event_id:
                event_outcomes.add(row.outcome)
        outcomes_by_event[event.provider_event_id] = tuple(sorted(event_outcomes))

    matches: list[EventMatch] = []
    for market in markets:
        scored = [
            _score_candidate(
                market,
                event,
                provider_outcomes=outcomes_by_event.get(event.provider_event_id, ()),
            )
            for event in provider_events
        ]
        scored.sort(key=lambda item: item.confidence, reverse=True)
        if not scored:
            matches.append(_unmatched(market, "no provider events supplied"))
            continue
        best = scored[0]
        second = scored[1].confidence if len(scored) > 1 else 0.0
        gap = best.confidence - second
        if best.confidence >= min_confidence and gap >= min_gap:
            decision = "matched"
            reason = "accepted by confidence and candidate gap"
        elif best.confidence >= min_confidence:
            decision = "review"
            reason = "good score but ambiguous versus second candidate"
        elif best.confidence >= min_confidence - 0.10 and gap >= min_gap:
            decision = "review"
            reason = "near threshold; manual review recommended"
        else:
            decision = "rejected"
            reason = "below confidence threshold"
        matches.append(
            EventMatch(
                market_id=best.market_id,
                polymarket_event_id=best.polymarket_event_id,
                polymarket_title=best.polymarket_title,
                polymarket_market_type=best.polymarket_market_type,
                provider_event_id=best.provider_event_id,
                provider_title=best.provider_title,
                confidence=best.confidence,
                second_confidence=second,
                gap=gap,
                decision=decision,
                reason=reason,
                entity_score=best.entity_score,
                title_score=best.title_score,
                time_score=best.time_score,
                sport_score=best.sport_score,
                league_score=best.league_score,
                polymarket_entities=best.polymarket_entities,
                provider_entities=best.provider_entities,
            )
        )
    return matches


def rewrite_odds_for_polymarket(
    markets: Sequence[Market],
    external_odds: Sequence[ExternalOdds],
    matches: Sequence[EventMatch],
    include_review: bool = False,
) -> list[ExternalOdds]:
    """Legacy ID rewrite, not a fair-value certification.

    Outrights are unsupported: this schema cannot attest a complete field. Binary
    markets require actual Yes/No source outcomes; never manufacture complements.
    New research must use EdgeLab's matching and settlement-reviewed pricing.
    """

    markets_by_id = {market.market_id: market for market in markets}
    odds_by_provider_event: dict[str, list[ExternalOdds]] = {}
    for row in external_odds:
        odds_by_provider_event.setdefault(row.event_id, []).append(row)

    output: list[ExternalOdds] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for match in matches:
        if match.decision == "rejected" or (match.decision == "review" and not include_review):
            continue
        if not match.provider_event_id:
            continue
        market = markets_by_id.get(match.market_id)
        if not market:
            continue
        pm_market_type = infer_market_type(market)
        if pm_market_type == "outright":
            continue
        for row in odds_by_provider_event.get(match.provider_event_id, []):
            if not _market_type_compatible(pm_market_type, row.market_type, market):
                continue
            mapped_outcomes = _map_external_outcome_to_polymarket(market, row)
            for outcome, decimal_odds, note in mapped_outcomes:
                _ = note
                key = (
                    market.event_id,
                    row.book,
                    pm_market_type,
                    outcome,
                    row.timestamp.isoformat(),
                    f"{decimal_odds:.10f}",
                )
                if key in seen:
                    continue
                seen.add(key)
                output.append(
                    ExternalOdds(
                        event_id=market.event_id,
                        sport=infer_sport(f"{market.sport} {market.title}"),
                        book=row.book,
                        market_type=pm_market_type,
                        outcome=outcome,
                        decimal_odds=decimal_odds,
                        timestamp=row.timestamp,
                    )
                )
    return output


def write_matches_csv(path: Path, matches: Sequence[EventMatch]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "decision",
        "confidence",
        "second_confidence",
        "gap",
        "reason",
        "polymarket_event_id",
        "market_id",
        "polymarket_market_type",
        "provider_event_id",
        "polymarket_title",
        "provider_title",
        "entity_score",
        "title_score",
        "time_score",
        "sport_score",
        "league_score",
        "polymarket_entities",
        "provider_entities",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for match in matches:
            writer.writerow(
                {
                    "decision": match.decision,
                    "confidence": f"{match.confidence:.4f}",
                    "second_confidence": f"{match.second_confidence:.4f}",
                    "gap": f"{match.gap:.4f}",
                    "reason": match.reason,
                    "polymarket_event_id": match.polymarket_event_id,
                    "market_id": match.market_id,
                    "polymarket_market_type": match.polymarket_market_type,
                    "provider_event_id": match.provider_event_id or "",
                    "polymarket_title": match.polymarket_title,
                    "provider_title": match.provider_title,
                    "entity_score": f"{match.entity_score:.4f}",
                    "title_score": f"{match.title_score:.4f}",
                    "time_score": f"{match.time_score:.4f}",
                    "sport_score": f"{match.sport_score:.4f}",
                    "league_score": f"{match.league_score:.4f}",
                    "polymarket_entities": "|".join(match.polymarket_entities),
                    "provider_entities": "|".join(match.provider_entities),
                }
            )


def write_external_odds_csv(path: Path, odds: Sequence[ExternalOdds]) -> None:
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
        for row in odds:
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


def extract_entities_from_title(title: str) -> list[str]:
    cleaned = _strip_market_descriptors(title)
    if ":" in cleaned:
        cleaned = cleaned.split(":", 1)[1]
    matchup = re.split(r"\s+(?:vs\.?|v\.?|at|@)\s+", cleaned, maxsplit=1, flags=re.I)
    if len(matchup) == 2:
        return [_clean_entity(part) for part in matchup if _clean_entity(part)]

    will_win = re.search(
        r"\bwill\s+(.+?)\s+win\s+(?:the\s+)?(.+?)(?:\?|$)",
        cleaned,
        flags=re.I,
    )
    if will_win:
        return [_clean_entity(will_win.group(1))]
    return []


def infer_sport(text: str) -> str:
    normalized = normalize_text(text)
    if any(
        token in normalized
        for token in (
            "ufc",
            "mma",
            "mixed martial arts",
            "fighter",
            "fight night",
        )
    ):
        return "MMA"
    if any(
        token in normalized
        for token in (
            "soccer",
            "football",
            "premier league",
            "champions league",
            "world cup",
            "la liga",
            "serie a",
            "bundesliga",
            "mls",
        )
    ):
        return "Soccer"
    if normalized in {"sports", "sport", "unknown", ""}:
        return "unknown"
    return text.strip() or "unknown"


def normalize_text(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    )
    lowered = ascii_value.lower()
    lowered = lowered.replace("&", " and ")
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return ALIASES.get(lowered, lowered)


def name_similarity(left: str, right: str) -> float:
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    left_norm = ALIASES.get(left_norm, left_norm)
    right_norm = ALIASES.get(right_norm, right_norm)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    left_tokens = _tokens(left_norm, keep_entity_suffixes=False)
    right_tokens = _tokens(right_norm, keep_entity_suffixes=False)
    if not left_tokens or not right_tokens:
        return SequenceMatcher(None, left_norm, right_norm).ratio()
    overlap = len(left_tokens & right_tokens)
    precision = overlap / len(left_tokens)
    recall = overlap / len(right_tokens)
    token_f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    sequence = SequenceMatcher(None, left_norm, right_norm).ratio()
    containment = 1.0 if left_tokens <= right_tokens or right_tokens <= left_tokens else 0.0
    return max(token_f1, 0.65 * token_f1 + 0.35 * sequence, 0.86 * containment)


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric, UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_datetime(float(text))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _score_candidate(
    market: Market,
    provider: ProviderEvent,
    provider_outcomes: Sequence[str],
) -> EventMatch:
    pm_sport = infer_sport(f"{market.sport} {market.title} {market.league or ''}")
    provider_sport = infer_sport(f"{provider.sport} {provider.title} {provider.league or ''}")
    sport_score = _sport_score(pm_sport, provider_sport)
    pm_entities = tuple(extract_entities_from_title(market.title))
    if not pm_entities:
        pm_entities = tuple(_entity_like_outcomes(market.outcomes))
    provider_entities = tuple(
        _dedupe_entities([*provider.entities, *_entity_like_outcomes(provider_outcomes)])
    )
    entity_score = _entity_score(pm_entities, provider_entities)
    title_score = name_similarity(_strip_market_descriptors(market.title), provider.title)
    time_score = _time_score(market.start_time, provider.start_time)
    league_score = _league_score(market.league, provider.league, market.title, provider.title)
    confidence = (
        0.42 * entity_score
        + 0.23 * title_score
        + 0.15 * time_score
        + 0.12 * sport_score
        + 0.08 * league_score
    )
    if _looks_like_outright_yes_no(market) and _looks_like_fixture_provider(
        provider, provider_outcomes
    ):
        confidence = min(confidence, 0.60)
    if sport_score == 0.0:
        confidence = min(confidence, 0.58)
    if entity_score < 0.35 and title_score < 0.55:
        confidence = min(confidence, 0.55)
    return EventMatch(
        market_id=market.market_id,
        polymarket_event_id=market.event_id,
        polymarket_title=market.title,
        polymarket_market_type=infer_market_type(market),
        provider_event_id=provider.provider_event_id,
        provider_title=provider.title,
        confidence=max(0.0, min(1.0, confidence)),
        second_confidence=0.0,
        gap=0.0,
        decision="candidate",
        reason="candidate",
        entity_score=entity_score,
        title_score=title_score,
        time_score=time_score,
        sport_score=sport_score,
        league_score=league_score,
        polymarket_entities=pm_entities,
        provider_entities=provider_entities,
    )


def _unmatched(market: Market, reason: str) -> EventMatch:
    return EventMatch(
        market_id=market.market_id,
        polymarket_event_id=market.event_id,
        polymarket_title=market.title,
        polymarket_market_type=infer_market_type(market),
        provider_event_id=None,
        provider_title="",
        confidence=0.0,
        second_confidence=0.0,
        gap=0.0,
        decision="rejected",
        reason=reason,
        entity_score=0.0,
        title_score=0.0,
        time_score=0.0,
        sport_score=0.0,
        league_score=0.0,
        polymarket_entities=tuple(extract_entities_from_title(market.title)),
        provider_entities=(),
    )


def _map_external_outcome_to_polymarket(
    market: Market,
    row: ExternalOdds,
) -> list[tuple[str, float, str]]:
    outcomes = market.outcomes
    direct = _best_outcome(row.outcome, outcomes)
    if direct and direct[1] >= 0.78:
        return [(direct[0], row.decimal_odds, "direct")]

    if _is_binary_yes_no_market(outcomes):
        market_type = infer_market_type(market)
        if market_type == "goes_distance":
            direct_yes_no = _best_outcome(row.outcome, outcomes)
            if direct_yes_no and direct_yes_no[1] >= 0.72:
                return [(direct_yes_no[0], row.decimal_odds, "binary")]

    return []


def _market_type_compatible(
    polymarket_market_type: str,
    external_market_type: str,
    market: Market,
) -> bool:
    pm_type = _canonical_market_type(polymarket_market_type)
    external_type = _canonical_market_type(external_market_type)
    if pm_type == external_type:
        return True
    if pm_type == "1x2":
        return external_type in {"h2h", "moneyline", "match_winner"}
    if pm_type == "moneyline":
        if _looks_like_outright_yes_no(market):
            return external_type in {
                "moneyline",
                "winner",
                "outright",
                "futures",
                "league_winner",
            }
        return external_type in {"h2h", "moneyline", "match_winner"}
    if pm_type == "league_winner":
        return external_type in {"winner", "outright", "futures", "moneyline"}
    if pm_type == "outright":
        return external_type in {"winner", "outright", "futures", "moneyline"}
    if pm_type == "top_4":
        return external_type in {"top_4", "top_four", "finish_top_four"}
    if pm_type == "goes_distance":
        return external_type in {"goes_distance", "distance"}
    return False


def _canonical_market_type(value: str) -> str:
    normalized = normalize_text(value).replace(" ", "_")
    aliases = {
        "ml": "moneyline",
        "match_result": "1x2",
        "h2h": "h2h",
        "head_to_head": "h2h",
        "correct_score": "exact_score",
        "total": "totals",
        "total_goals": "totals",
        "top_four": "top_4",
        "finish_top_four": "top_4",
        "title_winner": "league_winner",
    }
    return aliases.get(normalized, normalized)


def _looks_like_outright_yes_no(market: Market) -> bool:
    if not _is_binary_yes_no_market(market.outcomes):
        return False
    normalized = normalize_text(market.title)
    return bool(
        re.search(r"\bwill .+ win\b", normalized)
        or "title" in normalized
        or "world cup" in normalized
        or "league winner" in normalized
    )


def _looks_like_fixture_provider(
    provider: ProviderEvent,
    provider_outcomes: Sequence[str],
) -> bool:
    entity_outcomes = _entity_like_outcomes(provider_outcomes)
    has_draw = any(normalize_text(outcome) == "draw" for outcome in provider_outcomes)
    has_matchup_title = bool(re.search(r"\s(?:vs\.?|v\.?|at|@)\s", provider.title, re.I))
    small_team_set = 1 < len(entity_outcomes) <= 2 or 1 < len(provider.entities) <= 2
    return small_team_set and (has_draw or has_matchup_title)


def _best_outcome(candidate: str, outcomes: Sequence[str]) -> tuple[str, float] | None:
    if not outcomes:
        return None
    scores = [(outcome, name_similarity(candidate, outcome)) for outcome in outcomes]
    return max(scores, key=lambda item: item[1])


def _entity_score(pm_entities: Sequence[str], provider_entities: Sequence[str]) -> float:
    pm_clean = [_clean_entity(entity) for entity in pm_entities if _clean_entity(entity)]
    provider_clean = [
        _clean_entity(entity) for entity in provider_entities if _clean_entity(entity)
    ]
    if not pm_clean or not provider_clean:
        return 0.45
    if len(pm_clean) >= 2:
        return _best_pair_score(pm_clean[:2], provider_clean)
    return max(name_similarity(pm_clean[0], entity) for entity in provider_clean)


def _best_pair_score(pm_pair: Sequence[str], provider_entities: Sequence[str]) -> float:
    if len(provider_entities) < 2:
        best_single = max(
            name_similarity(pm_entity, provider_entity)
            for pm_entity in pm_pair
            for provider_entity in provider_entities
        )
        return min(0.72, best_single)
    best = 0.0
    for first in provider_entities:
        for second in provider_entities:
            if first == second:
                continue
            score = statistics.fmean(
                [
                    name_similarity(pm_pair[0], first),
                    name_similarity(pm_pair[1], second),
                ]
            )
            best = max(best, score)
    return best


def _time_score(left: datetime | None, right: datetime | None) -> float:
    if not left or not right:
        return 0.65
    seconds = abs((left - right).total_seconds())
    if seconds <= 2 * 3600:
        return 1.0
    if seconds <= 12 * 3600:
        return 0.86
    if seconds <= 36 * 3600:
        return 0.68
    if seconds <= 7 * 24 * 3600:
        return 0.35
    return 0.0


def _sport_score(left: str, right: str) -> float:
    left_norm = infer_sport(left)
    right_norm = infer_sport(right)
    canonical = {"mma", "soccer"}
    if left_norm.lower() not in canonical or right_norm.lower() not in canonical:
        return 0.70
    return 1.0 if left_norm.lower() == right_norm.lower() else 0.0


def _league_score(
    left_league: str | None,
    right_league: str | None,
    left_title: str,
    right_title: str,
) -> float:
    left = left_league or left_title
    right = right_league or right_title
    if not left_league and not right_league:
        return 0.65
    return name_similarity(left, right)


def _entity_like_outcomes(outcomes: Sequence[str]) -> list[str]:
    entities: list[str] = []
    for outcome in outcomes:
        normalized = normalize_text(outcome)
        if normalized in GENERIC_OUTCOMES:
            continue
        if normalized.startswith("over ") or normalized.startswith("under "):
            continue
        if re.fullmatch(r"\d+\s+\d+", normalized):
            continue
        cleaned = _clean_entity(outcome)
        if cleaned:
            entities.append(cleaned)
    return _dedupe_entities(entities)


def _dedupe_entities(entities: Sequence[str | None]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for entity in entities:
        if not entity:
            continue
        cleaned = _clean_entity(entity)
        key = normalize_text(cleaned)
        if not cleaned or key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
    return output


def _strip_market_descriptors(value: str) -> str:
    cleaned = MARKET_DESCRIPTOR_RE.sub(" ", value)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" ?:-")


def _clean_entity(value: str) -> str:
    cleaned = _strip_market_descriptors(str(value))
    cleaned = re.sub(r"\bby\s+(?:ko|tko|submission|decision).*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:ko|tko|submission|decision)\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?:-")
    return cleaned


def _tokens(value: str, keep_entity_suffixes: bool = True) -> set[str]:
    tokens = set(normalize_text(value).split())
    tokens -= STOPWORDS
    if not keep_entity_suffixes:
        tokens -= ENTITY_SUFFIXES
    return tokens


def _is_binary_yes_no_market(outcomes: Sequence[str]) -> bool:
    return {normalize_text(outcome) for outcome in outcomes} == {"yes", "no"}


def _event_id(payload: Mapping[str, Any]) -> str:
    value = (
        payload.get("id")
        or payload.get("eventId")
        or payload.get("event_id")
        or payload.get("eventid")
        or payload.get("uuid")
    )
    return str(value) if value is not None else ""


def _event_start_time(payload: Mapping[str, Any]) -> datetime | None:
    for key in (
        "startTime",
        "start_time",
        "startsAt",
        "starts_at",
        "commence_time",
        "startDate",
        "date",
        "timestamp",
    ):
        parsed = parse_datetime(payload.get(key))
        if parsed:
            return parsed
    return None


def _participants(payload: Mapping[str, Any]) -> list[str]:
    for key in ("participants", "competitors", "teams"):
        value = payload.get(key)
        if isinstance(value, list):
            names: list[str] = []
            for item in value:
                if isinstance(item, Mapping):
                    name = _nested_str(item, "name", "title", "teamName", "team_name")
                    if name:
                        names.append(name)
                elif item is not None:
                    names.append(str(item))
            if names:
                return names
    return []


def _league(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("league") or payload.get("competition") or payload.get("tournament")
    if isinstance(value, Mapping):
        return _nested_str(value, "name", "title", "slug")
    return str(value).strip() if value else None


def _nested_str(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping):
            nested = _nested_str(value, "name", "title", "slug")
            if nested:
                return nested
        elif value is not None:
            text = str(value).strip()
            if text:
                return text
    return None


def _join_non_empty(values: Sequence[str | None], separator: str) -> str:
    return separator.join(value for value in values if value)


def _mode(values: Sequence[str]) -> str | None:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]
