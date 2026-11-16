from __future__ import annotations

from sharpedge.data.odds_event_matcher import match_polymarket_text_to_odds_events


def test_typed_polymarket_fixture_matches_reversed_odds_event() -> None:
    events = [
        {
            "id": 1,
            "home": "Chelsea",
            "away": "Arsenal",
            "league": {"name": "England - Premier League"},
            "sport": {"name": "Football"},
            "date": "2026-06-01T12:00:00Z",
        }
    ]
    candidates = match_polymarket_text_to_odds_events("Arsenal vs Chelsea", events)
    assert candidates[0].decision == "MATCH"
    assert candidates[0].score >= 0.82


def test_typed_outright_does_not_match_fixture_odds() -> None:
    events = [
        {
            "id": 1,
            "home": "Spain",
            "away": "France",
            "league": {"name": "FIFA World Cup"},
            "sport": {"name": "Football"},
            "date": "2026-06-01T12:00:00Z",
        }
    ]
    candidates = match_polymarket_text_to_odds_events(
        "Will Spain win the 2026 FIFA World Cup?",
        events,
    )
    assert candidates[0].decision == "REJECT"
    assert candidates[0].rejection_reason == "typed_polymarket_event_is_futures_or_outright_not_fixture"
