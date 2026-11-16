"""Offline reproductions from the publication review, including bypass attempts."""

from datetime import UTC, datetime, timedelta

import pytest

from edgelab.config.settings import Settings
from edgelab.data.external_sources.odds_provider import select_sports_odds_provider
from edgelab.data.schemas import BestBidAsk, ExternalAnchor, FairValue, PredictionMarket
from edgelab.execution.paper_trader import PaperTrader
from edgelab.fair_value.sports_odds_model import SportsOddsModel
from edgelab.matching.event_resolver import resolve_events
from edgelab.matching.market_classifier import classify_market
from edgelab.matching.outcome_mapper import binary_yes_target_from_title
from edgelab.research.live_analysis import rewrite_anchors_for_matches
from edgelab.strategies.taker_edge import compute_edge_signal

NOW = datetime(2026, 11, 16, 8, tzinfo=UTC)
START = NOW + timedelta(hours=3)
UNSUPPORTED_TITLES = [
    "Will Arsenal win by at least 2 against Chelsea?",
    "Will Arsenal beat Chelsea by two or more?",
    "Arsenal vs Chelsea: Arsenal to win or draw?",
    "Arsenal vs Chelsea: draw or Chelsea win?",
    "Arsenal vs Chelsea: double chance",
    "Arsenal vs Chelsea: both teams to score?",
    "Arsenal vs Chelsea: draw no bet",
    "Arsenal vs Chelsea: handicap -1.5",
    "Will Arsenal not win against Chelsea?",
    "Will Arsenal win and keep a clean sheet against Chelsea?",
    "Arsenal vs Chelsea: first half winner",
    "Arsenal vs Chelsea: first team to score",
    "Will Arsenal fail to win against Chelsea?",
    "Will either Arsenal or Chelsea win?",
    "Will Arsenal vs Chelsea end with no draw?",
]


def fixture(title="Will Arsenal beat Chelsea?"):
    return PredictionMarket(
        market_id="pm-market",
        event_id="pm-event",
        category="sports_1x2",
        title=title,
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        start_time=START,
        end_time=START + timedelta(hours=3),
        resolution_source="synthetic official result",
    )


def odds_rows():
    return [
        ExternalAnchor(
            source_id=f"book:{outcome}",
            event_id="external-event",
            source="SyntheticBook",
            category="sports_1x2",
            outcome=outcome,
            decimal_odds=odds,
            timestamp=NOW,
            available_at=NOW,
            metadata={"start_time": START.isoformat()},
        )
        for outcome, odds in [("Arsenal", 2), ("Draw", 3.5), ("Chelsea", 4)]
    ]


@pytest.mark.parametrize("title", UNSUPPORTED_TITLES)
def test_unsupported_fixture_payoffs_are_unknown_and_have_no_binary_target(title):
    assert classify_market(title, outcomes=["Yes", "No"]) == "unknown"
    assert binary_yes_target_from_title(title, "Arsenal", "Chelsea") is None


@pytest.mark.parametrize("title", UNSUPPORTED_TITLES)
def test_forced_category_and_target_cannot_price_an_unsupported_payoff(title):
    market = fixture(title).model_copy(update={"event_id": "external-event"})
    market.raw_metadata["target_outcome"] = "Arsenal"
    assert SportsOddsModel(Settings(_env_file=None), NOW).compute([market], odds_rows(), []) == []
    match = resolve_events([market], odds_rows(), as_of=NOW)[0]
    assert match.decision == "REJECT"
    assert match.rejection_reason == "unsupported_fixture_payoff"


def test_outright_title_cannot_be_overridden_by_fixture_context_in_description():
    assert (
        classify_market(
            "Will Arsenal win the Premier League?",
            "Upcoming fixture: Arsenal vs Chelsea",
            ["Yes", "No"],
        )
        == "sports_outright"
    )
    assert classify_market("Premier League: Arsenal vs Chelsea 1X2 match winner") == "sports_1x2"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Will Arsenal FC win against Chelsea?", "Arsenal"),
        ("Arsenal to win against Chelsea?", "Arsenal"),
        ("Arsenal vs Chelsea: will Chelsea win?", "Chelsea"),
        ("Will Arsenal vs Chelsea end in a draw?", "Draw"),
        ("Arsenal vs Chelsea: who will win?", None),
    ],
)
def test_binary_target_requires_an_unambiguous_subject(title, expected):
    assert binary_yes_target_from_title(title, "Arsenal", "Chelsea") == expected


def test_totals_and_other_contract_rows_do_not_pollute_fixture_identity():
    rows = odds_rows()
    extra = [
        rows[0].model_copy(
            update={"category": "sports_total", "outcome": outcome, "source_id": outcome}
        )
        for outcome in ["Over 2.5", "Under 2.5"]
    ]
    extra.append(rows[0].model_copy(update={"market_id": "unrelated", "outcome": "Other"}))
    before = resolve_events([fixture()], rows, as_of=NOW)[0]
    after = resolve_events([fixture()], [*rows, *extra], as_of=NOW)[0]
    assert before.decision == after.decision == "MATCH"
    assert before.matched_outcomes == after.matched_outcomes == {"Arsenal": "Yes"}


def test_declared_target_cannot_override_the_question_subject():
    market = fixture().model_copy(update={"event_id": "external-event"})
    market.raw_metadata["target_outcome"] = "Chelsea"
    assert SportsOddsModel(Settings(_env_file=None), NOW).compute([market], odds_rows(), []) == []


@pytest.mark.parametrize("title", UNSUPPORTED_TITLES)
def test_manual_signal_cannot_bypass_payoff_checks_at_paper_execution(title):
    market = fixture(title)
    market.raw_metadata["synthetic_data"] = True
    settings = Settings(_env_file=None)
    fair = FairValue(
        event_id=market.event_id,
        market_id=market.market_id,
        outcome="Yes",
        fair_probability=0.9,
        source="synthetic",
        timestamp=NOW,
        uncertainty=0.01,
        metadata={"match_decision": "MATCH", "settlement_verified": True},
    )
    book = BestBidAsk(
        market_id=market.market_id,
        token_id="yes",
        timestamp=NOW,
        best_bid=0.3,
        best_ask=0.32,
        best_bid_size=100,
        best_ask_size=100,
        spread=0.02,
    )
    signal = compute_edge_signal(market, fair, book, settings, NOW)
    assert signal.action == "AVOID"
    # Even a corrupted serialized action must not override the independent risk gate.
    forged = signal.model_copy(update={"action": "BUY_YES", "risk_approved": True})
    decision = PaperTrader(settings).execute([forged], {market.market_id: market}, NOW)[0]
    assert not decision.approved
    assert "unsupported fixture payoff" in decision.reason


def test_invalid_time_does_not_promote_an_inactive_market_to_review():
    rows = odds_rows()
    for row in rows:
        row.metadata["start_time"] = "invalid-time"
    match = resolve_events([fixture().model_copy(update={"status": "closed"})], rows, as_of=NOW)[0]
    assert match.decision == "REJECT"


def test_native_provider_mixed_markets_match_without_network():
    provider = select_sports_odds_provider(
        Settings(_env_file=None, ODDS_PROVIDER="odds_api_io", ODDS_API_IO_KEY="DUMMY_TEST")
    )
    rows = provider.normalize_to_external_odds(
        [
            {
                "id": "external-event",
                "home": "Arsenal",
                "away": "Chelsea",
                "sport": {"slug": "football"},
                "date": START.isoformat(),
                "bookmakers": {
                    "SyntheticBook": [
                        {
                            "name": "ML",
                            "updatedAt": NOW.isoformat(),
                            "odds": [{"home": 2, "draw": 3.5, "away": 4}],
                        },
                        {
                            "name": "Totals",
                            "updatedAt": NOW.isoformat(),
                            "odds": [{"hdp": 2.5, "over": 1.9, "under": 1.9}],
                        },
                        {
                            "name": "Unsupported special",
                            "updatedAt": NOW.isoformat(),
                            "odds": [{"yes": 2, "no": 2}],
                        },
                    ]
                },
            }
        ]
    )
    assert {r.category for r in rows if r.metadata["market_name"] == "Unsupported special"} == {
        "unknown"
    }
    matches = resolve_events([fixture()], rows, as_of=NOW)
    assert matches[0].decision == "MATCH"
    values = SportsOddsModel(Settings(_env_file=None), NOW).compute(
        [fixture()], rewrite_anchors_for_matches(rows, matches), []
    )
    assert len(values) == 1
    assert values[0].fair_probability == pytest.approx(0.4827586206896552)


def test_legacy_pricing_cannot_reenable_an_outright_from_binary_source_rows():
    from sharpedge.data.schemas import ExternalOdds, Market
    from sharpedge.research.fair_value_model import FairValueModel

    market = Market(
        market_id="legacy",
        event_id="world-cup",
        sport="Soccer",
        title="Will Spain win the FIFA World Cup?",
        outcomes=["Yes", "No"],
        token_ids=["legacy-yes", "legacy-no"],
    )
    rows = [
        ExternalOdds(
            event_id="world-cup",
            sport="Soccer",
            book="SyntheticBook",
            market_type="outright",
            outcome=outcome,
            decimal_odds=odds,
            timestamp=NOW,
        )
        for outcome, odds in [("Yes", 5.5), ("No", 1.2)]
    ]
    with pytest.warns(DeprecationWarning, match="SharpEdge pricing is deprecated"):
        assert FairValueModel(now=NOW).compute_fair_values([market], rows, as_of=NOW) == []


def test_legacy_binary_mapping_does_not_manufacture_opposite_odds():
    from sharpedge.data.event_resolver import _map_external_outcome_to_polymarket
    from sharpedge.data.schemas import ExternalOdds, Market

    market = Market(
        market_id="legacy",
        event_id="fixture",
        sport="Soccer",
        title="Will Arsenal win against Chelsea?",
        outcomes=["Yes", "No"],
        token_ids=["legacy-yes", "legacy-no"],
    )
    row = ExternalOdds(
        event_id="fixture",
        sport="Soccer",
        book="SyntheticBook",
        market_type="1x2",
        outcome="Arsenal",
        decimal_odds=2,
        timestamp=NOW,
    )
    assert _map_external_outcome_to_polymarket(market, row) == []
