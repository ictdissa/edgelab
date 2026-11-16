"""Additional accounting, cache and research-evidence regression checks."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from edgelab.config.settings import Settings
from edgelab.data.cache import CachedHttpClient, CachedHttpError, RollingRateLimiter
from edgelab.data.external_sources.weather_provider import WeatherForecastProvider
from edgelab.data.external_sources.mock_provider import MockProvider
from edgelab.research.backtest import replay, Settlement
from edgelab.data.schemas import PredictionMarket, ExternalAnchor, OrderBook, PriceLevel
from edgelab.fair_value.crypto_threshold_model import CryptoThresholdModel
from edgelab.fair_value.sports_odds_model import generic_no_vig
from scripts.analyze_clv_robustness import build_robustness_rows
from scripts.collect_research_snapshots import normalize_signal_row, SNAPSHOT_SIGNAL_COLUMNS
from scripts.run_paper_trader import read_signals
from edgelab.research.pipeline import write_signals_csv, MockResearchRun


def test_concurrent_request_reservations_enforce_one_budget(tmp_path):
    limiter = RollingRateLimiter(tmp_path / "limit.json", 90, 3600)
    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(lambda _: limiter.reserve(now=1000), range(120)))
    assert sum(d.allowed for d in decisions) == 90
    assert len(json.loads((tmp_path / "limit.json").read_text())["request_timestamps"]) == 90


def test_failed_response_is_cached_and_secret_redacted(tmp_path):
    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(status_code=401, text="credential=dummy-test-key", headers={})

    client = CachedHttpClient(
        tmp_path, "test", RollingRateLimiter(tmp_path / "limit.json", 3), fake
    )
    for _ in range(2):
        with pytest.raises(CachedHttpError) as error:
            client.get_json("https://example.test/data", {"apiKey": "dummy-test-key"})
        assert "dummy-test-key" not in str(error.value)
    assert len(calls) == 1
    assert all(
        "dummy-test-key" not in path.read_text() for path in (tmp_path / "test").rglob("*.json")
    )


def test_real_replay_requires_receipts_and_settles_only_when_observed():
    now = datetime(2026, 10, 13, tzinfo=UTC)
    settings = Settings(_env_file=None, MIN_LIQUIDITY=1, MIN_ADJUSTED_EDGE=0.001, MAX_TRADE_SIZE=10)
    m = PredictionMarket(
        market_id="m",
        event_id="e",
        category="crypto_threshold",
        title="BTC above $110 at expiry",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        end_time=now + timedelta(days=30),
        resolution_source="index",
        raw_metadata={"collected_at": now.isoformat()},
    )
    a = ExternalAnchor(
        source_id="spot",
        event_id="e",
        market_id="m",
        category="crypto_threshold",
        source="mock_spot",
        outcome="Yes",
        timestamp=now,
        metadata={"spot": 100, "threshold": 110, "volatility": 0.5},
    )
    b = OrderBook(
        market_id="m",
        token_id="yes",
        timestamp=now,
        bids=[PriceLevel(price=0.10, size=100)],
        asks=[PriceLevel(price=0.12, size=100)],
    )
    assert replay([m], [a], [b], settings, [now]).decisions == []
    a.available_at = now
    b.available_at = now
    result = replay(
        [m],
        [a],
        [b],
        settings,
        [now, now + timedelta(days=31)],
        [Settlement("yes", 1, now + timedelta(days=31))],
    )
    fill = next(d for d in result.decisions if d.approved)
    assert fill.price == 0.12
    assert result.equity[-1][1] == pytest.approx(
        settings.paper_starting_cash + 10 + fill.cash_change
    )
    assert result.equity[0][1] < settings.paper_starting_cash


def test_snapshot_and_signal_csv_retain_contract_provenance(tmp_path):
    run = MockResearchRun(Settings(_env_file=None))
    signal = run.signals()[0]
    normalized = normalize_signal_row(
        signal.model_dump(mode="json"), "test", signal.category, run.settings
    )
    for field in (
        "token_id",
        "fair_value_timestamp",
        "orderbook_timestamp",
        "uncertainty",
        "match_decision",
        "book_bid_size",
        "book_ask_size",
    ):
        assert field in SNAPSHOT_SIGNAL_COLUMNS and normalized[field] != ""
    path = tmp_path / "signals.csv"
    write_signals_csv(path, [signal])
    assert read_signals(path)[0].token_id == signal.token_id


def test_repeated_crypto_rows_are_one_block_not_independent_markets():
    rows = [
        {
            "clv": ".1",
            "side": "BUY_YES",
            "horizon": "5m",
            "category": "crypto_threshold",
            "market_id": str(i),
            "paper_trade_id": str(i),
            "entry_timestamp": "2026-10-13T12:00:00Z",
        }
        for i in range(100)
    ]
    overall = next(r for r in build_robustness_rows(rows) if r["group_type"] == "overall")
    assert overall["independent_blocks"] == "1"
    assert overall["verdict"] == "INSUFFICIENT_SAMPLE"


def test_weather_adapter_fails_closed_without_requests():
    def forbidden(*args, **kwargs):
        raise AssertionError("uncalibrated weather must not fetch or price")

    assert (
        WeatherForecastProvider(Settings(_env_file=None), forbidden).fetch_anchors_for_markets(
            MockProvider().markets()
        )
        == []
    )


def test_bad_or_unverified_anchor_cannot_be_priced():
    provider = MockProvider()
    m = next(m for m in provider.markets() if m.category == "crypto_threshold")
    a = next(a for a in provider.anchors() if a.category == "crypto_threshold")
    model = CryptoThresholdModel(Settings(_env_file=None), a.timestamp)
    malformed = a.model_copy(update={"metadata": {**a.metadata, "expiry": "invalid"}})
    unverified = a.model_copy(update={"metadata": {**a.metadata, "cache_verified": False}})
    assert model.compute([m], [malformed], []) == []
    assert model.compute([m], [unverified], []) == []


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), 1.0])
def test_no_vig_rejects_nonfinite_and_invalid_odds(invalid):
    with pytest.raises(ValueError):
        generic_no_vig({"a": invalid, "b": 2.0})


def test_clv_round_trip_preserves_new_ledger_cost_and_identity_fields(tmp_path):
    from scripts.run_paper_trader import LEDGER_COLUMNS, write_csv
    from scripts.update_clv import read_ledger_rows, write_ledger_rows, update_ledger_clv

    row = {field: "" for field in LEDGER_COLUMNS}
    row.update(
        paper_trade_id="t",
        timestamp="2026-10-13T00:00:00Z",
        market_id="m",
        event_id="e",
        token_id="yes",
        outcome="Yes",
        side="BUY_YES",
        execution_price=".4",
        size="10",
        fees=".1",
        slippage=".2",
        clv_status="pending",
    )
    path = tmp_path / "ledger.csv"
    write_csv(path, [row], LEDGER_COLUMNS)
    updated, _ = update_ledger_clv(read_ledger_rows(path), [])
    write_ledger_rows(path, updated)
    saved = read_ledger_rows(path)[0]
    assert saved["fees"] == ".1" and saved["token_id"] == "yes" and saved["event_id"] == "e"


def test_live_failure_invalidates_signals_and_writes_freshness(tmp_path, monkeypatch):
    from edgelab.research.live_analysis import write_live_failure_report

    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, ODDS_API_IO_KEY="dummy-test-key")
    output = tmp_path / "report.md"
    freshness = write_live_failure_report(
        "sports", output, settings, RuntimeError("failed dummy-test-key")
    )
    assert "AVOID" in output.read_text()
    assert "dummy-test-key" not in output.read_text()
    assert "collection_failure" in freshness.read_text()
    assert read_signals(tmp_path / "reports/latest_signals.csv") == []


def test_sports_discovery_queries_only_external_participant_pairs(tmp_path):
    from edgelab.data.polymarket_collector import PolymarketCollector

    calls = []

    def fake(url, **kwargs):
        assert url.endswith("/public-search")
        calls.append(kwargs["params"]["q"])
        return SimpleNamespace(status_code=200, json=lambda: {"events": [], "markets": []})

    anchors = [
        ExternalAnchor(
            source_id=o,
            event_id="fixture",
            source="book",
            category="sports_1x2",
            outcome=o,
            decimal_odds=2 + i,
            metadata={"home": "Arsenal", "away": "Chelsea"},
        )
        for i, o in enumerate(("Arsenal", "Draw", "Chelsea"))
    ]
    collector = PolymarketCollector(
        Settings(
            _env_file=None,
            EDGELAB_CACHE_DIR=tmp_path / "cache",
            EDGELAB_LIVE_DATA_DIR=tmp_path / "live",
        ),
        mock=False,
        http_get=fake,
    )
    assert collector.fetch_fixture_markets(anchors) == []
    assert calls == ["Arsenal vs Chelsea", "Chelsea vs Arsenal"]
    collector.fetch_fixture_markets(anchors)
    assert len(calls) == 2
    assert (tmp_path / "live/sports_discovery_queries.json").exists()


def test_odds_payload_keeps_kickoff_from_event_discovery(monkeypatch):
    from edgelab.data.external_sources.odds_api_io_provider import OddsAPIIOProvider

    provider = OddsAPIIOProvider(Settings(_env_file=None, ODDS_API_IO_KEY="dummy-test-key"))
    monkeypatch.setattr(
        provider,
        "list_events",
        lambda *a, **k: [
            {"id": 1, "home": "Arsenal", "away": "Chelsea", "date": "2026-10-14T19:00:00Z"}
        ],
    )
    monkeypatch.setattr(
        provider, "fetch_odds_payloads", lambda *a, **k: [{"id": 1, "bookmakers": {}}]
    )
    captured = []

    def normalize(payloads):
        captured.extend(payloads)
        return []

    monkeypatch.setattr(provider, "normalize_to_external_odds", normalize)
    provider.fetch_odds("football")
    assert captured[0]["date"] == "2026-10-14T19:00:00Z"
    assert captured[0]["home"] == "Arsenal"


def test_malformed_json_is_cached_without_repeat_requests(tmp_path):
    from edgelab.data.cache import CachedJSONError

    calls = []

    def invalid():
        raise ValueError("not JSON")

    def fake(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(status_code=200, text="bad response", json=invalid)

    client = CachedHttpClient(
        tmp_path, "test", RollingRateLimiter(tmp_path / "budget.json", 3), fake
    )
    for _ in range(2):
        with pytest.raises(CachedJSONError):
            client.get_json("https://example.test/data")
    assert len(calls) == 1
