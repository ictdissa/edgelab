from __future__ import annotations

from datetime import UTC, datetime, timedelta

from edgelab.data.schemas import ExternalAnchor, PredictionMarket
from edgelab.matching.sports_discovery import (
    SPORTS_DECISION_DATE,
    SPORTS_DECISION_MARKET_TYPE,
    SPORTS_DECISION_NO_MARKET,
    audit_sports_matching,
)


def _anchor(
    event_id: str = "ext1",
    home: str = "Arsenal",
    away: str = "Chelsea",
    commence_time: datetime | None = None,
) -> ExternalAnchor:
    return ExternalAnchor(
        source_id=f"book:{event_id}:home",
        event_id=event_id,
        source="book",
        category="sports_1x2",
        outcome=home,
        decimal_odds=2.0,
        timestamp=datetime(2026, 5, 20, tzinfo=UTC),
        metadata={
            "home": home,
            "away": away,
            "event_title": f"{home} vs {away}",
            "league": "Premier League",
            "commence_time": (commence_time or datetime(2026, 6, 1, tzinfo=UTC)).isoformat(),
        },
    )


def _market(
    title: str,
    category: str = "sports_1x2",
    start_time: datetime | None = None,
) -> PredictionMarket:
    return PredictionMarket(
        market_id=title.lower().replace(" ", "-"),
        event_id="pm1",
        category=category,
        title=title,
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        start_time=start_time,
        resolution_source="official",
    )


def test_fixture_odds_must_not_match_world_cup_outright_markets() -> None:
    audits = audit_sports_matching(
        [_anchor(home="France", away="Brazil")],
        [_market("Will France win the World Cup?", category="sports_outright")],
    )

    assert audits[0].decision == SPORTS_DECISION_MARKET_TYPE


def test_no_polymarket_candidate_is_no_market_found() -> None:
    audits = audit_sports_matching([_anchor()], [_market("Will Liverpool beat Everton?")])

    assert audits[0].decision == SPORTS_DECISION_NO_MARKET


def test_market_type_differs_is_market_type_mismatch() -> None:
    audits = audit_sports_matching(
        [_anchor()],
        [_market("Will Arsenal win the Premier League?", category="sports_outright")],
    )

    assert audits[0].decision == SPORTS_DECISION_MARKET_TYPE


def test_teams_match_but_date_differs_is_date_mismatch() -> None:
    audits = audit_sports_matching(
        [_anchor(commence_time=datetime(2026, 6, 1, tzinfo=UTC))],
        [_market("Will Arsenal beat Chelsea?", start_time=datetime(2026, 6, 10, tzinfo=UTC))],
        date_tolerance=timedelta(days=1),
    )

    assert audits[0].decision == SPORTS_DECISION_DATE
