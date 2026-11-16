from __future__ import annotations

from scripts.run_research_experiment import experiment_plan


class Args:
    experiment_id = "unit_experiment"
    categories = "crypto,relative_value"
    iterations = 3
    interval_seconds = 1
    refresh_live = False


def test_research_experiment_plan_keeps_live_trading_disabled(tmp_path) -> None:
    plan = experiment_plan(Args(), tmp_path / "unit_experiment")

    assert plan["live_trading_enabled"] is False
    assert plan["experiment_id"] == "unit_experiment"
    assert any("scripts/run_snapshot_campaign.py" in command for command in plan["commands"][0])
