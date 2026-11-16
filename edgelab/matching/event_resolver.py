"""Event matching with explicit audit reasons."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from dataclasses import replace
from datetime import datetime
import re
from typing import Sequence

from edgelab.data.schemas import ExternalAnchor, PredictionMarket
from edgelab.matching.market_classifier import classify_market, is_fixture_winner_contract
from edgelab.matching.outcome_mapper import binary_yes_target_from_title, normalize_name, similarity

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventMatch:
    external_event_id: str
    prediction_market_event_id: str
    market_id: str
    title: str
    matched_outcomes: dict[str, str]
    score: float
    decision: str
    reasons: list[str]
    rejection_reason: str | None = None
    candidate_count: int = 0
    best_alternative_score: float | None = None


def resolve_events(
    markets: Sequence[PredictionMarket],
    anchors: Sequence[ExternalAnchor],
    min_score: float = 0.80,
    as_of: datetime | None = None,
) -> list[EventMatch]:
    events: dict[str, list[ExternalAnchor]] = {}
    for anchor in anchors:
        events.setdefault(anchor.event_id, []).append(anchor)

    matches: list[EventMatch] = []
    for market in markets:
        candidates = []
        for event_id, rows in events.items():
            compatible = [
                a
                for a in rows
                if a.category == market.category
                and (a.market_id is None or a.market_id == market.market_id)
            ]
            # Totals, props and another venue contract cannot become fixture participants.
            outcomes = {a.outcome for a in compatible}
            score = 1.0 if event_id == market.event_id else _title_event_score(market, outcomes)
            reasons = [f"event_id_match={event_id == market.event_id}", f"score={score:.3f}"]
            decision = "MATCH" if score >= min_score else "REJECT"
            rejection_reason = None if decision == "MATCH" else "below_match_threshold"
            matched = _matched_outcomes(market, outcomes)
            if market.category == "sports_outright" and not all(
                a.metadata.get("complete_field") is True for a in rows
            ):
                decision = "REJECT"
                rejection_reason = "partial_outright_field"
                reasons.append("partial outright field cannot anchor a yes/no outright")
            if not compatible:
                decision, rejection_reason = "REJECT", "fixture_or_contract_type_mismatch"
            if (
                market.category in {"sports_1x2", "sports_moneyline"}
                and _title_event_score(market, outcomes) < 0.80
            ):
                decision, rejection_reason = "REJECT", "both_fixture_teams_required"
                reasons.append(
                    "both fixture teams must be present in title or verified fixture context"
                )
            if market.status != "active" or (
                as_of and market.end_time and market.end_time <= as_of
            ):
                decision, rejection_reason = "REJECT", "inactive_or_expired"
            times = [
                a.metadata.get("start_time") for a in compatible if a.metadata.get("start_time")
            ]
            if (
                market.category in {"sports_1x2", "sports_moneyline"}
                and event_id != market.event_id
                and not (market.start_time and times)
                and decision != "REJECT"
            ):
                decision, rejection_reason = "REVIEW", "fixture_time_unverified"
                reasons.append("both kickoff times required for cross-provider matching")
            if market.start_time and times:
                try:
                    gap = abs(
                        (
                            market.start_time
                            - datetime.fromisoformat(str(times[0]).replace("Z", "+00:00"))
                        ).total_seconds()
                    )
                    if gap > 6 * 3600:
                        decision, rejection_reason = "REJECT", "fixture_start_time_mismatch"
                except (ValueError, TypeError):
                    if decision != "REJECT":
                        decision, rejection_reason = "REVIEW", "invalid_fixture_time"
            if market.raw_metadata.get("league") and compatible:
                league = compatible[0].metadata.get("league")
                if league and similarity(str(league), str(market.raw_metadata["league"])) < 0.75:
                    decision, rejection_reason = "REJECT", "league_mismatch"
            if not matched:
                decision = "REJECT"
                rejection_reason = rejection_reason or "no_outcome_mapping"
                reasons.append("no outcome mapping")
            if market.category in {"sports_1x2", "sports_moneyline"} and not (
                is_fixture_winner_contract(market.title, market.description, market.outcomes)
            ):
                decision, rejection_reason = "REJECT", "unsupported_fixture_payoff"
                reasons.append("contract is not an unambiguous full-match winner or draw")
            candidates.append(
                EventMatch(
                    external_event_id=event_id,
                    prediction_market_event_id=market.event_id,
                    market_id=market.market_id,
                    title=market.title,
                    matched_outcomes=matched,
                    score=score,
                    decision=decision,
                    reasons=reasons,
                    rejection_reason=rejection_reason,
                )
            )
        candidates.sort(key=lambda row: (row.decision != "REJECT", row.score), reverse=True)
        if candidates:
            best = candidates[0]
            alternative = candidates[1].score if len(candidates) > 1 else None
            viable = [c for c in candidates[1:] if c.decision != "REJECT"]
            if best.decision != "REJECT" and viable and best.score - viable[0].score < 0.05:
                best = replace(
                    best,
                    decision="REVIEW",
                    rejection_reason="ambiguous_event_candidates",
                    reasons=[*best.reasons, "runner-up score gap below 0.05"],
                )
            matches.append(
                EventMatch(
                    external_event_id=best.external_event_id,
                    prediction_market_event_id=best.prediction_market_event_id,
                    market_id=best.market_id,
                    title=best.title,
                    matched_outcomes=best.matched_outcomes,
                    score=best.score,
                    decision=best.decision,
                    reasons=best.reasons,
                    rejection_reason=best.rejection_reason,
                    candidate_count=len(candidates),
                    best_alternative_score=alternative,
                )
            )
        else:
            matches.append(
                EventMatch(
                    external_event_id="",
                    prediction_market_event_id=market.event_id,
                    market_id=market.market_id,
                    title=market.title,
                    matched_outcomes={},
                    score=0.0,
                    decision="REJECT",
                    reasons=["no external anchor candidates"],
                    rejection_reason="no_external_anchor_candidates",
                    candidate_count=0,
                )
            )
    LOGGER.info(
        "event_resolution_complete markets=%d matches=%d rejects=%d",
        len(markets),
        sum(1 for match in matches if match.decision == "MATCH"),
        sum(1 for match in matches if match.decision != "MATCH"),
    )
    return matches


def write_event_match_audit(path: Path, matches: Sequence[EventMatch]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "market_id",
        "title",
        "external_event_id",
        "decision",
        "score",
        "candidate_count",
        "best_alternative_score",
        "matched_outcomes",
        "rejection_reason",
        "reasons",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for match in matches:
            writer.writerow(
                {
                    "market_id": match.market_id,
                    "title": match.title,
                    "external_event_id": match.external_event_id,
                    "decision": match.decision,
                    "score": f"{match.score:.4f}",
                    "candidate_count": match.candidate_count,
                    "best_alternative_score": ""
                    if match.best_alternative_score is None
                    else f"{match.best_alternative_score:.4f}",
                    "matched_outcomes": match.matched_outcomes,
                    "rejection_reason": match.rejection_reason or "",
                    "reasons": "; ".join(match.reasons),
                }
            )


def _title_event_score(market: PredictionMarket, outcomes: set[str]) -> float:
    teams = [o for o in outcomes if o.casefold() not in {"draw", "yes", "no"}]
    if len(teams) != 2:
        return 0.0
    text = normalize_name(f"{market.title} {market.raw_metadata.get('fixture', '')}")
    return (
        0.96
        if all(re.search(rf"\b{re.escape(normalize_name(team))}\b", text) for team in teams)
        else 0.0
    )


def _matched_outcomes(market: PredictionMarket, outcomes: set[str]) -> dict[str, str]:
    if {outcome.lower() for outcome in market.outcomes} == {"yes", "no"}:
        if market.category in {"sports_1x2", "sports_moneyline"}:
            sorted_outcomes = sorted(outcomes)
            non_draw = [outcome for outcome in sorted_outcomes if outcome.lower() != "draw"]
            target = binary_yes_target_from_title(
                market.title,
                non_draw[0] if non_draw else "",
                non_draw[-1] if non_draw else "",
            )
            return {target: "Yes"} if target else {}
        if classify_market(market.title, market.description, market.outcomes) in {
            "crypto_threshold",
            "weather_threshold",
        }:
            return {"Yes": "Yes"}
    return {outcome: outcome for outcome in outcomes if outcome in market.outcomes}
