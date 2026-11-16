"""Prospective timing, immutable inputs, crash recovery and honest inference."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from edgelab.config.settings import Settings
from edgelab.data.cache import CacheMissError, CachedHttpClient, RollingRateLimiter
from edgelab.execution.paper_trader import PaperTrader
from edgelab.research.experiment import ROOT, ResearchProtocol, verify_run
from edgelab.research.study import StudyDesign, StudyRunner, register_study
from edgelab.research.study_report import followups, primary_statistics
from edgelab.research.study_sources import FrozenBookProvider, LiveStudySource, SyntheticStudySource

START = datetime(2026, 10, 13, 12, tzinfo=UTC)


def defaults():
    return Settings(_env_file=None, **{k: f.default for k, f in Settings.model_fields.items()})


def setup_study(
    path,
    provider="synthetic",
    now=START,
    start=START,
    design_overrides=None,
    settings=None,
    additional_market_ids=(),
):
    protocol = ResearchProtocol.model_validate_json(
        (ROOT / "research/protocol_soccer_1x2.json").read_text()
    ).model_copy(
        update={
            "protocol_id": "study-test",
            "leagues": ["Synthetic Premier League"],
            "bookmaker_sources": ["SyntheticBook"],
            "evaluation_start": start,
            "evaluation_end": start + timedelta(minutes=10),
        }
    )
    design = StudyDesign(
        provider=provider,
        league=protocol.leagues[0],
        league_slug="synthetic-premier-league",
        bookmakers=protocol.bookmaker_sources,
        cache_dir=path / "cache",
        shared_odds_budget_state=path / "budget.json",
        **(design_overrides or {}),
    )
    spec = dict(
        review_status="reviewed",
        period="regulation_time",
        includes_overtime=False,
        draw_is_outcome=True,
        cancellation_rule="synthetic-identical",
        evidence_reference="UNIT TEST ONLY",
        reviewed_at=(start - timedelta(seconds=1)).isoformat(),
    )
    registry = [
        dict(
            market_id="synthetic-ars-che",
            external_event_id="synthetic-odds",
            book="SyntheticBook",
            prediction=spec,
            external=spec,
        )
    ]
    registry.extend({**registry[0], "market_id": market_id} for market_id in additional_market_ids)
    register_study(
        path, protocol, design, settings or defaults(), registry, start - timedelta(seconds=1)
    )
    clock = SimpleNamespace(now=now)
    runner = StudyRunner(path, clock=lambda: clock.now)
    return runner, SyntheticStudySource(runner) if provider == "synthetic" else None, clock


def test_cache_only_miss_spends_zero_requests(tmp_path):
    calls = []
    limiter = RollingRateLimiter(tmp_path / "budget.json", 90, 3600)
    client = CachedHttpClient(
        tmp_path, "fixture", limiter, http_get=lambda *a, **kw: calls.append(a), cache_only=True
    )
    with pytest.raises(CacheMissError):
        client.get_json("https://example.test/data")
    with pytest.raises(ValueError, match="cannot refresh"):
        client.get_json("https://example.test/data", refresh=True)
    assert calls == [] and not limiter.state_path.exists()


def test_registration_freezes_environment_and_provider_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("FEE_BPS", "9999")
    monkeypatch.setenv("ODDS_API_IO_KEY", "DUMMY_SECRET")
    monkeypatch.setenv("ODDS_API_IO_BASE", "https://wrong.example")
    runner, _, _ = setup_study(tmp_path)
    assert runner.settings.fee_bps == 20
    assert runner.settings.odds_api_io_base == "https://api.odds-api.io/v3"
    assert runner.settings.odds_api_io_key is None
    assert runner.settings.odds_api_io_max_requests_per_hour == 90
    assert "DUMMY_SECRET" not in (tmp_path / "manifest.json").read_text()
    assert verify_run(tmp_path)["evidence_type"] == "synthetic_integration"


def test_future_boundary_and_immutable_registration(tmp_path):
    runner, _, _ = setup_study(tmp_path / "original")
    with pytest.raises(ValueError, match="after registration"):
        register_study(
            tmp_path / "past",
            runner.protocol,
            runner.design.model_copy(update={"provider": "odds_api_io"}),
            defaults(),
            [],
            START,
        )
    assert not (tmp_path / "past").exists()
    with pytest.raises(FileExistsError):
        register_study(tmp_path / "original", runner.protocol, runner.design, defaults(), [])
    with pytest.raises(ValueError, match="exactly equal"):
        register_study(
            tmp_path / "different",
            runner.protocol,
            runner.design.model_copy(update={"bookmakers": ["OtherBook"]}),
            defaults(),
            [],
        )


def test_wait_before_start_and_no_duplicate_cycle(tmp_path):
    runner, source, clock = setup_study(tmp_path, now=START - timedelta(seconds=1))
    assert runner.run_once(source) == "waiting_for_start"
    assert runner.history() == []
    assert (tmp_path / "data_freshness.md").exists()
    clock.now = START
    assert runner.run_once(source) == "collected"
    before = (tmp_path / "ledger.csv").read_text()
    assert runner.run_once(source) == "not_due"
    assert (tmp_path / "ledger.csv").read_text() == before
    assert len(runner.history()) == 1


def test_entry_cutoff_missed_slots_and_final_boundary(tmp_path):
    runner, source, clock = setup_study(tmp_path)
    runner.run_once(source)
    clock.now = runner.end
    runner.run_once(source)
    payload, result = runner.history()[-1]
    assert payload["missed_slots_before"] == 9
    assert all(not d["approved"] for d in result["decisions"])
    assert all(s["action"] == "AVOID" for s in result["signals"])
    assert "INTERIM" in (tmp_path / "report.md").read_text()
    clock.now = runner.final_at + timedelta(seconds=1)
    assert runner.run_once(source) == "complete"
    assert len(runner.history()) == 2
    assert "FINAL FIXED-WINDOW" in (tmp_path / "report.md").read_text()


def test_interrupted_execution_recovers_without_duplicate_fills(tmp_path, monkeypatch):
    import edgelab.research.study as module

    runner, source, _ = setup_study(tmp_path)
    original = module.seal_payload

    def crash_result(path, payload):
        if path.name == "result.json":
            raise RuntimeError("simulated crash after accounting commit")
        return original(path, payload)

    monkeypatch.setattr(module, "seal_payload", crash_result)
    with pytest.raises(RuntimeError, match="simulated crash"):
        runner.run_once(source)
    trader = PaperTrader(runner.settings, portfolio_path=tmp_path / "portfolio.sqlite")
    before = [d.model_dump() for d in trader.portfolio.decisions()]
    trader.portfolio.connection.close()
    assert any(d["approved"] for d in before)
    monkeypatch.setattr(module, "seal_payload", original)
    assert runner.run_once(source) == "recovered"
    trader = PaperTrader(runner.settings, portfolio_path=tmp_path / "portfolio.sqlite")
    assert [d.model_dump() for d in trader.portfolio.decisions()] == before
    trader.portfolio.connection.close()
    assert len(runner.history()) == 1


def test_input_and_design_tampering_block_resume(tmp_path):
    runner, source, _ = setup_study(tmp_path / "inputs")
    runner.run_once(source)
    path = tmp_path / "inputs/cycles/000000/snapshot.json"
    data = json.loads(path.read_text())
    data["payload"]["snapshot"]["anchors"][0]["decimal_odds"] = 99
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="integrity"):
        runner.run_once(source)
    runner, source, _ = setup_study(tmp_path / "design")
    (tmp_path / "design/study_design.json").write_text("{}")
    with pytest.raises(ValueError, match="artifact changed"):
        runner.run_once(source)


def test_clock_rollback_and_missing_accounting_fail_closed(tmp_path):
    runner, source, clock = setup_study(tmp_path)
    runner.run_once(source)
    clock.now -= timedelta(seconds=1)
    with pytest.raises(ValueError, match="clock moved backward"):
        runner.run_once(source)
    clock.now = START + timedelta(minutes=1)
    (tmp_path / "portfolio.sqlite").unlink()
    with pytest.raises(ValueError, match="portfolio state missing"):
        runner.run_once(source)


@pytest.mark.parametrize(
    "failure",
    [
        "future_anchor",
        "future_book",
        "other_book",
        "other_league",
        "future_metadata",
        "invalid_metadata",
        "unverified_metadata",
    ],
)
def test_future_and_out_of_scope_inputs_never_fill(tmp_path, failure):
    runner, source, _ = setup_study(tmp_path)
    original = source.collect

    class BadSource:
        def collect(self, *args):
            snapshot = original(*args)
            if failure == "future_anchor":
                for anchor in snapshot.anchors:
                    anchor.available_at = START + timedelta(seconds=1)
            elif failure == "future_book":
                for book in snapshot.books:
                    book.available_at = START + timedelta(seconds=1)
            elif failure == "other_book":
                for anchor in snapshot.anchors:
                    anchor.source = "UnregisteredBook"
            elif failure == "other_league":
                for anchor in snapshot.anchors:
                    anchor.metadata["league"] = "OtherLeague"
            elif failure == "invalid_metadata":
                snapshot.markets[0].raw_metadata["collected_at"] = "invalid-date"
            elif failure == "unverified_metadata":
                snapshot.markets[0].raw_metadata["metadata_cache_verified"] = False
            else:
                for market in snapshot.markets:
                    market.raw_metadata["collected_at"] = (START + timedelta(seconds=1)).isoformat()
            return snapshot

    runner.run_once(BadSource())
    assert not any(d["approved"] for d in runner.history()[0][1]["decisions"])


def test_collection_failure_is_recorded_without_secret_text(tmp_path):
    runner, _, _ = setup_study(tmp_path)

    class BrokenSource:
        def collect(self, *args):
            raise RuntimeError("https://example.test?apiKey=SHOULD_NOT_APPEAR")

    runner.run_once(BrokenSource())
    payload, _ = runner.history()[0]
    assert payload["snapshot"]["errors"] == ["collection_failed:RuntimeError"]
    assert "SHOULD_NOT_APPEAR" not in (tmp_path / "cycles/000000/snapshot.json").read_text()
    assert "No actionable edge found" in (tmp_path / "report.md").read_text()


def test_followup_exact_token_pending_depth_and_frozen_costs(tmp_path):
    runner, source, clock = setup_study(tmp_path)
    runner.run_once(source)
    first = runner.history()[0][1]
    from edgelab.data.schemas import TradeDecision

    trades = [TradeDecision.model_validate(d) for d in first["decisions"]]
    rows = followups(runner.history(), trades, START, 30)
    assert rows and all(r["status"] == "pending" for r in rows)
    clock.now += timedelta(minutes=5)
    runner.run_once(source)
    rows = followups(runner.history(), trades, clock.now, 30)
    row = next(r for r in rows if r["horizon"] == "5m")
    trade = next(t for t in trades if t.approved)
    assert row["status"] == "observed"
    assert row["exit_price"] == 0.32  # bid, not ask or midpoint
    assert row["external_fair_clv"] == pytest.approx(0)
    assert row["net_quote_markout"] == pytest.approx(
        0.32
        - trade.price
        - (trade.fees + trade.slippage) / trade.size
        - 0.32 * trade.fee_bps_at_entry / 10000
        - trade.slippage_bps_at_entry / 10000
    )
    changed = runner.history()
    changed[-1][0]["snapshot"]["books"][0]["token_id"] = "WRONG_TOKEN"
    assert (
        next(
            r
            for r in followups(changed, trades, clock.now + timedelta(seconds=61), 30)
            if r["horizon"] == "5m"
        )["status"]
        == "missing_followup"
    )
    changed = runner.history()
    changed[-1][0]["snapshot"]["books"][0]["bids"][0]["size"] = 1
    row = next(r for r in followups(changed, trades, clock.now, 30) if r["horizon"] == "5m")
    assert row["status"] == "insufficient_depth"
    assert primary_statistics([row], 0.05)["primary_equal_weight_event_mean"] is None


def test_event_weighting_not_pseudoreplicated_snapshot_weighting():
    rows = [dict(event_id="A", horizon="5m", status="observed", net_quote_markout=0.1)] * 20
    rows += [dict(event_id="B", horizon="5m", status="observed", net_quote_markout=-0.1)]
    stats = primary_statistics(rows, 0.05)
    assert stats["primary_equal_weight_event_mean"] == pytest.approx(0)
    assert stats["independent_event_blocks_observed"] == 2
    assert stats["event_bootstrap_ci"] is None


def test_frozen_books_never_fallback_or_make_catalog_requests(tmp_path):
    calls = []

    def denied(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=403, text="allowed: OtherBook", headers={})

    settings = defaults().model_copy(
        update={
            "odds_api_io_key": "DUMMY",
            "cache_dir": tmp_path,
            "odds_api_io_rate_limit_state": tmp_path / "budget.json",
        }
    )
    provider = FrozenBookProvider(settings, http_get=denied)
    with pytest.raises(RuntimeError):
        provider.fetch_odds_payloads(["1"], refresh=True)
    assert len(calls) == 1 and calls[0].endswith("/odds")
    assert len(json.loads((tmp_path / "budget.json").read_text())["request_timestamps"]) == 1


def test_polling_plan_retains_hard_cap_headroom():
    with pytest.raises(ValueError, match="headroom"):
        StudyDesign(league="EPL", bookmakers=["Book"], max_events=30, odds_refresh_seconds=60)


def test_live_adapter_cache_miss_is_offline_and_reported(tmp_path, monkeypatch):
    runner, _, _ = setup_study(tmp_path, provider="odds_api_io")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ODDS_API_IO_KEY", raising=False)
    calls = []
    monkeypatch.setattr(CachedHttpClient, "_http_get", lambda *a, **k: calls.append(a))
    runner.run_once(LiveStudySource(runner))
    payload, result = runner.history()[0]
    assert calls == []
    assert payload["snapshot"]["requests"][0]["error_type"] == "CacheMissError"
    assert result["decisions"] == []
    assert "No actionable edge found" in (tmp_path / "report.md").read_text()
    assert (tmp_path / "data_freshness.csv").exists()
    assert not runner.design.shared_odds_budget_state.exists()


def test_real_adapters_with_fake_transport_then_cached_replay(tmp_path, monkeypatch):
    start = datetime.now(UTC) - timedelta(seconds=1)
    runner, _, clock = setup_study(tmp_path, provider="odds_api_io", start=start, now=start)
    runner.clock = lambda: datetime.now(UTC)
    key = "UNIT_TEST_SECRET_MUST_NOT_BE_ARCHIVED"
    monkeypatch.setenv("ODDS_API_IO_KEY", key)
    calls = []
    kickoff = start + timedelta(hours=3)

    def fake_http(self, url, **kwargs):
        calls.append((url, kwargs.get("params")))
        now = datetime.now(UTC).isoformat()
        if url.endswith("/events"):
            payload = [
                dict(
                    id="synthetic-odds",
                    home="Arsenal",
                    away="Chelsea",
                    sport="football",
                    league="Synthetic Premier League",
                    date=kickoff.isoformat(),
                )
            ]
        elif url.endswith("/odds"):
            payload = dict(
                id="synthetic-odds",
                bookmakers={
                    "SyntheticBook": [
                        dict(name="ML", updatedAt=now, odds=[dict(home=2.0, draw=3.5, away=4.0)])
                    ]
                },
            )
        elif url.endswith("/public-search"):
            payload = {
                "markets": [
                    dict(
                        conditionId="synthetic-ars-che",
                        eventId="synthetic-polymarket",
                        question="Arsenal vs Chelsea match winner",
                        outcomes=["Arsenal", "Draw", "Chelsea"],
                        clobTokenIds=["synthetic-ars", "synthetic-draw", "synthetic-che"],
                        gameStartTime=kickoff.isoformat(),
                        endDate=(kickoff + timedelta(hours=2)).isoformat(),
                        resolutionSource="UNIT TEST",
                        active=True,
                    )
                ]
            }
        elif url.endswith("/book"):
            payload = dict(
                timestamp=now, bids=[dict(price=0.29, size=100)], asks=[dict(price=0.31, size=100)]
            )
        else:
            pytest.fail(f"unexpected request {url}")
        return SimpleNamespace(status_code=200, headers={}, json=lambda: payload)

    monkeypatch.setattr(CachedHttpClient, "_http_get", fake_http)
    assert runner.run_once(LiveStudySource(runner, refresh=True)) == "collected"
    payload, result = runner.history()[0]
    assert any(d["approved"] for d in result["decisions"])
    assert len(calls) == 7
    query = calls[0][1]
    assert query["league"] == "synthetic-premier-league"
    assert query["status"] == "pending"
    assert query["limit"] == "50"
    assert datetime.fromisoformat(query["from"]) == runner.start
    assert datetime.fromisoformat(query["to"]) == runner.end + timedelta(hours=72)
    assert len(payload["raw_hashes"]) == 7
    for path in (tmp_path / "cycles/000000").rglob("*.json"):
        assert key not in path.read_text()
    assert [r["remaining_local_budget"] for r in payload["snapshot"]["requests"][:2]] == [89, 88]
    assert not any("bookmakers" in url for url, _ in calls)
    # Reuse the same response cache in a later study cycle without refreshing anything.
    runner.clock = lambda: clock.now
    clock.now = start + timedelta(minutes=2)
    assert runner.run_once(LiveStudySource(runner)) == "collected"
    assert len(calls) == 7
    cached = runner.history()[-1][0]["snapshot"]["requests"]
    assert cached and all(r.get("cache_hit") for r in cached)
    assert all(not d["approved"] for d in runner.history()[-1][1]["decisions"])
    assert "stale" in (tmp_path / "cycles/000001/signals.csv").read_text()


def test_conflicting_followup_quotes_are_not_selected_opportunistically(tmp_path):
    from edgelab.data.schemas import TradeDecision

    runner, source, clock = setup_study(tmp_path)
    runner.run_once(source)
    trades = [TradeDecision.model_validate(d) for d in runner.history()[0][1]["decisions"]]
    clock.now += timedelta(minutes=5)
    runner.run_once(source)
    history = runner.history()
    books = history[-1][0]["snapshot"]["books"]
    conflicting = json.loads(json.dumps(books[0]))
    conflicting["bids"][0]["price"] = 0.9
    conflicting["asks"][0]["price"] = 0.91
    books.append(conflicting)
    assert (
        next(r for r in followups(history, trades, clock.now, 30) if r["horizon"] == "5m")["status"]
        == "ambiguous_followup_quotes"
    )


def test_heldout_requires_pilot_plan_and_reviewed_pairs(tmp_path):
    with pytest.raises(ValueError, match="pilot variance"):
        StudyDesign(league="EPL", bookmakers=["Book"], purpose="held_out_evaluation")
    runner, _, _ = setup_study(tmp_path / "pilot")
    heldout = runner.design.model_copy(
        update={"purpose": "held_out_evaluation", "pilot_event_std": 0.02}
    )
    with pytest.raises(ValueError, match="reviewed settlement pairs"):
        register_study(
            tmp_path / "heldout",
            runner.protocol,
            heldout,
            defaults(),
            [],
            START - timedelta(seconds=1),
        )


def test_per_cycle_csv_tampering_is_detected(tmp_path):
    runner, source, _ = setup_study(tmp_path)
    runner.run_once(source)
    (tmp_path / "cycles/000000/signals.csv").write_text("changed")
    with pytest.raises(ValueError, match="CSV differs"):
        runner.run_once(source)


def test_budget_status_never_reserves_and_corruption_blocks(tmp_path):
    path = tmp_path / "budget.json"
    limiter = RollingRateLimiter(path, 90, 3600)
    assert limiter.status(now=100).remaining == 90
    assert not path.exists()
    limiter.reserve(now=100)
    before = path.read_text()
    assert limiter.status(now=100).remaining == 89
    assert path.read_text() == before
    path.write_text("invalid")
    with pytest.raises(RuntimeError, match="Corrupt"):
        limiter.reserve(now=101)


def test_missingness_bounds_do_not_hide_an_unobserved_event():
    rows = [
        dict(event_id="A", horizon="5m", status="observed", net_quote_markout=0.1),
        dict(
            event_id="B",
            horizon="5m",
            status="missing_followup",
            net_quote_markout=None,
            missing_net_lower_bound=-0.5,
            missing_net_upper_bound=0.5,
        ),
    ]
    stats = primary_statistics(rows, 0.05)
    assert stats["primary_equal_weight_event_mean"] == pytest.approx(0.1)
    assert stats["missingness_quote_model_bounds"] == pytest.approx([-0.2, 0.3])


def test_hydrated_metadata_uses_later_receipt_and_verification(tmp_path):
    from edgelab.data.cache import CachedResponse
    from edgelab.data.polymarket_collector import PolymarketCollector
    from edgelab.data.schemas import ExternalAnchor

    collector = PolymarketCollector(
        defaults().model_copy(update={"live_data_dir": tmp_path}), mock=False
    )
    full = dict(
        conditionId="m",
        question="Arsenal vs Chelsea match winner",
        outcomes=["Arsenal", "Draw", "Chelsea"],
        clobTokenIds=["a", "d", "c"],
    )

    def get_json(url, **kwargs):
        if "/events/slug/" in url:
            return CachedResponse(
                {"id": "pm", "markets": [full]},
                True,
                tmp_path / "hydration",
                fetched_at=START + timedelta(seconds=30),
                verified=True,
            )
        return CachedResponse(
            {"events": [{"id": "pm", "slug": "fixture", "markets": [{"slug": "arsenal"}]}]},
            True,
            tmp_path / "search",
            fetched_at=START,
            verified=True,
        )

    collector.client = SimpleNamespace(get_json=get_json)
    anchor = ExternalAnchor(
        source_id="x",
        event_id="e",
        source="Book",
        category="sports_1x2",
        outcome="Arsenal",
        decimal_odds=2,
        timestamp=START,
        metadata={"home": "Arsenal", "away": "Chelsea"},
    )
    markets = collector.fetch_fixture_markets([anchor])
    assert markets[0].raw_metadata["collected_at"] == (START + timedelta(seconds=30)).isoformat()
    assert markets[0].raw_metadata["metadata_cache_verified"] is True


def test_canonical_fixture_cap_across_venue_wrappers_and_restart(tmp_path):
    settings = defaults()
    settings.max_position_per_event = 100
    runner, source, clock = setup_study(
        tmp_path, settings=settings, additional_market_ids=["wrapper-market"]
    )

    class Wrappers:
        def collect(self, *args):
            snapshot = source.collect(*args)
            market = snapshot.markets[0]
            snapshot.markets.append(
                market.model_copy(
                    update={
                        "market_id": "wrapper-market",
                        "event_id": "another-venue-event",
                        "token_ids": [t + "-wrapper" for t in market.token_ids],
                    },
                    deep=True,
                )
            )
            snapshot.books.extend(
                [
                    b.model_copy(
                        update={"market_id": "wrapper-market", "token_id": b.token_id + "-wrapper"},
                        deep=True,
                    )
                    for b in list(snapshot.books)
                ]
            )
            return snapshot

    runner.run_once(Wrappers())
    result = runner.history()[0][1]
    fills = [d for d in result["decisions"] if d["approved"]]
    assert len(fills) == 1 and sum(d["size"] for d in fills) == 100
    assert {s["risk_event_id"] for s in result["signals"]} == {"fixture:synthetic:synthetic-odds"}
    assert any("max position per event" in d["reason"] for d in result["decisions"])
    clock.now += timedelta(minutes=1)
    runner = StudyRunner(tmp_path, clock=lambda: clock.now)
    runner.run_once(Wrappers())
    assert not any(d["approved"] for d in runner.history()[-1][1]["decisions"])
    trader = PaperTrader(runner.settings, portfolio_path=tmp_path / "portfolio.sqlite")
    assert trader.portfolio.exposures()[1] == {"fixture:synthetic:synthetic-odds": 100}
    trader.portfolio.connection.close()


def test_final_outage_counts_missing_tail_and_entry_slots(tmp_path):
    runner, source, clock = setup_study(tmp_path)
    runner.run_once(source)
    clock.now = runner.final_at + timedelta(seconds=1)
    runner.run_once(source)
    coverage = json.loads((tmp_path / "collection_coverage.json").read_text())
    assert coverage["expected_slots"] == 72
    assert coverage["attempted_slots"] == 1
    assert coverage["missed_slots"] == coverage["missed_tail_slots"] == 71
    assert coverage["entry_missed_slots"] == 9
    assert not coverage["schedule_complete"]
    report = (tmp_path / "report.md").read_text()
    assert "missed scheduled slots: 71" in report
    assert "INCOMPLETE" in report
    assert json.loads((tmp_path / "checkpoint.json").read_text())["schedule_coverage"] == coverage


def test_never_started_study_does_not_claim_complete_collection(tmp_path):
    runner, source, clock = setup_study(tmp_path)
    clock.now = runner.final_at + timedelta(seconds=1)
    assert runner.run_once(source) == "complete"
    coverage = runner.schedule_coverage(runner.history(), clock.now)
    assert coverage["attempted_slots"] == 0 and coverage["missed_slots"] == 72
    assert coverage["entry_missed_slots"] == 10
    assert "INCOMPLETE" in (tmp_path / "report.md").read_text()


def test_schedule_counts_initial_internal_and_tail_gaps(tmp_path):
    runner, source, clock = setup_study(tmp_path, now=START + timedelta(minutes=2))
    runner.run_once(source)
    clock.now = START + timedelta(minutes=5)
    runner.run_once(source)
    coverage = runner.schedule_coverage(runner.history(), runner.final_at + timedelta(seconds=1))
    assert (
        coverage["missed_initial_slots"],
        coverage["missed_internal_slots"],
        coverage["missed_tail_slots"],
    ) == (2, 2, 66)
    assert coverage["missed_slots"] == 70
    assert runner.schedule_coverage([], START - timedelta(seconds=1))["expected_slots"] == 0
    assert runner.schedule_coverage(runner.history(), runner.end)["entry_expected_slots"] == 10


def fixture_event(event_id, league="synthetic-premier-league"):
    return dict(
        id=event_id,
        home="Arsenal",
        away="Chelsea",
        sport="football",
        league={"slug": league, "name": "Synthetic Premier League"},
        date=(START + timedelta(hours=3)).isoformat(),
    )


def test_scoped_discovery_paginates_before_selection(tmp_path):
    runner, _, _ = setup_study(
        tmp_path,
        provider="odds_api_io",
        design_overrides={
            "event_page_size": 2,
            "max_event_pages": 2,
            "max_events": 4,
            "odds_refresh_seconds": 180,
        },
    )
    calls = []

    class Pages:
        def list_events(self, *args, **kwargs):
            calls.append(kwargs)
            return (
                [fixture_event("1"), fixture_event("2")]
                if kwargs["skip"] == 0
                else [fixture_event("3")]
            )

    events, selected, audit, rejected = LiveStudySource(runner).discover_events(Pages())
    assert [c["skip"] for c in calls] == [0, 2]
    assert all(c["league"] == runner.design.league_slug and c["status"] == "pending" for c in calls)
    assert len(events) == len(selected) == 3 and rejected == []
    assert audit.complete and audit.pages == 2


@pytest.mark.parametrize(
    "reason", ["page_budget_exhausted", "selected_event_cap_reached", "repeated_page_no_progress"]
)
def test_discovery_caps_never_claim_complete_coverage(tmp_path, reason):
    config = {"event_page_size": 2}
    if reason == "selected_event_cap_reached":
        config["max_events"] = 1
    if reason == "repeated_page_no_progress":
        config.update(max_event_pages=2, odds_refresh_seconds=180)
    runner, _, _ = setup_study(tmp_path, provider="odds_api_io", design_overrides=config)

    class FullPage:
        def list_events(self, *args, **kwargs):
            return [fixture_event("1"), fixture_event("2")]

    events, selected, audit, _ = LiveStudySource(runner).discover_events(FullPage())
    assert len(events) == 2 and not audit.complete
    assert audit.status == "capped" and audit.reason == reason
    assert len(selected) <= runner.design.max_events


def test_partial_page_failure_preserves_denominator_and_blocks_new_odds(tmp_path, monkeypatch):
    runner, _, _ = setup_study(
        tmp_path,
        provider="odds_api_io",
        design_overrides={
            "event_page_size": 2,
            "max_event_pages": 2,
            "odds_refresh_seconds": 180,
        },
    )

    def pages(self, *args, **kwargs):
        if kwargs["skip"]:
            raise RuntimeError("simulated rate-limit denial")
        return [fixture_event("1"), fixture_event("2")]

    monkeypatch.setattr(FrozenBookProvider, "list_events", pages)
    monkeypatch.setattr(
        FrozenBookProvider,
        "fetch_odds_payloads",
        lambda *a, **k: pytest.fail("failed discovery must not request odds"),
    )
    runner.run_once(LiveStudySource(runner))
    snapshot = runner.history()[0][0]["snapshot"]
    assert len(snapshot["odds_events"]) == 2
    assert snapshot["event_discovery"]["status"] == "failed"
    assert not snapshot["event_discovery"]["complete"]
    assert snapshot["errors"] and snapshot["anchors"] == []


def test_explicit_league_slug_and_page_budget_are_required():
    with pytest.raises(ValueError, match="league_slug"):
        StudyDesign(league="EPL", bookmakers=["Book"])
    with pytest.raises(ValueError, match="headroom"):
        StudyDesign(
            league="EPL",
            league_slug="england-premier-league",
            bookmakers=["Book"],
            max_event_pages=3,
        )


def test_league_query_finds_fixture_beyond_old_global_cap(tmp_path, monkeypatch):
    runner, _, _ = setup_study(tmp_path, provider="odds_api_io")
    calls = []

    def response(url, **kwargs):
        params = kwargs["params"]
        calls.append(params)
        global_rows = [fixture_event(str(i), "another-league") for i in range(10)] + [
            fixture_event("target")
        ]
        rows = (
            [e for e in global_rows if e["league"]["slug"] == params.get("league")]
            if params.get("league")
            else global_rows
        )
        return SimpleNamespace(
            status_code=200, headers={}, json=lambda: rows[: int(params["limit"])]
        )

    settings = runner.settings.model_copy(update={"odds_api_io_key": "DUMMY"})
    provider = FrozenBookProvider(settings, http_get=response)
    source = LiveStudySource(runner)
    events, selected, audit, _ = source.discover_events(provider)
    assert [e["id"] for e in selected] == ["target"]
    assert calls[0]["league"] == runner.design.league_slug
    assert audit.complete and len(events) == 1


def test_inconsistent_fixture_identity_across_pages_is_failed_discovery(tmp_path):
    runner, _, _ = setup_study(
        tmp_path,
        provider="odds_api_io",
        design_overrides={
            "event_page_size": 2,
            "max_event_pages": 2,
            "odds_refresh_seconds": 180,
        },
    )

    class ConflictingPages:
        def list_events(self, *args, **kwargs):
            if kwargs["skip"] == 0:
                return [fixture_event("1"), fixture_event("2")]
            return [{**fixture_event("1"), "home": "Different participant"}]

    _, _, audit, _ = LiveStudySource(runner).discover_events(ConflictingPages())
    assert audit.status == "failed" and not audit.complete
    assert "conflicting_event_identity" in audit.reason


def test_short_repeated_page_is_not_misread_as_exhausted_coverage(tmp_path):
    runner, _, _ = setup_study(
        tmp_path,
        provider="odds_api_io",
        design_overrides={
            "event_page_size": 2,
            "max_event_pages": 2,
            "odds_refresh_seconds": 180,
        },
    )

    class Repeated:
        def list_events(self, *args, **kwargs):
            return (
                [fixture_event("1"), fixture_event("2")]
                if kwargs["skip"] == 0
                else [fixture_event("1")]
            )

    _, _, audit, _ = LiveStudySource(runner).discover_events(Repeated())
    assert not audit.complete and audit.reason == "repeated_page_no_progress"


@pytest.mark.parametrize("payload", [{"unexpected": []}, ["invalid-event-row"]])
def test_bad_event_schema_does_not_certify_empty_coverage(tmp_path, payload):
    settings = defaults().model_copy(
        update={
            "odds_api_io_key": "DUMMY",
            "cache_dir": tmp_path,
            "odds_api_io_rate_limit_state": tmp_path / "budget.json",
        }
    )
    provider = FrozenBookProvider(
        settings,
        http_get=lambda *a, **k: SimpleNamespace(status_code=200, headers={}, json=lambda: payload),
    )
    with pytest.raises(ValueError, match="coverage cannot be established"):
        provider.list_events("football", league="england-premier-league")
