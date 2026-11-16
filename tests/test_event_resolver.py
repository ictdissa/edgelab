from __future__ import annotations

from datetime import UTC, datetime

from sharpedge.data.event_resolver import (
    EventResolver,
    PolymarketCandidate,
    ProviderEvent,
    resolve_market_event_matches,
    rewrite_odds_for_polymarket,
)
from sharpedge.data.schemas import ExternalOdds, Market
from edgelab.data.schemas import ExternalAnchor, PredictionMarket
from edgelab.matching.event_resolver import resolve_events as edgelab_resolve_events
from edgelab.matching.event_resolver import write_event_match_audit


def test_resolver_matches_soccer_fixture_and_rewrites_outcomes() -> None:
    start = datetime(2026, 6, 2, 19, tzinfo=UTC)
    market = Market(
        market_id="pm-1",
        event_id="pm-ars-che",
        sport="Soccer",
        league="Premier League",
        title="Premier League: Arsenal vs Chelsea 1X2 match winner",
        outcomes=["Arsenal", "Draw", "Chelsea"],
        token_ids=["ars", "draw", "che"],
        start_time=start,
        status="active",
    )
    provider = ProviderEvent(
        provider_event_id="odds-1",
        title="Arsenal vs Chelsea",
        sport="football",
        league="Premier League",
        start_time=start,
        entities=("Arsenal", "Chelsea"),
        outcomes=("Arsenal", "Draw", "Chelsea"),
    )
    odds = [
        ExternalOdds(
            event_id="odds-1",
            sport="Soccer",
            book="Pinnacle",
            market_type="1x2",
            outcome=outcome,
            decimal_odds=price,
            timestamp=start,
        )
        for outcome, price in [("Arsenal", 2.1), ("Draw", 3.4), ("Chelsea", 3.3)]
    ]
    odds.append(
        ExternalOdds(
            event_id="odds-1",
            sport="Soccer",
            book="Pinnacle",
            market_type="exact_score",
            outcome="Arsenal 1-0",
            decimal_odds=8.5,
            timestamp=start,
        )
    )

    matches = resolve_market_event_matches([market], [provider], odds)
    assert matches[0].decision == "matched"
    assert matches[0].confidence > 0.90

    rewritten = rewrite_odds_for_polymarket([market], odds, matches)
    assert {(row.event_id, row.market_type, row.outcome) for row in rewritten} == {
        ("pm-ars-che", "1x2", "Arsenal"),
        ("pm-ars-che", "1x2", "Draw"),
        ("pm-ars-che", "1x2", "Chelsea"),
    }


def test_legacy_rewrite_rejects_partial_outright_instead_of_synthetic_complement() -> None:
    timestamp = datetime(2026, 5, 16, tzinfo=UTC)
    market = Market(
        market_id="pm-spain",
        event_id="pm-spain-world-cup",
        sport="Sports",
        league=None,
        title="Will Spain win the 2026 FIFA World Cup?",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        start_time=None,
        status="active",
    )
    provider = ProviderEvent(
        provider_event_id="wc-2026",
        title="2026 FIFA World Cup Winner",
        sport="football",
        league="FIFA World Cup",
        start_time=None,
        entities=("Spain", "France", "England"),
        outcomes=("Spain", "France", "England"),
    )
    odds = [
        ExternalOdds(
            event_id="wc-2026",
            sport="Soccer",
            book="Pinnacle",
            market_type="moneyline",
            outcome="Spain",
            decimal_odds=5.5,
            timestamp=timestamp,
        ),
        ExternalOdds(
            event_id="wc-2026",
            sport="Soccer",
            book="Pinnacle",
            market_type="moneyline",
            outcome="France",
            decimal_odds=6.0,
            timestamp=timestamp,
        ),
    ]

    matches = resolve_market_event_matches([market], [provider], odds)
    assert matches[0].decision == "matched"

    rewritten = rewrite_odds_for_polymarket([market], odds, matches)
    assert rewritten == []


def test_resolver_rejects_wrong_fixture() -> None:
    market = Market(
        market_id="pm-1",
        event_id="pm-ars-che",
        sport="Soccer",
        league="Premier League",
        title="Premier League: Arsenal vs Chelsea 1X2 match winner",
        outcomes=["Arsenal", "Draw", "Chelsea"],
        token_ids=["ars", "draw", "che"],
        status="active",
    )
    wrong_provider = ProviderEvent(
        provider_event_id="odds-2",
        title="Barcelona vs Real Madrid",
        sport="football",
        league="La Liga",
        entities=("Barcelona", "Real Madrid"),
        outcomes=("Barcelona", "Draw", "Real Madrid"),
    )

    matches = resolve_market_event_matches([market], [wrong_provider], [])
    assert matches[0].decision == "rejected"
    assert matches[0].confidence < 0.72


def test_outright_market_does_not_match_single_fixture_for_same_team() -> None:
    market = Market(
        market_id="pm-spain",
        event_id="pm-spain-world-cup",
        sport="Sports",
        league=None,
        title="Will Spain win the 2026 FIFA World Cup?",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        status="active",
    )
    fixture = ProviderEvent(
        provider_event_id="spain-france",
        title="Spain vs France",
        sport="football",
        league="FIFA World Cup",
        entities=("Spain", "France"),
        outcomes=("Spain", "Draw", "France"),
    )

    matches = resolve_market_event_matches([market], [fixture], [])
    assert matches[0].decision == "rejected"
    assert matches[0].confidence < 0.72


def test_event_resolver_class_matches_reversed_fixture_order() -> None:
    odds_event = {
        "id": "odds-ars-che",
        "home": "Arsenal",
        "away": "Chelsea",
        "league": {"name": "England - Premier League"},
        "sport": {"name": "Football"},
        "date": "2026-06-01T12:00:00Z",
    }
    candidate = PolymarketCandidate(
        odds_event_id="odds-ars-che",
        event_id="pm-event",
        market_id="pm-market",
        title="Chelsea vs. Arsenal",
        sport="Soccer",
        league="Premier League",
        start_time=datetime(2026, 6, 1, 12, tzinfo=UTC),
        outcomes=("Chelsea", "Draw", "Arsenal"),
        token_ids=("che", "draw", "ars"),
        status="active",
        closed=False,
        active=True,
        accepting_orders=True,
    )
    result = EventResolver(min_match_score=0.82).match_odds_events_to_polymarket(
        [odds_event],
        [candidate],
    )
    assert len(result.matched) == 1
    assert result.matched[0].outcome_mapping["Arsenal"] == "Arsenal"
    assert result.matched[0].outcome_mapping["Chelsea"] == "Chelsea"


def test_event_resolver_matches_polymarket_binary_draw_with_sparse_metadata() -> None:
    odds_event = {
        "id": "61301259",
        "home": "Leeds United",
        "away": "Brighton & Hove Albion",
        "league": {"name": "England - Premier League"},
        "sport": {"name": "Football"},
        "date": "2026-05-17T14:00:00Z",
    }
    candidate = PolymarketCandidate(
        odds_event_id="61301259",
        event_id="446672",
        market_id="0x33769d8e6d84cedf22b802960bee0b21be649db958cad0e71e4746f5a9bf6a04",
        title="Will Leeds United FC vs. Brighton & Hove Albion FC end in a draw?",
        sport="Leeds United FC vs. Brighton & Hove Albion FC",
        league=None,
        start_time=None,
        outcomes=("Yes", "No"),
        token_ids=("yes-token", "no-token"),
        status="active",
        closed=False,
        active=True,
        accepting_orders=True,
    )
    result = EventResolver(min_match_score=0.82).match_odds_events_to_polymarket(
        [odds_event],
        [candidate],
    )
    assert len(result.matched) == 1
    assert result.matched[0].market_type == "1x2"


def test_event_resolver_class_rejects_fixture_to_world_cup_outright() -> None:
    odds_event = {
        "id": "odds-ars-che",
        "home": "Arsenal",
        "away": "Chelsea",
        "league": {"name": "England - Premier League"},
        "sport": {"name": "Football"},
        "date": "2026-06-01T12:00:00Z",
    }
    candidate = PolymarketCandidate(
        odds_event_id="odds-ars-che",
        event_id="pm-spain",
        market_id="pm-spain-market",
        title="Will Spain win the 2026 FIFA World Cup?",
        sport="Soccer",
        league="FIFA World Cup",
        start_time=datetime(2026, 6, 1, 12, tzinfo=UTC),
        outcomes=("Yes", "No"),
        token_ids=("yes", "no"),
        status="active",
        closed=False,
        active=True,
        accepting_orders=True,
    )
    result = EventResolver(min_match_score=0.82).match_odds_events_to_polymarket(
        [odds_event],
        [candidate],
    )
    assert len(result.rejected) == 1
    assert result.rejected[0].rejection_reason == "fixture_odds_cannot_match_futures_or_outright_market"


def test_event_resolver_class_single_team_title_is_review_or_reject() -> None:
    odds_event = {
        "id": "odds-ars-che",
        "home": "Arsenal",
        "away": "Chelsea",
        "league": {"name": "England - Premier League"},
        "sport": {"name": "Football"},
        "date": "2026-06-01T12:00:00Z",
    }
    candidate = PolymarketCandidate(
        odds_event_id="odds-ars-che",
        event_id="pm-ars",
        market_id="pm-ars-market",
        title="Arsenal match winner",
        sport="Soccer",
        league="Premier League",
        start_time=datetime(2026, 6, 1, 12, tzinfo=UTC),
        outcomes=("Arsenal", "No"),
        token_ids=("yes", "no"),
        status="active",
        closed=False,
        active=True,
        accepting_orders=True,
    )
    result = EventResolver(min_match_score=0.82).match_odds_events_to_polymarket(
        [odds_event],
        [candidate],
    )
    assert not result.matched
    assert result.review or result.rejected


def test_edgelab_resolver_rejects_partial_outright_field() -> None:
    market = PredictionMarket(
        market_id="spain",
        event_id="world-cup",
        category="sports_outright",
        title="Will Spain win the 2026 FIFA World Cup?",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        resolution_source="FIFA",
    )
    anchors = [
        ExternalAnchor(
            source_id="a",
            event_id="other",
            source="mock",
            category="sports_1x2",
            outcome="Spain",
            decimal_odds=2.0,
        )
    ]
    match = edgelab_resolve_events([market], anchors)[0]
    assert match.decision == "REJECT"
    assert any("partial outright" in reason for reason in match.reasons)


def test_edgelab_event_match_audit_writes_diagnostics(tmp_path) -> None:
    market = PredictionMarket(
        market_id="btc",
        event_id="btc-event",
        category="crypto_threshold",
        title="Will BTC be above 100k?",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        resolution_source="index",
    )
    anchor = ExternalAnchor(
        source_id="btc-anchor",
        event_id="btc-event",
        source="mock",
        category="crypto_threshold",
        outcome="Yes",
        probability=0.5,
    )
    matches = edgelab_resolve_events([market], [anchor])
    path = tmp_path / "audit.csv"
    write_event_match_audit(path, matches)
    text = path.read_text()
    assert "candidate_count" in text
    assert "MATCH" in text
