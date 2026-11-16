"""Feasibility must never become a synthetic performance claim or execute a trade."""

import json
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from edgelab.config.settings import Settings
from edgelab.data.cache import CachedHttpClient, RollingRateLimiter, atomic_json
from edgelab.data.schemas import ExternalAnchor, OrderBook, PredictionMarket, PriceLevel
from edgelab.research.pilot import (
    PilotContext,
    run_pilot,
    write_pilot_report,
    reparse_pilot,
    write_pilot_manifest,
)
from edgelab.research.study import StudyDesign, StudySnapshot
from edgelab.research.study_sources import AuditedClient
from edgelab.research.study_sources import FrozenBookProvider, LiveStudySource
from edgelab.data.polymarket_collector import PolymarketCollector
from edgelab.execution.paper_trader import PaperTrader
from edgelab.execution.live_trader import LiveTrader

NOW = datetime(2026, 10, 13, 15, tzinfo=UTC)


def design(tmp_path):
    return StudyDesign(
        league="Premier League",
        league_slug="england-premier-league",
        bookmakers=["TestBook"],
        cache_dir=tmp_path / "cache",
        shared_odds_budget_state=tmp_path / "budget.json",
    )


def settings():
    return Settings(_env_file=None, **{k: f.default for k, f in Settings.model_fields.items()})


def test_feasibility_cache_only_miss_never_calls_http(tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("network access in cache-only pilot")

    monkeypatch.setattr(CachedHttpClient, "_http_get", forbidden)
    monkeypatch.setattr(PaperTrader, "__init__", forbidden)
    monkeypatch.setattr(LiveTrader, "__init__", forbidden)
    summary = run_pilot(tmp_path / "run", design(tmp_path))
    assert summary["odds_network_attempts"] == 0
    assert summary["paper_trades"] == 0
    assert summary["clv"] is None and summary["net_markout"] is None
    assert summary["profitability_established"] is False
    assert "not proof that no fixtures exist" in (tmp_path / "run/report.md").read_text()
    assert not (tmp_path / "budget.json").exists()
    assert not (tmp_path / "run/portfolio.sqlite").exists()


def test_missing_credential_still_produces_failure_report(tmp_path, monkeypatch):
    monkeypatch.delenv("ODDS_API_IO_KEY", raising=False)
    monkeypatch.setattr("edgelab.research.study_sources.dotenv_values", lambda p: {})
    result = run_pilot(tmp_path / "run", design(tmp_path), refresh=True)
    assert result["odds_network_attempts"] == 0
    assert result["external_1x2_rows"] == 0
    assert "collection_failed:ValueError" in (tmp_path / "run/data_freshness.json").read_text()


def test_recorded_fixture_reuse_cannot_spend_discovery_request(tmp_path, monkeypatch):
    first = tmp_path / "first"
    run_pilot(first, design(tmp_path))

    def forbidden(*a, **kw):
        pytest.fail("recorded fixture reuse must not discover events")

    monkeypatch.setattr(FrozenBookProvider, "list_events", forbidden)
    result = run_pilot(tmp_path / "reuse", design(tmp_path), events_from=first)
    assert result["odds_network_attempts"] == 0
    assert result["event_discovery"]["status"] == "reused"
    (first / "snapshot.json").write_text("{}")
    with pytest.raises(ValueError, match="integrity"):
        run_pilot(tmp_path / "tampered", design(tmp_path), events_from=first)


@pytest.mark.parametrize("seconds", [-3600, 60])
def test_recorded_fixture_reuse_rejects_stale_or_future_observation(tmp_path, monkeypatch, seconds):
    observed = datetime.now(UTC) + timedelta(seconds=seconds)
    snapshot = StudySnapshot(started_at=observed, as_of=observed)
    monkeypatch.setattr(
        "edgelab.research.pilot.load_pilot_observation", lambda p: (snapshot, design(tmp_path), {})
    )
    with pytest.raises(ValueError, match="stale or unavailable"):
        run_pilot(tmp_path / "run", design(tmp_path), events_from=tmp_path / "recorded")
    assert not (tmp_path / "run").exists()


def test_odds_access_failure_does_not_erase_successful_fixture_discovery(tmp_path, monkeypatch):
    fixture = dict(
        id="odds1",
        home="Arsenal",
        away="Chelsea",
        date=(NOW + timedelta(days=1)).isoformat(),
        league={"slug": "england-premier-league", "name": "Premier League"},
    )
    monkeypatch.setattr(FrozenBookProvider, "list_events", lambda *a, **kw: [fixture])

    def denied(*a, **kw):
        raise RuntimeError("source entitlement denied")

    monkeypatch.setattr(FrozenBookProvider, "fetch_odds_payloads", denied)
    monkeypatch.setattr(PolymarketCollector, "fetch_fixture_markets", lambda *a, **kw: [])
    monkeypatch.setattr(PolymarketCollector, "fetch_orderbooks", lambda *a, **kw: [])
    context = PilotContext(design(tmp_path), settings(), NOW, NOW, lambda: NOW)
    snapshot = LiveStudySource(context).collect(None, NOW, tmp_path, True, True)
    assert snapshot.event_discovery.status == "complete" and snapshot.event_discovery.complete
    assert len(snapshot.odds_events) == 1 and snapshot.anchors == []
    assert snapshot.errors == ["odds_collection_failed:RuntimeError; prior vintage retained"]


def test_per_run_cap_counts_failed_attempts_and_redacts_exceptions(tmp_path):
    calls = []

    def failure(*a, **kw):
        calls.append(1)
        raise RuntimeError("DUMMY_PRIVATE_KEY")

    limiter = RollingRateLimiter(tmp_path / "budget.json", 90, 3600)
    original = CachedHttpClient(tmp_path / "cache", "odds_api_io", limiter, failure)
    audits = []
    client = AuditedClient(original, tmp_path, audits, False, "DUMMY_PRIVATE_KEY", 3)
    for _ in range(4):
        with pytest.raises(RuntimeError, match="odds_api_io request failed"):
            client.get_json("https://example.test/events", refresh=True)
    assert len(calls) == 3
    assert sum(r.get("network_attempt") is True for r in audits) == 3
    assert limiter.status().remaining == 87
    assert "DUMMY_PRIVATE_KEY" not in (tmp_path / "request_audit.json").read_text()


def test_cache_hits_do_not_consume_per_run_allowance(tmp_path):
    calls = []

    def response(*a, **kw):
        calls.append(1)
        return SimpleNamespace(status_code=200, headers={}, json=lambda: [])

    original = CachedHttpClient(
        tmp_path / "cache",
        "odds_api_io",
        RollingRateLimiter(tmp_path / "budget.json", 90, 3600),
        response,
    )
    audits = []
    client = AuditedClient(original, tmp_path, audits, False, network_request_limit=1)
    client.get_json("https://example.test/events", refresh=True)
    assert client.get_json("https://example.test/events").cache_hit
    assert len(calls) == 1


def test_pilot_never_overwrites_or_accepts_heldout_design(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "sentinel").write_text("preserve")
    with pytest.raises(FileExistsError):
        run_pilot(run, design(tmp_path))
    assert (run / "sentinel").read_text() == "preserve"
    too_many = design(tmp_path).model_copy(update={"max_event_pages": 3})
    with pytest.raises(ValueError, match="three requests"):
        run_pilot(tmp_path / "other", too_many)
    heldout = design(tmp_path).model_copy(update={"purpose": "held_out_evaluation"})
    with pytest.raises(ValueError, match="data-quality design"):
        run_pilot(tmp_path / "heldout", heldout)


@pytest.mark.parametrize("stale", [False, True])
def test_real_feasibility_avoids_even_large_apparent_edges(tmp_path, stale):
    kickoff = NOW + timedelta(days=1)
    timestamp = NOW - timedelta(hours=1) if stale else NOW
    market = PredictionMarket(
        market_id="m1",
        event_id="venue1",
        category="sports_1x2",
        title="Arsenal vs Chelsea match winner",
        outcomes=["Arsenal", "Draw", "Chelsea"],
        token_ids=["t1", "t2", "t3"],
        start_time=kickoff,
        resolution_source="TEST_RULES_NOT_REVIEWED",
        raw_metadata={"collected_at": NOW.isoformat(), "metadata_cache_verified": True},
    )
    anchors = [
        ExternalAnchor(
            source_id=outcome,
            event_id="odds1",
            category="sports_1x2",
            source="TestBook",
            outcome=outcome,
            decimal_odds=odds,
            timestamp=timestamp,
            available_at=NOW,
            metadata={
                "cache_verified": True,
                "home": "Arsenal",
                "away": "Chelsea",
                "start_time": kickoff.isoformat(),
                "provider": "odds_api_io",
            },
        )
        for outcome, odds in [("Arsenal", 2), ("Draw", 3.5), ("Chelsea", 4)]
    ]
    book = OrderBook(
        market_id="m1",
        token_id="t1",
        timestamp=NOW,
        available_at=NOW,
        bids=[PriceLevel(price=0.09, size=100)],
        asks=[PriceLevel(price=0.1, size=100)],
    )
    snapshot = StudySnapshot(
        started_at=NOW, as_of=NOW, markets=[market], anchors=anchors, books=[book]
    )
    context = PilotContext(design(tmp_path), settings(), NOW, NOW, lambda: NOW)
    summary = write_pilot_report(tmp_path, snapshot, context)
    assert summary["paper_trades"] == 0
    assert "AVOID" in (tmp_path / "signals.csv").read_text()
    assert "feasibility observation only" in (tmp_path / "signals.csv").read_text()
    if stale:
        assert summary["fresh_complete_fair_values"] == 0
        assert summary["freshness_exclusions"]["stale_odds"] == 3
    else:
        assert summary["fresh_complete_fair_values"] == 3
    assert json.loads((tmp_path / "data_freshness.json").read_text())["as_of"] == NOW.isoformat()


def test_offline_reparse_preserves_both_clocks_and_makes_zero_calls(tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("offline reparse must never call a provider or trader")

    monkeypatch.setattr(CachedHttpClient, "_http_get", forbidden)
    monkeypatch.setattr(PaperTrader, "__init__", forbidden)
    monkeypatch.setattr(LiveTrader, "__init__", forbidden)
    original = tmp_path / "original"
    original.mkdir()
    received = NOW + timedelta(seconds=1)
    payload = {
        "asset_id": "t1",
        "timestamp": str(int(NOW.timestamp() * 1000)),
        "bids": [{"price": "0.4", "size": "10"}],
        "asks": [{"price": "0.42", "size": "10"}],
    }
    atomic_json(
        original / "raw/book.json",
        {
            "cache_schema": 1,
            "http_status": 200,
            "fetched_at": received.isoformat(),
            "payload": payload,
            "payload_sha256": hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest(),
        },
    )
    snapshot = StudySnapshot(
        started_at=NOW,
        as_of=NOW + timedelta(seconds=5),
        books=[
            OrderBook(
                market_id="m1",
                token_id="t1",
                timestamp=datetime.fromtimestamp(0, UTC),
                available_at=received,
            )
        ],
        requests=[
            {
                "status": "ok",
                "endpoint": "https://example.test/book",
                "provider": "polymarket",
                "consumed_response": "raw/book.json",
            }
        ],
    )
    atomic_json(original / "snapshot.json", snapshot.model_dump(mode="json"))
    atomic_json(original / "study_design.json", design(tmp_path).model_dump(mode="json"))
    write_pilot_manifest(original, settings())
    output = tmp_path / "reparsed"
    summary = reparse_pilot(original, output)
    corrected = StudySnapshot.model_validate_json((output / "snapshot.json").read_text())
    assert corrected.as_of == snapshot.as_of
    assert corrected.books[0].timestamp == NOW
    assert corrected.books[0].available_at == received
    assert summary["odds_network_attempts"] == 0
    assert summary["offline_reparse"] is True
    assert "1970-01-01" in (original / "snapshot.json").read_text()
    (original / "raw/book.json").write_text("{}")
    with pytest.raises(ValueError, match="integrity"):
        reparse_pilot(original, tmp_path / "corrupt")
