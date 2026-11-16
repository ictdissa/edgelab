from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

from edgelab.config.settings import Settings
from edgelab.data.external_sources.odds_provider import ProviderDiagnosis
from edgelab.data.schemas import ExternalAnchor, OrderBook, PredictionMarket, PriceLevel
from edgelab.research import live_analysis


def reviewed_spec():
    return dict(
        review_status="reviewed",
        period="regulation_time",
        includes_overtime=False,
        draw_is_outcome=True,
        cancellation_rule="test-only-identical-rules",
        evidence_reference="synthetic unit-test rules",
        reviewed_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
    )


class PassingProvider:
    def diagnose(self, sport: str, limit: int = 3) -> ProviderDiagnosis:
        return ProviderDiagnosis(
            selected_provider="odds_api_io",
            base_url="https://api.odds-api.io/v3",
            key_present=True,
            key_fingerprint="test...key",
            key_source="ODDS_API_IO_KEY",
            endpoint_path="/events",
            http_status=200,
            provider_error_body=None,
            auth_succeeded=True,
            sport_key_valid=True,
            odds_or_events_returned=True,
            returned_count=1,
            failure_reason=None,
            recommended_next_action="continue",
        )

    def fetch_odds(self, *args, **kwargs):
        now = datetime.now(UTC)
        rows = [
            ExternalAnchor(
                source_id="book:e1:ars",
                event_id="ext1",
                source="BookA",
                category="sports_1x2",
                outcome="Arsenal",
                decimal_odds=2.0,
                timestamp=now,
                metadata={
                    "provider": "odds_api_io",
                    "event_title": "Arsenal vs Chelsea",
                    "start_time": (now + timedelta(hours=2)).isoformat(),
                },
            ),
            ExternalAnchor(
                source_id="book:e1:che",
                event_id="ext1",
                source="BookA",
                category="sports_1x2",
                outcome="Chelsea",
                decimal_odds=2.0,
                timestamp=now,
                metadata={
                    "provider": "odds_api_io",
                    "event_title": "Arsenal vs Chelsea",
                    "start_time": (now + timedelta(hours=2)).isoformat(),
                },
            ),
            ExternalAnchor(
                source_id="draw",
                event_id="ext1",
                source="BookA",
                category="sports_1x2",
                outcome="Draw",
                decimal_odds=10.0,
                timestamp=now,
                metadata={"provider": "odds_api_io"},
            ),
        ]
        for row in rows:
            row.metadata["settlement_spec"] = reviewed_spec()
        return rows


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
                end_time=datetime.now(UTC) + timedelta(days=1),
                start_time=datetime.now(UTC) + timedelta(hours=2),
                resolution_source="official result",
                raw_metadata={"settlement_spec": reviewed_spec()},
            )
        ]

    def fetch_fixture_markets(self, anchors, limit, refresh):
        assert {a.outcome for a in anchors} == {"Arsenal", "Draw", "Chelsea"}
        return self.fetch_markets("sports", limit, refresh)

    def fetch_orderbooks(self, markets, limit: int, refresh: bool):
        return [
            OrderBook(
                market_id="m1",
                token_id="yes",
                timestamp=datetime.now(UTC),
                bids=[PriceLevel(price=0.37, size=100)],
                asks=[PriceLevel(price=0.38, size=100)],
            )
        ]


def test_fake_odds_and_polymarket_books_produce_fair_values_and_signals(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        live_analysis, "select_sports_odds_provider", lambda *a, **k: PassingProvider()
    )
    monkeypatch.setattr(live_analysis, "PolymarketCollector", FakeCollector)

    settings = Settings(
        ODDS_PROVIDER="odds_api_io",
        ODDS_API_IO_KEY="test-key",
        EDGELAB_CACHE_DIR=tmp_path / "cache",
        EDGELAB_LIVE_DATA_DIR=tmp_path / "live",
        MIN_LIQUIDITY=1,
        MIN_ADJUSTED_EDGE=0.01,
    )
    live_analysis.run_live_research(
        category="sports",
        output=tmp_path / "reports/live_sports_report.md",
        settings=settings,
    )

    with Path("reports/latest_signals.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["action"] == "BUY_YES"
    assert abs(float(rows[0]["fair_probability"]) - 5 / 11) < 1e-9
    assert rows[0]["fair_value_provider"] == "odds_api_io"
    assert rows[0]["risk_approved"] == "True"
