"""One bounded real-data feasibility observation, never a performance experiment."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from edgelab.config.settings import Settings
from edgelab.data.cache import atomic_json
from edgelab.data.external_sources.odds_provider import parse_timestamp
from edgelab.fair_value.sports_odds_model import SportsOddsModel
from edgelab.data.polymarket_collector import parse_clob_order_book
from edgelab.matching.event_resolver import resolve_events, write_event_match_audit
from edgelab.research.experiment import file_hash, settings_snapshot, source_hashes
from edgelab.research.live_analysis import add_avoid_signals, rewrite_anchors_for_matches
from edgelab.research.pipeline import write_signals_csv
from edgelab.research.study import StudyDesign, StudySnapshot
from edgelab.research.study_sources import LiveStudySource, StudyCollectionContext
from edgelab.research.study_report import write_rows
from edgelab.strategies.taker_edge import generate_signals


@dataclass
class PilotContext:
    design: StudyDesign
    settings: Settings
    start: datetime
    end: datetime
    clock: Callable[[], datetime]


def write_pilot_report(
    directory: Path,
    snapshot: StudySnapshot,
    context: StudyCollectionContext,
    offline_reparse: bool = False,
) -> dict[str, object]:
    """Preserve exclusions and distinguish coverage failure from evidence of no alpha."""
    now, settings = snapshot.as_of, context.settings
    markets = []
    freshness: Counter[str] = Counter()
    for market in snapshot.markets:
        received = parse_timestamp(market.raw_metadata.get("collected_at"))
        if received is None or received > now:
            freshness["unavailable_market_metadata"] += 1
        elif market.raw_metadata.get("metadata_cache_verified") is not True:
            freshness["unverified_market_metadata"] += 1
        elif (now - received).total_seconds() > settings.max_data_age_seconds:
            freshness["stale_market_metadata"] += 1
        else:
            markets.append(market)
    anchors = []
    for anchor in snapshot.anchors:
        if anchor.available_at is None or max(anchor.timestamp, anchor.available_at) > now:
            freshness["unavailable_odds"] += 1
        elif anchor.metadata.get("cache_verified") is not True:
            freshness["unverified_odds"] += 1
        elif (now - min(anchor.timestamp, anchor.available_at)).total_seconds() > min(
            settings.max_data_age_seconds, settings.max_odds_age_seconds
        ):
            freshness["stale_odds"] += 1
        else:
            anchors.append(anchor)
    books = []
    for book in snapshot.books:
        if book.available_at is None or max(book.timestamp, book.available_at) > now:
            freshness["unavailable_orderbook"] += 1
        elif (now - min(book.timestamp, book.available_at)).total_seconds() > min(
            settings.max_data_age_seconds, settings.max_orderbook_age_seconds
        ):
            freshness["stale_orderbook"] += 1
        else:
            books.append(book)
    # Matching diagnostics include stale rows; only eligible rows can enter valuation.
    matches = resolve_events(
        snapshot.markets, snapshot.anchors, context.design.min_match_score, now
    )
    write_event_match_audit(directory / "event_match_audit.csv", matches)
    eligible_matches = resolve_events(markets, anchors, context.design.min_match_score, now)
    rewritten = rewrite_anchors_for_matches(anchors, eligible_matches)
    fairs = SportsOddsModel(settings, now).compute(markets, rewritten, books)
    bbas = [b.best_bid_ask() for b in books]
    signals = generate_signals(markets, fairs, bbas, settings, now)
    pricing = {(s.market_id, s.outcome): s for s in signals}
    edge_rows = []
    for fair in fairs:
        signal = pricing.get((fair.market_id, fair.outcome))
        quoted = (
            signal is not None
            and signal.prediction_bid is not None
            and signal.prediction_ask is not None
        )
        side = None
        if signal is not None and quoted:
            buy = signal.buy_edge_adjusted
            sell = signal.sell_edge_adjusted
            if buy is not None and sell is not None:
                side = "BUY_YES" if buy >= sell else "SELL_YES"
        edge_rows.append(
            {
                "market_id": fair.market_id,
                "outcome": fair.outcome,
                "fair_probability": fair.fair_probability,
                "source": fair.source,
                "uncertainty": fair.uncertainty,
                "diagnostic_side": side,
                "bid": signal.prediction_bid if signal else None,
                "ask": signal.prediction_ask if signal else None,
                "raw_edge": signal.raw_edge if signal and quoted else None,
                "cost_drag": signal.cost_drag if signal and quoted else None,
                "adjusted_edge": signal.adjusted_edge if signal and quoted else None,
                "available_size": signal.available_size if signal and quoted else None,
                "action": "AVOID",
                "risk_decision": "NOT_EVALUATED_FEASIBILITY",
                "reason": signal.reason if signal else "no fresh executable quote",
            }
        )
    write_rows(
        directory / "edge_observations.csv",
        edge_rows,
        list(edge_rows[0]) if edge_rows else ["market_id", "outcome", "action", "reason"],
    )
    signals = add_avoid_signals(
        snapshot.markets, signals, fairs, bbas, matches, anchors, settings, now
    )
    for signal in signals:
        signal.action = "AVOID"
        signal.reason = "feasibility observation only; no execution authorized; " + signal.reason
    write_signals_csv(directory / "signals.csv", signals)
    blockers = [
        "settlement pairs have not been reviewed in this feasibility observation",
        "venue fees, arrival latency and partial-fill assumptions are unvalidated",
        "uncertainty buffers lack an independent training calibration",
        "no prospective held-out observations or settled strategy returns",
    ]
    if not snapshot.odds_events:
        blockers.append("no fixture universe observed; inspect provider/request diagnostics")
    if not snapshot.anchors:
        blockers.append("no registered-bookmaker 1X2 odds received; entitlement unconfirmed")
    if not snapshot.markets:
        blockers.append("no comparable Polymarket candidates collected")
    if not fairs:
        blockers.append("no fresh complete comparable fair values")
    if not books:
        blockers.append("no fresh matched executable books")
    elif not any(s.freshness_status == "fresh" for s in pricing.values()):
        blockers.append("no jointly fresh quote/odds pairs after cross-source skew checks")
    if snapshot.event_discovery is None or not snapshot.event_discovery.complete:
        blockers.append("fixture discovery incomplete; zero matches is not a census result")
    summary: dict[str, object] = {
        "evidence_type": "unregistered_feasibility_observation",
        "as_of": now.isoformat(),
        "offline_reparse": offline_reparse,
        "live_orders": False,
        "paper_trades": 0,
        "profitability_established": False,
        "odds_events": len(snapshot.odds_events),
        "external_1x2_rows": len(snapshot.anchors),
        "polymarket_candidates": len(snapshot.markets),
        "matching": dict(Counter(m.decision for m in matches)),
        "matched_external_fixtures": len(
            {m.external_event_id for m in matches if m.decision == "MATCH"}
        ),
        "queries_attempted": len(snapshot.discovery_queries),
        "candidate_cap_reached": len(snapshot.markets) >= context.design.max_markets,
        "fresh_complete_fair_values": len(fairs),
        "fresh_books": len(books),
        "jointly_fresh_priced_outcomes": sum(
            s.prediction_bid is not None
            and s.prediction_ask is not None
            and s.freshness_status == "fresh"
            for s in pricing.values()
        ),
        "freshness_exclusions": dict(freshness),
        "odds_network_attempts": 0
        if offline_reparse
        else sum(
            r.get("provider") == "odds_api_io" and r.get("network_attempt") is True
            for r in snapshot.requests
        ),
        "request_failures": [r for r in snapshot.requests if r.get("status") != "ok"],
        "event_discovery": snapshot.event_discovery.model_dump(mode="json")
        if snapshot.event_discovery
        else None,
        "blockers": blockers,
        "clv": None,
        "net_markout": None,
    }
    atomic_json(directory / "readiness.json", summary)
    lines = [
        "# Soccer 1X2 Feasibility Observation",
        "",
        "**AVOID. No actionable edge established. This is not a preregistered held-out study,**",
        "**not a completed pilot campaign, and not evidence for or against profitability.**",
        "No orders or paper trades are executed by this command. Costs remain placeholders.",
        "",
        f"As-of: {now.isoformat()}. League: {context.design.league}.",
        f"Requested books: {', '.join(context.design.bookmakers)}. No source substitutions.",
        f"Odds-API.io network attempts: {summary['odds_network_attempts']} (per-run cap 3; shared cap 90/h).",
        "Original observation time retained; offline reparse made zero network calls."
        if offline_reparse
        else "Single observation; no continuous collection is running.",
        "Bookmaker sharpness/independence is unvalidated; a Bet365-only check is recreational coverage, not a sharp anchor.",
        "",
        "## Coverage And Freshness",
        "",
        f"- Fixtures observed: {len(snapshot.odds_events)}",
        f"- External 1X2 rows: {len(snapshot.anchors)}",
        f"- Polymarket candidate markets: {len(snapshot.markets)}",
        f"- Match decisions: {summary['matching']}",
        f"- Distinct matched fixtures: {summary['matched_external_fixtures']} (not independent trade samples)",
        f"- Targeted queries: {summary['queries_attempted']}; candidate cap reached: {summary['candidate_cap_reached']}",
        f"- Fresh complete fair values: {len(fairs)}; fresh matched books: {len(books)}",
        f"- Jointly fresh priced outcomes after cross-source skew checks: {summary['jointly_fresh_priced_outcomes']}",
        f"- Freshness exclusions: {dict(freshness)}",
        f"- Discovery: {summary['event_discovery']}",
        "",
        "## Provider Diagnostics",
        "",
    ]
    lines.extend(
        [
            "Captured request diagnostics; these are not new calls during an offline reparse.",
            "",
            "| Provider | Captured attempts | Failures | Cache hits | Last local budget |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for provider in sorted({r["provider"] for r in snapshot.requests}):
        requests = [r for r in snapshot.requests if r["provider"] == provider]
        lines.append(
            f"| {provider} | {len(requests)} | {sum(r.get('status') != 'ok' for r in requests)} | "
            f"{sum(r.get('cache_hit') is True for r in requests)} | {requests[-1].get('remaining_local_budget', 'unknown')} |"
        )
    for request in (r for r in snapshot.requests if r.get("status") != "ok"):
        lines.append(
            f"- {request['provider']}: {request['status']}; "
            f"HTTP {request.get('http_status', 'not reported')}; "
            f"error {request.get('error_type', 'none')}; "
            f"remaining local budget {request.get('remaining_local_budget', 'unknown')}"
        )
    lines.extend(["", "## Observed Fixtures", ""])
    for event in snapshot.odds_events:
        lines.append(
            f"- {event.get('id', '')}: {event.get('home', '')} vs {event.get('away', '')}; "
            f"{event.get('date') or event.get('startTime') or 'kickoff unavailable'}"
        )
    if not snapshot.odds_events:
        lines.append("No fixtures observed. A source failure is not proof that no fixtures exist.")
    lines.extend(
        [
            "",
            "## Diagnostic Pricing",
            "",
            "`edge_observations.csv` contains probabilities, bid/ask, costs and uncertainty.",
            "Blank pricing fields mean no eligible quote, not a zero price or zero cost.",
            "Every action remains AVOID; settlement and execution have not been certified.",
        ]
    )
    quoted_rows = [r for r in edge_rows if r["bid"] is not None and r["ask"] is not None]
    if quoted_rows:
        lines.extend(
            [
                "",
                "Individual-book freshness only: joint timing or settlement checks may still fail.",
                "SELL rows require funded inventory; no naked short is implied. Costs include heuristic uncertainty.",
                "",
                "| Market | Side | Fair | Bid | Ask | Raw edge | Cost drag | Adjusted edge | Action |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        titles = {m.market_id: m.title for m in markets}
        for row in quoted_rows:
            prices = " | ".join(
                f"{row[key]:.4f}"
                for key in (
                    "fair_probability",
                    "bid",
                    "ask",
                    "raw_edge",
                    "cost_drag",
                    "adjusted_edge",
                )
            )
            title = titles[str(row["market_id"])].replace("|", "\\|")
            lines.append(f"| {title} | {row['diagnostic_side']} | {prices} | AVOID |")
    lines.extend(["", "## Study Launch Blockers", "", *[f"- {b}" for b in blockers]])
    lines.extend(
        [
            "",
            "## Next Decision",
            "",
            "Resolve source failures/entitlement first; then collect a separate training pilot.",
            "Review actual venue and bookmaker regulation-time and cancellation terms per pair.",
            "Estimate costs and uncertainty on training events, never this held-out sample.",
            "Only then register future evaluation dates, an event-level power plan and frozen settings.",
            "Retain every rejection and missing follow-up. Do not relax gates to manufacture trades.",
            "A single observation cannot supply event-level variance, CLV or a profitability estimate.",
        ]
    )
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    atomic_json(
        directory / "data_freshness.json",
        {
            "as_of": now.isoformat(),
            "exclusions": dict(freshness),
            "source_errors": snapshot.errors,
            "requests": snapshot.requests,
        },
    )
    return summary


def load_pilot_observation(directory: Path) -> tuple[StudySnapshot, StudyDesign, dict[str, Any]]:
    """Verify local input hashes, not the truth or economic quality of the source."""
    manifest = json.loads((directory / "manifest.json").read_text())
    if (
        manifest.get("evidence_type") != "unregistered_feasibility_observation"
        or manifest.get("live_trading_enabled") is not False
        or manifest.get("profitability_claim") is not False
    ):
        raise ValueError("input must be a research-only feasibility observation")
    if not {"snapshot.json", "study_design.json"} <= set(manifest["artifacts"]):
        raise ValueError("observation is missing sealed inputs")
    for relative, expected in manifest["artifacts"].items():
        path = (directory / relative).resolve()
        if Path(relative).is_absolute() or not path.is_relative_to(directory.resolve()):
            raise ValueError("observation artifact escapes directory")
        if file_hash(path) != expected:
            raise ValueError("observation artifact integrity failure")
    return (
        StudySnapshot.model_validate_json((directory / "snapshot.json").read_text()),
        StudyDesign.model_validate_json((directory / "study_design.json").read_text()),
        manifest,
    )


def reparse_pilot(source: Path, directory: Path) -> dict[str, object]:
    """Repair recorded CLOB parsing without changing receipt time or making requests."""
    snapshot, design, manifest = load_pilot_observation(source)
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError("offline reparse requires a new empty directory")
    tokens: dict[str, str] = {}
    for book in snapshot.books:
        if book.token_id in tokens and tokens[book.token_id] != book.market_id:
            raise ValueError("ambiguous recorded token identity")
        tokens[book.token_id] = book.market_id
    books = []
    for request in snapshot.requests:
        if (
            request.get("provider") != "polymarket"
            or request.get("status") != "ok"
            or not request["endpoint"].endswith("/book")
        ):
            continue
        relative = request["consumed_response"]
        if relative not in manifest["artifacts"]:
            raise ValueError("unsealed book response")
        envelope = json.loads((source / relative).read_text())
        payload = envelope.get("payload")
        if (
            not isinstance(payload, dict)
            or envelope.get("cache_schema") != 1
            or envelope.get("http_status") != 200
            or hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            != envelope.get("payload_sha256")
        ):
            raise ValueError("invalid cached book envelope")
        token = str(payload.get("asset_id") or "")
        received = parse_timestamp(envelope.get("fetched_at"))
        if token not in tokens or received is None or received > snapshot.as_of:
            raise ValueError("unavailable or unidentified book response")
        parsed = parse_clob_order_book(tokens[token], token, payload, observed_at=received)
        if parsed is None:
            raise ValueError("unparseable recorded book")
        books.append(parsed)
    if len(books) != len(snapshot.books):
        raise ValueError("cannot reconstruct every recorded book")
    snapshot = snapshot.model_copy(update={"books": books})
    settings = Settings(_env_file=None, **manifest["settings"])
    context = PilotContext(
        design, settings, snapshot.started_at, snapshot.started_at, lambda: snapshot.as_of
    )
    directory.mkdir(parents=True, exist_ok=True)
    for relative in manifest["artifacts"]:
        if relative.startswith("raw/"):
            atomic_json(directory / relative, json.loads((source / relative).read_text()))
    atomic_json(directory / "study_design.json", design.model_dump(mode="json"))
    atomic_json(directory / "snapshot.json", snapshot.model_dump(mode="json"))
    atomic_json(
        directory / "reparse_provenance.json",
        {
            "source_manifest_sha256": file_hash(source / "manifest.json"),
            "source_snapshot_sha256": file_hash(source / "snapshot.json"),
            "original_as_of": snapshot.as_of.isoformat(),
            "network_requests": 0,
            "operation": "reparse recorded CLOB timestamp; not a new live observation",
        },
    )
    summary = write_pilot_report(directory, snapshot, context, offline_reparse=True)
    write_pilot_manifest(directory, settings)
    return summary


def write_pilot_manifest(directory: Path, settings: Settings) -> None:
    atomic_json(
        directory / "manifest.json",
        {
            "evidence_type": "unregistered_feasibility_observation",
            "created_at": datetime.now(UTC).isoformat(),
            "live_trading_enabled": False,
            "profitability_claim": False,
            "source_hashes": source_hashes(),
            "settings": settings_snapshot(settings),
            "artifacts": {
                str(p.relative_to(directory)): file_hash(p)
                for p in sorted(directory.rglob("*"))
                if p.is_file()
            },
        },
    )


def run_pilot(
    directory: Path,
    design: StudyDesign,
    refresh: bool = False,
    events_from: Path | None = None,
) -> dict[str, object]:
    """Spend at most three odds requests; never register, schedule or execute trades."""
    if design.provider != "odds_api_io" or design.purpose != "data_quality_pilot":
        raise ValueError("feasibility requires a real Odds-API.io data-quality design")
    if design.max_event_pages + (design.max_events + 9) // 10 > 3:
        raise ValueError("feasibility discovery plus odds batches must fit three requests")
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError("feasibility observation requires a new empty directory")
    design = StudyDesign.model_validate(design.model_dump())
    settings = Settings(_env_file=None, **{k: f.default for k, f in Settings.model_fields.items()})
    settings.cache_dir = design.cache_dir
    settings.odds_api_io_rate_limit_state = design.shared_odds_budget_state
    settings.odds_api_io_bookmakers = ",".join(design.bookmakers)
    now = datetime.now(UTC)
    context = PilotContext(design, settings, now, now, lambda: datetime.now(UTC))
    seed = None
    if events_from is not None:
        # Reuse only a hash-bound, recent observation. Never turn an old cache into today's feed.
        seed, original_design, _ = load_pilot_observation(events_from)
        if not 0 <= (now - seed.as_of).total_seconds() <= settings.max_data_age_seconds:
            raise ValueError("recorded fixtures are stale or unavailable; refresh explicitly")
        for field in ("league_slug", "sport", "event_window_hours"):
            if getattr(design, field) != getattr(original_design, field):
                raise ValueError("fixture seed must retain its league, sport and date window")
        context.start = context.end = seed.started_at
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "study_design.json", design.model_dump(mode="json"))
    if seed is not None and events_from is not None:
        atomic_json(
            directory / "fixture_seed.json",
            {
                "snapshot": seed.model_dump(mode="json"),
                "source_snapshot_sha256": file_hash(events_from / "snapshot.json"),
                "note": "Recorded fixture identities only; odds and books are not reused as fresh.",
            },
        )
    try:
        source = LiveStudySource(
            context,
            refresh=refresh,
            odds_request_limit=1 if seed is not None else 3,
            seed_events=seed.odds_events if seed is not None else None,
        )
        snapshot = source.collect(None, now, directory, refresh_odds=True, entries_open=True)
    except Exception as exc:
        # Provider exceptions can contain credentials; persist only sanitized audit/type.
        audit = directory / "request_audit.json"
        snapshot = StudySnapshot(
            started_at=now,
            as_of=context.clock(),
            requests=json.loads(audit.read_text()) if audit.exists() else [],
            errors=[f"collection_failed:{type(exc).__name__}"],
        )
    atomic_json(directory / "snapshot.json", snapshot.model_dump(mode="json"))
    summary = write_pilot_report(directory, snapshot, context)
    write_pilot_manifest(directory, settings)
    return summary
