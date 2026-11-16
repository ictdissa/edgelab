from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from edgelab.config.settings import Settings
from edgelab.data.external_sources.odds_provider import ProviderDiagnosis
from edgelab.data.schemas import PredictionMarket
from edgelab.research import live_analysis


class FailedProvider:
    def diagnose(self, sport: str, limit: int = 3) -> ProviderDiagnosis:
        return ProviderDiagnosis(
            selected_provider="the_odds_api",
            base_url="https://api.the-odds-api.com/v4",
            key_present=True,
            key_fingerprint="bad...key",
            key_source="THE_ODDS_API_KEY",
            endpoint_path=f"/sports/{sport}/odds",
            http_status=401,
            provider_error_body='{"error_code":"INVALID_KEY"}',
            auth_succeeded=False,
            sport_key_valid=None,
            odds_or_events_returned=False,
            returned_count=0,
            failure_reason="provider rejected key",
            recommended_next_action="rotate key",
        )

    def fetch_odds(self, *args, **kwargs):
        raise AssertionError("failed diagnosis must not fetch odds")


class FakeCollector:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def fetch_markets(self, category: str, limit: int, refresh: bool):
        return [
            PredictionMarket(
                market_id="m1",
                event_id="pm1",
                category="sports_1x2",
                title="Will Arsenal beat Chelsea?",
                outcomes=["Yes", "No"],
                token_ids=["yes", "no"],
                end_time=datetime.now(UTC),
                resolution_source="official",
            )
        ]

    def fetch_orderbooks(self, *args, **kwargs):
        raise AssertionError("no CLOB books are needed after failed provider auth")


def test_failed_provider_auth_outputs_avoid_signals_not_trades(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(live_analysis, "select_sports_odds_provider", lambda *a, **k: FailedProvider())
    monkeypatch.setattr(live_analysis, "PolymarketCollector", FakeCollector)

    settings = Settings(
        ODDS_PROVIDER="the_odds_api",
        THE_ODDS_API_KEY="bad-key",
        EDGELAB_CACHE_DIR=tmp_path / "cache",
        EDGELAB_LIVE_DATA_DIR=tmp_path / "live",
    )
    live_analysis.run_live_research(
        category="sports",
        output=tmp_path / "reports/live_sports_report.md",
        settings=settings,
    )

    with Path("reports/latest_signals.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == []
    assert "provider rejected key" in (tmp_path/"reports/live_sports_report.md").read_text()
