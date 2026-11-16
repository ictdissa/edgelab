from __future__ import annotations

from edgelab.matching.outcome_mapper import binary_yes_target_from_title, map_outcomes


def test_outcome_mapper_exact_and_binary() -> None:
    assert map_outcomes(["Arsenal", "Draw", "Chelsea"], ["Arsenal", "Chelsea"]) == {
        "Arsenal": "Arsenal",
        "Chelsea": "Chelsea",
    }
    assert binary_yes_target_from_title("Will Arsenal win against Chelsea?", "Arsenal", "Chelsea") == "Arsenal"
    assert binary_yes_target_from_title("Will Arsenal vs Chelsea end in a draw?", "Arsenal", "Chelsea") == "Draw"
