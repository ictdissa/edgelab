from __future__ import annotations

from sharpedge.data.market_type import classify_market_type, market_types_compatible


def test_fixture_match_winner_is_not_outright() -> None:
    market_type = classify_market_type(
        "Premier League: Arsenal vs Chelsea 1X2 match winner",
        "Soccer",
        ["Arsenal", "Draw", "Chelsea"],
    )
    assert market_type == "1x2"


def test_polymarket_team_win_yes_no_fixture_is_1x2() -> None:
    market_type = classify_market_type(
        "Will Leeds United FC win on 2026-05-17?",
        "Soccer",
        ["Yes", "No"],
    )
    assert market_type == "1x2"


def test_world_cup_winner_is_outright() -> None:
    assert (
        classify_market_type(
            "Will Spain win the 2026 FIFA World Cup?",
            "Soccer",
            ["Yes", "No"],
        )
        == "outright"
    )


def test_fixture_odds_do_not_match_outright_or_top4() -> None:
    assert not market_types_compatible("1x2", "outright", odds_is_fixture=True)
    assert not market_types_compatible("1x2", "top_4", odds_is_fixture=True)
    assert market_types_compatible("ML", "1x2", odds_is_fixture=True)
