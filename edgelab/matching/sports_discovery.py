"""Sports opportunity discovery and failure-mode auditing."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from edgelab.data.schemas import ExternalAnchor, PredictionMarket
from edgelab.matching.outcome_mapper import similarity

SPORTS_DECISION_MATCH = "MATCH"
SPORTS_DECISION_NO_MARKET = "NO_MARKET_FOUND"
SPORTS_DECISION_MARKET_TYPE = "MARKET_TYPE_MISMATCH"
SPORTS_DECISION_DATE = "DATE_MISMATCH"
SPORTS_DECISION_LEAGUE = "LEAGUE_MISMATCH"
SPORTS_DECISION_TITLE_PARSE = "TITLE_PARSE_FAILED"


@dataclass(frozen=True)
class SportsEventCandidate:
    external_event_id: str
    home: str
    away: str
    league: str
    commence_time: datetime | None
    category: str
    event_title: str


@dataclass(frozen=True)
class QueryAudit:
    query: str
    candidate_count: int
    market_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SportsMatchFailure:
    external_event_id: str
    event_title: str
    market_id: str
    market_title: str
    decision: str
    reason: str
    queries: list[QueryAudit]
    best_score: float
    market_type: str
    external_type: str


def audit_sports_matching(
    anchors: Sequence[ExternalAnchor],
    markets: Sequence[PredictionMarket],
    date_tolerance: timedelta = timedelta(days=1),
) -> list[SportsMatchFailure]:
    """Run odds-first sports discovery over already-collected Polymarket candidates."""

    events = _events_from_anchors(anchors)
    audits: list[SportsMatchFailure] = []
    for event in events:
        queries = _queries_for_event(event)
        query_audits = [_run_query(query, markets) for query in queries]
        candidate_ids = {market_id for audit in query_audits for market_id in audit.market_ids}
        candidates = [market for market in markets if market.market_id in candidate_ids]
        if not candidates:
            audits.append(
                SportsMatchFailure(
                    external_event_id=event.external_event_id,
                    event_title=event.event_title,
                    market_id="",
                    market_title="",
                    decision=SPORTS_DECISION_NO_MARKET,
                    reason="Polymarket candidate search returned no active market for this odds event",
                    queries=query_audits,
                    best_score=0.0,
                    market_type="",
                    external_type=event.category,
                )
            )
            continue
        best = max(candidates, key=lambda market: _event_market_score(event, market))
        decision, reason = classify_candidate_match(event, best, date_tolerance=date_tolerance)
        audits.append(
            SportsMatchFailure(
                external_event_id=event.external_event_id,
                event_title=event.event_title,
                market_id=best.market_id,
                market_title=best.title,
                decision=decision,
                reason=reason,
                queries=query_audits,
                best_score=_event_market_score(event, best),
                market_type=best.category,
                external_type=event.category,
            )
        )
    return audits


def audit_polymarket_first(
    markets: Sequence[PredictionMarket],
    anchors: Sequence[ExternalAnchor],
    date_tolerance: timedelta = timedelta(days=1),
) -> list[SportsMatchFailure]:
    """Explain why active Polymarket sports markets do not map to external odds."""

    events = _events_from_anchors(anchors)
    audits: list[SportsMatchFailure] = []
    for market in markets:
        if not str(market.category).startswith("sports"):
            continue
        if market.category == "sports_outright" and not any(
            event.category == "sports_outright" for event in events
        ):
            audits.append(
                SportsMatchFailure(
                    external_event_id="",
                    event_title="",
                    market_id=market.market_id,
                    market_title=market.title,
                    decision=SPORTS_DECISION_MARKET_TYPE,
                    reason="Polymarket market is an outright/future but external odds are event-level fixtures",
                    queries=[],
                    best_score=0.0,
                    market_type=market.category,
                    external_type="event_fixture",
                )
            )
            continue
        if not events:
            audits.append(
                SportsMatchFailure(
                    external_event_id="",
                    event_title="",
                    market_id=market.market_id,
                    market_title=market.title,
                    decision=SPORTS_DECISION_NO_MARKET,
                    reason="No external sports odds event was available for comparison",
                    queries=[],
                    best_score=0.0,
                    market_type=market.category,
                    external_type="",
                )
            )
            continue
        best = max(events, key=lambda event: _event_market_score(event, market))
        decision, reason = classify_candidate_match(best, market, date_tolerance=date_tolerance)
        audits.append(
            SportsMatchFailure(
                external_event_id=best.external_event_id,
                event_title=best.event_title,
                market_id=market.market_id,
                market_title=market.title,
                decision=decision,
                reason=reason,
                queries=[],
                best_score=_event_market_score(best, market),
                market_type=market.category,
                external_type=best.category,
            )
        )
    return audits


def classify_candidate_match(
    event: SportsEventCandidate,
    market: PredictionMarket,
    date_tolerance: timedelta = timedelta(days=1),
) -> tuple[str, str]:
    if not event.home or not event.away:
        return (
            SPORTS_DECISION_TITLE_PARSE,
            "External odds event did not include parseable home/away teams",
        )
    if (
        event.category in {"sports_1x2", "sports_moneyline"}
        and market.category == "sports_outright"
    ):
        return (
            SPORTS_DECISION_MARKET_TYPE,
            "Fixture-level odds cannot anchor a Polymarket outright/futures market",
        )
    if market.category not in {
        "sports_1x2",
        "sports_moneyline",
        "sports_total",
        "sports_exact_score",
    }:
        return (
            SPORTS_DECISION_MARKET_TYPE,
            f"Unsupported sports market type for fixture odds: {market.category}",
        )
    if not _teams_mentioned(event, market.title):
        return SPORTS_DECISION_NO_MARKET, "Best candidate does not mention both event teams"
    market_time = market.start_time or market.end_time
    if event.commence_time and market_time:
        delta = abs(event.commence_time - market_time)
        if delta > date_tolerance:
            return SPORTS_DECISION_DATE, f"Teams match, but event dates differ by {delta}"
    league = event.league.lower().strip()
    if (
        league
        and league not in _normalize(market.title)
        and _looks_like_league_specific(market.title)
    ):
        return SPORTS_DECISION_LEAGUE, "Teams/title matched but league context differs"
    if _event_market_score(event, market) >= 0.78:
        return SPORTS_DECISION_MATCH, "Teams, type, and date checks passed"
    return SPORTS_DECISION_NO_MARKET, "No candidate exceeded sports discovery score threshold"


def write_sports_match_failure_audit(
    path: Path,
    odds_first: Sequence[SportsMatchFailure],
    polymarket_first: Sequence[SportsMatchFailure],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Sports Match Failure Audit",
        "",
        "## Summary",
        "",
        f"- odds-first events audited: {len(odds_first)}",
        f"- Polymarket-first markets audited: {len(polymarket_first)}",
        "",
        "### Odds-First Decisions",
        "",
    ]
    lines.extend(_decision_table(odds_first))
    lines.extend(["", "### Polymarket-First Decisions", ""])
    lines.extend(_decision_table(polymarket_first))
    lines.extend(["", "## Odds Events Searched", ""])
    if not odds_first:
        lines.append("No odds events were available.")
    else:
        for row in odds_first[:25]:
            lines.extend(
                [
                    f"### {row.event_title or row.external_event_id}",
                    "",
                    f"- decision: `{row.decision}`",
                    f"- reason: {row.reason}",
                    f"- best Polymarket candidate: `{row.market_title}`",
                    f"- best score: `{row.best_score:.3f}`",
                    f"- market type: `{row.market_type}` vs external `{row.external_type}`",
                    "",
                    "| query | candidates | market_ids |",
                    "| --- | --- | --- |",
                ]
            )
            for query in row.queries:
                lines.append(
                    f"| {query.query.replace('|', '/')} | {query.candidate_count} | "
                    f"{', '.join(query.market_ids[:5])} |"
                )
            lines.append("")
    lines.extend(["", "## Polymarket Markets Without Comparable Odds", ""])
    for row in polymarket_first[:50]:
        lines.append(f"- `{row.market_id}`: {row.decision} - {row.reason} - {row.market_title}")
    path.write_text("\n".join(lines), encoding="utf-8")


def _events_from_anchors(anchors: Sequence[ExternalAnchor]) -> list[SportsEventCandidate]:
    grouped: dict[str, list[ExternalAnchor]] = defaultdict(list)
    for anchor in anchors:
        if str(anchor.category).startswith("sports"):
            grouped[anchor.event_id].append(anchor)
    events: list[SportsEventCandidate] = []
    for event_id, rows in grouped.items():
        first = rows[0]
        meta = first.metadata
        home = str(meta.get("home") or "").strip()
        away = str(meta.get("away") or "").strip()
        title = str(meta.get("event_title") or f"{home} vs {away}").strip(" vs ")
        if not home or not away:
            home, away = _teams_from_title(title)
        events.append(
            SportsEventCandidate(
                external_event_id=event_id,
                home=home,
                away=away,
                league=str(meta.get("league") or meta.get("sport_key") or ""),
                commence_time=_parse_time(meta.get("commence_time") or meta.get("start_time")),
                category=first.category,
                event_title=title or event_id,
            )
        )
    return events


def _queries_for_event(event: SportsEventCandidate) -> list[str]:
    queries = [
        event.home,
        event.away,
        f"{event.home} vs {event.away}",
        f"{event.away} vs {event.home}",
    ]
    if event.league:
        queries.append(event.league)
    return [query for query in dict.fromkeys(q.strip() for q in queries) if query]


def _run_query(query: str, markets: Sequence[PredictionMarket]) -> QueryAudit:
    query_norm = _normalize(query)
    ids = [
        market.market_id
        for market in markets
        if query_norm
        and (query_norm in _normalize(market.title) or similarity(query, market.title) >= 0.68)
    ]
    return QueryAudit(query=query, candidate_count=len(ids), market_ids=ids)


def _event_market_score(event: SportsEventCandidate, market: PredictionMarket) -> float:
    text = market.title
    components = [
        similarity(event.home, text) if event.home else 0.0,
        similarity(event.away, text) if event.away else 0.0,
        similarity(event.event_title, text) if event.event_title else 0.0,
    ]
    both_teams_bonus = 0.20 if _teams_mentioned(event, text) else 0.0
    return min(1.0, max(components) * 0.65 + sum(components[:2]) / 2 * 0.35 + both_teams_bonus)


def _teams_mentioned(event: SportsEventCandidate, title: str) -> bool:
    text = _normalize(title)
    home_tokens = _team_tokens(event.home)
    away_tokens = _team_tokens(event.away)
    return bool(
        home_tokens
        and away_tokens
        and home_tokens & set(text.split())
        and away_tokens & set(text.split())
    )


def _team_tokens(team: str) -> set[str]:
    stop = {"fc", "sc", "cf", "afc", "club", "the", "women", "reserves", "u21", "u23"}
    return {token for token in _normalize(team).split() if len(token) > 2 and token not in stop}


def _teams_from_title(title: str) -> tuple[str, str]:
    for separator in (" vs ", " v ", " against "):
        if separator in title.lower():
            parts = re.split(separator, title, maxsplit=1, flags=re.IGNORECASE)
            return parts[0].strip(), parts[1].strip()
    return "", ""


def _looks_like_league_specific(title: str) -> bool:
    text = _normalize(title)
    return any(word in text for word in ("league", "cup", "tournament", "season", "championship"))


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _decision_table(rows: Iterable[SportsMatchFailure]) -> list[str]:
    counts = Counter(row.decision for row in rows)
    output = ["| decision | count |", "| --- | --- |"]
    if not counts:
        output.append("| none | 0 |")
    else:
        output.extend(f"| {decision} | {count} |" for decision, count in counts.items())
    return output


def _normalize(value: str) -> str:
    lowered = value.lower().replace("&", " and ")
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()
