from __future__ import annotations

from edgelab.matching.market_classifier import classify_market


def test_classifier_requested_cases() -> None:
    assert classify_market("Will Spain win the 2026 FIFA World Cup?", outcomes=["Yes", "No"]) == "sports_outright"
    assert classify_market("Arsenal vs Chelsea match winner", outcomes=["Arsenal", "Draw", "Chelsea"]) == "sports_1x2"
    assert classify_market("Will BTC be above 100k by June 30?", outcomes=["Yes", "No"]) == "crypto_threshold"
    assert classify_market("Will temperature exceed 100F tomorrow?", outcomes=["Yes", "No"]) == "weather_threshold"
    assert classify_market("Will Alice win the presidential election?", outcomes=["Yes", "No"]) == "politics_election"
    assert classify_market("Will something happen?", outcomes=["Yes", "No"]) == "unknown"


def test_classifier_conservative_secondary_cases() -> None:
    assert classify_market("Arsenal vs Chelsea exact score 1-0", outcomes=["Yes", "No"]) == "sports_exact_score"
    assert classify_market("Arsenal vs Chelsea over 2.5 goals", outcomes=["Yes", "No"]) == "sports_total"
    assert classify_market("Will Arsenal reach the final?", outcomes=["Yes", "No"]) == "implication_market"
    assert classify_market("Which team wins Group A?", outcomes=["A", "B", "C"]) == "unknown"


def test_fixture_league_name_does_not_make_an_outright() -> None:
    assert classify_market("Premier League: Arsenal vs Chelsea 1X2 match winner") == "sports_1x2"
    assert classify_market("UFC: Pereira vs Hill moneyline") == "sports_moneyline"
    assert classify_market("Will the artist withdraw tomorrow?") == "unknown"
