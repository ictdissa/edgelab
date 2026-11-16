"""Prospective-study diagnostics with a fixed primary estimand and honest missingness."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any, TYPE_CHECKING

import numpy as np

from edgelab.data.cache import atomic_json
from edgelab.data.schemas import FairValue, TradeDecision
from edgelab.execution.paper_trader import PaperTrader
from edgelab.research.clv import ObservationStore
from edgelab.research.experiment import independent_block_sample_size

if TYPE_CHECKING:
    from edgelab.research.study import StudyRunner


def write_rows(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def followups(
    history: list[tuple[dict[str, Any], dict[str, Any] | None]],
    decisions: list[TradeDecision],
    now: datetime,
    max_book_age: int,
) -> list[dict[str, Any]]:
    from edgelab.research.study import StudySnapshot

    observations: list[dict[str, Any]] = []
    snapshots = [(StudySnapshot.model_validate(p["snapshot"]), r) for p, r in history if r]
    external_ids = {}
    for _, result in history:
        if result:
            matches = {
                m["market_id"]: m["external_event_id"]
                for m in result["matches"]
                if m["decision"] == "MATCH"
            }
            external_ids.update(
                {
                    d["signal_id"]: matches.get(d["market_id"], d["event_id"])
                    for d in result["decisions"]
                    if d["approved"]
                }
            )
    for trade in decisions:
        if not trade.approved or trade.price is None or not trade.token_id:
            continue
        for label, seconds in (("5m", 300), ("30m", 1800), ("1h", 3600)):
            target = trade.timestamp + timedelta(seconds=seconds)
            deadline = target + timedelta(seconds=60)
            row: dict[str, Any] = {
                "paper_trade_id": trade.signal_id,
                "event_id": trade.risk_event_id
                or external_ids.get(trade.signal_id, trade.event_id),
                "external_event_id": external_ids.get(trade.signal_id, ""),
                "prediction_event_id": trade.event_id,
                "market_id": trade.market_id,
                "token_id": trade.token_id,
                "outcome": trade.outcome,
                "horizon": label,
                "entry_at": trade.timestamp.isoformat(),
                "target_at": target.isoformat(),
                "observed_at": "",
                "quote_source_at": "",
                "status": "pending" if now <= deadline else "missing_followup",
                "entry_price": trade.price,
                "exit_price": None,
                "exit_depth": None,
                "gross_quote_markout": None,
                "net_quote_markout": None,
                "external_fair_clv": None,
                "fair_source_at": "",
                "missing_net_lower_bound": None,
                "missing_net_upper_bound": None,
            }
            if trade.fee_bps_at_entry is not None and trade.slippage_bps_at_entry is not None:
                entry_drag = (trade.fees + trade.slippage) / trade.size
                exit_slip = trade.slippage_bps_at_entry / 10000
                exit_fee = trade.fee_bps_at_entry / 10000
                endpoints = [
                    (price - trade.price if trade.side == "BUY_YES" else trade.price - price)
                    - entry_drag
                    - exit_slip
                    - exit_fee * price
                    for price in (0, 1)
                ]
                row["missing_net_lower_bound"] = min(endpoints)
                row["missing_net_upper_bound"] = max(endpoints)
            # First eligible quote, never select the most favorable observation.
            for snapshot, _ in snapshots:
                if not target <= snapshot.as_of <= min(now, deadline):
                    continue
                quotes = [
                    b.best_bid_ask()
                    for b in snapshot.books
                    if b.market_id == trade.market_id
                    and b.token_id == trade.token_id
                    and target <= b.timestamp <= snapshot.as_of
                    and b.available_at is not None
                    and b.available_at <= snapshot.as_of
                    and (snapshot.as_of - b.timestamp).total_seconds() <= max_book_age
                ]
                if not quotes:
                    continue
                if len({q.model_dump_json() for q in quotes}) != 1:
                    row["status"] = "ambiguous_followup_quotes"
                    break
                quote = quotes[0]
                selling = trade.side == "BUY_YES"
                exit_price = quote.best_bid if selling else quote.best_ask
                depth = quote.best_bid_size if selling else quote.best_ask_size
                if exit_price is None:
                    continue
                row.update(
                    observed_at=snapshot.as_of.isoformat(),
                    quote_source_at=quote.timestamp.isoformat(),
                    exit_price=exit_price,
                    exit_depth=depth,
                    status="observed"
                    if depth is not None and depth >= trade.size
                    else "insufficient_depth",
                )
                gross = exit_price - trade.price if selling else trade.price - exit_price
                row["gross_quote_markout"] = gross
                if trade.fee_bps_at_entry is not None and trade.slippage_bps_at_entry is not None:
                    row["net_quote_markout"] = (
                        gross
                        - (trade.fees + trade.slippage) / trade.size
                        - exit_price * trade.fee_bps_at_entry / 10000
                        - trade.slippage_bps_at_entry / 10000
                    )
                break
            # External CLV is a separate observation, not a fallback for a missing exit quote.
            for snapshot, result in snapshots:
                if not target <= snapshot.as_of <= min(now, deadline):
                    continue
                fair = next(
                    (
                        FairValue.model_validate(f)
                        for f in result["fair_values"]
                        if f["market_id"] == trade.market_id
                        and f["outcome"].casefold() == trade.outcome.casefold()
                        and target <= datetime.fromisoformat(f["timestamp"]) <= snapshot.as_of
                    ),
                    None,
                )
                if fair is not None and trade.fair_probability is not None:
                    sign = 1 if trade.side == "BUY_YES" else -1
                    row["external_fair_clv"] = sign * (
                        fair.fair_probability - trade.fair_probability
                    )
                    row["fair_source_at"] = fair.timestamp.isoformat()
                    break
            observations.append(row)
    return observations


def primary_statistics(rows: list[dict[str, Any]], alpha: float) -> dict[str, Any]:
    primary = [r for r in rows if r["horizon"] == "5m"]
    blocks: dict[str, list[float]] = defaultdict(list)
    for row in primary:
        if row["status"] == "observed" and row["net_quote_markout"] is not None:
            blocks[row["event_id"]].append(row["net_quote_markout"])
    event_means = [mean(v) for v in blocks.values()]
    trade_values = [v for values in blocks.values() for v in values]
    ci = None
    if len(event_means) >= 20:
        rng = np.random.default_rng(1907)
        estimates = rng.choice(event_means, size=(2000, len(event_means)), replace=True).mean(
            axis=1
        )
        ci = [float(v) for v in np.quantile(estimates, [alpha / 2, 1 - alpha / 2])]
    bounds = []
    for key in ("missing_net_lower_bound", "missing_net_upper_bound"):
        event_values: dict[str, list[float]] = defaultdict(list)
        for row in primary:
            value = row["net_quote_markout"] if row["status"] == "observed" else row.get(key)
            if value is None:
                break
            event_values[row["event_id"]].append(value)
        else:
            bounds.append(mean(mean(v) for v in event_values.values()) if event_values else None)
    return {
        "filled_trades": len(primary),
        "independent_event_blocks_observed": len(blocks),
        "primary_equal_weight_event_mean": mean(event_means) if event_means else None,
        "event_median": median(event_means) if event_means else None,
        "trade_weighted_mean_diagnostic": mean(trade_values) if trade_values else None,
        "event_bootstrap_ci": ci,
        "status_counts": dict(Counter(r["status"] for r in primary)),
        "missingness_quote_model_bounds": bounds if len(bounds) == 2 else None,
        "additional_50bps_each_way_stress": mean(event_means) - 0.01 if event_means else None,
        "inference_caveat": "complete-case descriptive estimate; missing follow-ups may be nonrandom; same-event trades are one block; event independence unverified",
    }


def write_study_report(
    runner: StudyRunner,
    history: list[tuple[dict[str, Any], dict[str, Any] | None]],
    decisions: list[TradeDecision],
    now: datetime,
    trader: PaperTrader,
) -> None:
    from edgelab.research.study import StudySnapshot

    observations = followups(history, decisions, now, runner.settings.max_orderbook_age_seconds)
    stats = primary_statistics(observations, runner.protocol.significance_level)
    store = ObservationStore(runner.directory / "followup_revisions.sqlite")
    try:
        store.append(
            [{k: "" if v is None else str(v) for k, v in row.items()} for row in observations]
        )
    finally:
        store.close()
    columns = (
        list(observations[0])
        if observations
        else ["paper_trade_id", "horizon", "status", "net_quote_markout"]
    )
    write_rows(runner.directory / "followups.csv", observations, columns)
    atomic_json(runner.directory / "primary_statistics.json", stats)
    counts: Counter[str] = Counter()
    coverage = runner.schedule_coverage(history, now)
    atomic_json(runner.directory / "collection_coverage.json", coverage)
    odds_ids: set[str] = set()
    scoped_ids: set[str] = set()
    market_ids: set[str] = set()
    matched_ids: set[str] = set()
    quality = []
    edge_rows = []
    signal_reasons: Counter[str] = Counter()
    skipped: Counter[str] = Counter(d.reason for d in decisions if not d.approved)
    match_rejections: Counter[str] = Counter()
    query_events: set[str] = set()
    for payload, result in history:
        snapshot = StudySnapshot.model_validate(payload["snapshot"])
        odds_ids.update(str(e.get("id")) for e in snapshot.odds_events if e.get("id") is not None)
        scoped_ids.update(a.event_id for a in snapshot.anchors)
        market_ids.update(m.market_id for m in snapshot.markets)
        query_events.update(
            str(q["event_id"]) for q in snapshot.discovery_queries if q.get("event_id")
        )
        counts["missed_slots"] += payload["missed_slots_before"]
        counts["queries"] += len(snapshot.discovery_queries)
        counts["request_attempts"] += len(snapshot.requests)
        counts["network_responses"] += sum(r.get("cache_hit") is False for r in snapshot.requests)
        counts["failed_requests"] += sum(r.get("status") != "ok" for r in snapshot.requests)
        counts["collection_errors"] += len(snapshot.errors)
        counts["scope_rejected_rows"] += len(snapshot.scope_rejections)
        for r in snapshot.scope_rejections:
            match_rejections[r["reason"]] += 1
        expected_tokens = {t for m in snapshot.markets for t in m.token_ids}
        book_ages = [(snapshot.as_of - b.timestamp).total_seconds() for b in snapshot.books]
        odds_ages = [(snapshot.as_of - a.timestamp).total_seconds() for a in snapshot.anchors]
        skews = (
            [
                abs(
                    (
                        datetime.fromisoformat(s["fair_value_timestamp"])
                        - datetime.fromisoformat(s["orderbook_timestamp"])
                    ).total_seconds()
                )
                for s in result["signals"]
                if s.get("fair_value_timestamp") and s.get("orderbook_timestamp")
            ]
            if result
            else []
        )
        quality.append(
            {
                "as_of": snapshot.as_of.isoformat(),
                "slot": payload["slot"],
                "odds_events": len(snapshot.odds_events),
                "scoped_events": len({a.event_id for a in snapshot.anchors}),
                "event_discovery_status": snapshot.event_discovery.status
                if snapshot.event_discovery
                else "unknown",
                "event_discovery_complete": snapshot.event_discovery.complete
                if snapshot.event_discovery
                else False,
                "event_discovery_reason": snapshot.event_discovery.reason
                if snapshot.event_discovery
                else "missing_discovery_audit",
                "discovery_pages": snapshot.event_discovery.pages
                if snapshot.event_discovery
                else 0,
                "candidate_markets": len(snapshot.markets),
                "books": len(snapshot.books),
                "candidate_tokens_without_book": len(
                    expected_tokens - {b.token_id for b in snapshot.books}
                ),
                "oldest_odds_age_seconds": max(odds_ages) if odds_ages else None,
                "oldest_book_age_seconds": max(book_ages) if book_ages else None,
                "maximum_valued_source_skew_seconds": max(skews) if skews else None,
                "stale_odds_rows": sum(v > runner.settings.max_odds_age_seconds for v in odds_ages),
                "stale_book_rows": sum(
                    v > runner.settings.max_orderbook_age_seconds for v in book_ages
                ),
                "future_or_missing_receipt_rows": result["future_or_missing_receipt_rows"]
                if result
                else 0,
                "out_of_scope_anchor_rows": result["out_of_scope_anchor_rows"] if result else 0,
                "errors": "; ".join(snapshot.errors),
            }
        )
        if result:
            decisions_by_id = {d["signal_id"]: d for d in result["decisions"]}
            for signal in result["signals"]:
                decision = decisions_by_id[signal["signal_id"]]
                signal_reasons[signal["reason"]] += 1
                edge_rows.append(
                    {
                        "timestamp": signal["timestamp"],
                        "event": signal["event_id"],
                        "risk_event": signal["risk_event_id"],
                        "market": signal["market_id"],
                        "outcome": signal["outcome"],
                        "source": signal["fair_value_source"],
                        "fair_probability": signal["fair_probability"]
                        if signal["fair_value_source"] != "none"
                        else None,
                        "bid": signal["prediction_bid"],
                        "ask": signal["prediction_ask"],
                        "raw_edge": signal["raw_edge"],
                        "cost_drag": signal["cost_drag"],
                        "adjusted_edge": signal["adjusted_edge"],
                        "liquidity": signal["available_size"],
                        "action": signal["action"],
                        "signal_reason": signal["reason"],
                        "risk_approved": decision["approved"],
                        "risk_reason": decision["reason"],
                    }
                )
            for m in result["matches"]:
                counts[m["decision"]] += 1
                if m["decision"] == "MATCH":
                    matched_ids.add(m["external_event_id"])
                elif m["rejection_reason"]:
                    match_rejections[m["rejection_reason"]] += 1
    write_rows(
        runner.directory / "data_freshness.csv",
        quality,
        list(quality[0]) if quality else ["as_of", "errors"],
    )
    write_rows(
        runner.directory / "signal_observations.csv",
        edge_rows,
        list(edge_rows[0]) if edge_rows else ["timestamp", "market", "action", "risk_reason"],
    )
    unique_traded = {d.risk_event_id or d.event_id for d in decisions if d.approved}
    final = now >= runner.final_at
    power_n = (
        independent_block_sample_size(
            runner.design.pilot_event_std,
            runner.protocol.minimum_effect_size,
            runner.protocol.significance_level,
            runner.protocol.target_power,
        )
        if runner.design.pilot_event_std is not None
        else None
    )
    evidence = runner.manifest["evidence_type"]
    lines = [
        "# EdgeLab Protocol-Driven Study",
        "",
        f"Evidence: **{evidence}**. Purpose: **{runner.design.purpose}**.",
        "SYNTHETIC FIXTURE, NOT PERFORMANCE OR ALPHA EVIDENCE."
        if evidence == "synthetic_integration"
        else "Prospective locally sealed registration; not externally timestamped.",
        "Live trading disabled. Costs/uncertainty are unvalidated research assumptions.",
        "",
        "## Window And Status",
        "",
        f"Entry window: [{runner.start.isoformat()}, {runner.end.isoformat()}).",
        f"Fixed final-report boundary: {runner.final_at.isoformat()}; report as of {now.isoformat()}.",
        f"Status: {'FINAL FIXED-WINDOW REPORT' if final else 'INTERIM, DO NOT SELECT A FAVORABLE STOP'}. New entries cease at the frozen cutoff.",
        "Collection schedule: **INCOMPLETE**; the calendar boundary does not certify complete data collection."
        if coverage["missed_slots"] or coverage["pending_slots"]
        else "Collection schedule: all due slots attempted; this does not establish complete provider/market coverage.",
        "",
        "## Coverage And Freshness",
        "",
        f"Cycles: {len(history)}; expected due slots: {coverage['expected_slots']}; missed scheduled slots: {coverage['missed_slots']} (never backfilled).",
        f"Missing initial/internal/tail slots: {coverage['missed_initial_slots']}/{coverage['missed_internal_slots']}/{coverage['missed_tail_slots']}. Entry-window attempts: {coverage['entry_attempted_slots']}/{coverage['entry_expected_slots']}; uncommitted slots: {coverage['pending_slots']}.",
        f"Unique provider events: {len(odds_ids)}; scoped events: {len(scoped_ids)}; queried events: {len(query_events)}; candidate markets: {len(market_ids)}; matched external events: {len(matched_ids)}.",
        f"Scoped events with no comparable market: {sorted(scoped_ids - matched_ids)}. Discovery queries are league/date-scoped before selection, but selected-event/page/market caps can still truncate coverage. Inspect each cycle's event_discovery audit before inferring absence of comparable markets.",
        f"MATCH/REVIEW/REJECT observations: {counts['MATCH']}/{counts['REVIEW']}/{counts['REJECT']}. Repeated snapshots are not independent events.",
        f"Request attempts: {counts['request_attempts']}; successful network responses: {counts['network_responses']}; failures: {counts['failed_requests']}; collection errors: {counts['collection_errors']}.",
        "See data_freshness.csv, sealed cycle requests/errors, discovery queries and per-cycle event_match_audit.csv. Missing books include unmatched candidates; they are not automatically API failures.",
        f"Match/scope rejection counts: {dict(match_rejections)}.",
        f"Signal reason counts: {dict(signal_reasons)}.",
        "If zero matches: inspect each recorded event/query and rejection before changing selection. Verify provider league labels, complete three-way odds, participant aliases, kickoff times and settlement evidence. Cache-only misses need an explicit budgeted refresh, not relaxed freshness gates.",
        "",
        "## Paper Execution",
        "",
        f"Approved paper decisions: {sum(d.approved for d in decisions)} across {len(unique_traded)} events; skipped decisions: {sum(not d.approved for d in decisions)}.",
        "Fills use ask/bid, displayed size, cash/inventory constraints, independent risk and entry-frozen cost assumptions. Quote-based simulation does not establish achievable fills.",
        f"Rejection reasons: {dict(skipped)}.",
        f"Cash: {trader.portfolio.cash:.6f}; conservative unrealized PnL: {trader.portfolio.unrealized_pnl():.6f}. No settlement outcomes are inferred from future information.",
        f"Stale open marks: {len(trader.portfolio.stale_positions(now, runner.settings.max_orderbook_age_seconds))}. Stale marks are not current executable liquidation prices.",
        "",
        "## Primary Outcome",
        "",
        "5-minute net executable quote markout per share, equal weight per external fixture frozen at entry. Several prediction-market contracts for that fixture remain one block. First exact-token source quote within [target, target+60s], current displayed exit depth >= entry size. This is not realized PnL.",
        f"Primary event mean: {stats['primary_equal_weight_event_mean']}; event median: {stats['event_median']}; observed event blocks: {stats['independent_event_blocks_observed']}.",
        f"Primary follow-up status: {stats['status_counts']}; bootstrap CI: {stats['event_bootstrap_ci']}.",
        f"Missingness sensitivity bounds under binary quote endpoints and frozen costs: {stats['missingness_quote_model_bounds']}. These are hypothetical quote-model bounds, not guaranteed executable exits or realized PnL.",
        "CI withheld below 20 observed blocks; 20 is only a computational reporting floor, not a sample-size approval gate. Missing/depth-deficient follow-ups are excluded and can bias the complete-case estimate.",
        f"Additional 50bps per leg cost stress: {stats['additional_50bps_each_way_stress']}.",
        "External fair CLV is separately timestamped in followups.csv. A missing quote is never replaced by fair value or midpoint. 30m/1h are diagnostic, not alternative primary endpoints.",
        f"Approximate planned independent event requirement: {power_n if power_n is not None else 'UNPLANNED: no independent-event pilot variance supplied'}.",
        "Cross-event dependence, multiple testing and missingness require further empirical validation. No positive result automatically permits capital deployment.",
        "",
        "## Interpretation And Next Experiments",
        "",
        "No validated profitability conclusion. No actionable edge found."
        if not any(d.approved for d in decisions)
        else "Paper candidates survived configured costs; this is not proof of tradable edge. Synthetic prices were deliberately constructed to exercise the lifecycle."
        if evidence == "synthetic_integration"
        else "Paper candidates survived configured costs; achievable fills and net profitability remain unproven.",
        "Before held-out evaluation: adjudicate mapping/settlement benchmarks, measure coverage and latency in a pilot, calibrate uncertainty and fees, then register a new untouched evaluation window. Do not tune this study against its held-out results.",
        "An incomplete schedule or truncated/failed discovery cannot establish that the broader market universe has no edge.",
        "Before real money: verified fee schedules, latency/depth/fill validation, independent event-level evidence, adverse-selection stress, settlement reconciliation, operational incident controls, and a separate execution/security review. No live-order adapter is enabled here.",
        "",
        "## Reproduction",
        "",
        "manifest.json freezes protocol, numeric settings, design, settlement registry and source archive. Input/result cycles form a hash chain; SQLite is paper-accounting authority. CSV/Markdown files are rebuildable projections, not signed evidence. Whole-package malicious replacement is outside local hashes' guarantees.",
        "Research/education only; no financial advice.",
        "",
    ]
    entry_rows = [r for r in edge_rows if datetime.fromisoformat(r["timestamp"]) < runner.end]
    latest = max((r["timestamp"] for r in entry_rows), default=None)
    table = [
        "## Latest Entry-Window Diagnostics",
        "",
        "All observations, costs and risk reasons: signal_observations.csv. Table shows up to 20 outcomes at the last entry-window observation; unpriced outcomes are blank, never a zero-probability forecast.",
        "",
        "| Market / Outcome | Fair | Bid | Ask | Raw Edge | Cost Drag | Adjusted | Size | Action | Risk |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in [r for r in entry_rows if r["timestamp"] == latest][:20]:

        def display(value: Any) -> str:
            return (
                ""
                if value is None
                else f"{value:.6f}"
                if isinstance(value, float)
                else str(value).replace("|", "/").replace("\n", " ")
            )

        values = [
            f"{row['market']} / {row['outcome']}",
            *[
                row[k]
                for k in (
                    "fair_probability",
                    "bid",
                    "ask",
                    "raw_edge",
                    "cost_drag",
                    "adjusted_edge",
                    "liquidity",
                    "action",
                )
            ],
            "APPROVED PAPER" if row["risk_approved"] else row["risk_reason"],
        ]
        table.append("| " + " | ".join(display(v) for v in values) + " |")
    lines.extend(["", *table, ""])
    (runner.directory / "report.md").write_text("\n".join(lines))
    (runner.directory / "data_freshness.md").write_text(
        f"# Data Freshness\n\nAs of {now.isoformat()}. Evidence: {evidence}.\n\n"
        f"Expected slots: {coverage['expected_slots']}; attempted: {coverage['attempted_slots']}; missing: {coverage['missed_slots']}; missing tail: {coverage['missed_tail_slots']}. See collection_coverage.json.\n\n"
        "See data_freshness.csv for all cycle source ages, missing books, unavailable rows and errors. "
        "Cache receipts retain original times; cached-only runs do not refresh age.\n"
    )
