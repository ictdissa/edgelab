#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from scripts.generate_paper_signals import is_empirical_fully_priced_relative_value_candidate

READINESS_INFRASTRUCTURE_ONLY = "INFRASTRUCTURE_ONLY"
READINESS_WEAK_OR_NEGATIVE = "WEAK_OR_NEGATIVE"
READINESS_NEEDS_DATA_QUALITY_FIXES = "NEEDS_DATA_QUALITY_FIXES"
READINESS_READY_FOR_EXTENDED_PAPER = "READY_FOR_EXTENDED_PAPER"
READINESS_READY_FOR_PAPER_SCALING = "READY_FOR_PAPER_SCALING"
READINESS_NOT_READY_NO_DATA = READINESS_INFRASTRUCTURE_ONLY
READINESS_NOT_READY_NO_MATCHES = READINESS_INFRASTRUCTURE_ONLY
READINESS_NOT_READY_NO_CANDIDATES = READINESS_INFRASTRUCTURE_ONLY
READINESS_NOT_READY_NO_PAPER_TRADES = READINESS_INFRASTRUCTURE_ONLY
READINESS_NOT_READY_NO_CLV = READINESS_INFRASTRUCTURE_ONLY
READINESS_NOT_READY_NO_TRADABLE_CLV = READINESS_NOT_READY_NO_CLV
READINESS_RESEARCH_PROMISING = "RESEARCH_PROMISING"
READINESS_PAPER_TRADING_PROMISING = READINESS_READY_FOR_PAPER_SCALING
READINESS_LIVE_TRADING_NOT_APPROVED = "LIVE_TRADING_NOT_APPROVED"


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: str
    timestamp: str
    categories: list[str]
    markets_scanned: int
    matched_markets: int
    apparent_edges: int
    risk_approved_paper_signals: int
    signals: list[dict[str, str]]
    match_audit: list[dict[str, str]]


@dataclass(frozen=True)
class EvidenceScorecard:
    total_snapshots: int
    markets_scanned: int
    matched_markets: int
    total_signals: int
    apparent_edges: int
    positive_raw_edge_count: int
    positive_adjusted_edge_count: int
    rejected_directional_edge_count: int
    single_leg_paper_candidates: int
    relative_value_executable_candidates: int
    relative_value_paper_eligible_candidates: int
    risk_approved_paper_trades: int
    single_leg_paper_trades: int
    relative_value_paper_trades: int
    average_adjusted_edge: float | None
    average_clv: float | None
    median_clv: float | None
    clv_win_rate: float | None
    clv_observations: int
    relative_value_clv_observations: int
    average_clv_by_trade_type: dict[str, float | None]
    median_clv_by_trade_type: dict[str, float | None]
    clv_by_category: dict[str, dict[str, float | int | None]]
    rejection_reasons: dict[str, int]
    liquidity_distribution: dict[str, float | int | None]
    spread_distribution: dict[str, float | int | None]
    sanity_warning_counts: dict[str, int]
    top_blockers: dict[str, int]
    readiness_classification: str
    live_trading_status: str
    signal_funnel: SignalFunnelSummary
    shadow_clv: ShadowCLVSummary
    threshold_sensitivity: ThresholdSensitivitySummary


@dataclass(frozen=True)
class CampaignArtifactsSummary:
    rejected_directional_edges: int = 0
    single_leg_paper_candidates: int = 0
    relative_value_executable_candidates: int = 0
    relative_value_paper_eligible_candidates: int = 0
    sanity_warning_counts: dict[str, int] | None = None
    top_blockers: dict[str, int] | None = None


@dataclass(frozen=True)
class SignalFunnelSummary:
    total_signals: int = 0
    paper_candidates: int = 0
    biggest_rejection_reason: str = ""
    biggest_rejection_count: int = 0
    closest_category: str = ""
    closest_score: float | None = None


@dataclass(frozen=True)
class ShadowCLVSummary:
    observed_rows: int = 0
    average_clv: float | None = None
    median_clv: float | None = None
    positive: bool = False


@dataclass(frozen=True)
class ThresholdSensitivitySummary:
    settings_tested: int = 0
    max_candidates: int = 0
    settings_with_candidates: int = 0
    average_shadow_clv_at_best: float | None = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Build EdgeLab evidence scorecard")
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    parser.add_argument(
        "--extra-snapshot-dir",
        type=Path,
        action="append",
        default=[],
        help="Additional snapshot directories to include in scorecard totals.",
    )
    parser.add_argument("--ledger", type=Path, default=Path("data/paper_trades/ledger.csv"))
    parser.add_argument("--single-leg-ledger", type=Path)
    parser.add_argument(
        "--relative-value-ledger",
        type=Path,
        default=Path("data/paper_trades/relative_value_ledger.csv"),
    )
    parser.add_argument("--signal-funnel", type=Path)
    parser.add_argument("--shadow-clv", type=Path)
    parser.add_argument("--threshold-sensitivity", type=Path)
    parser.add_argument("--output", type=Path, default=Path("reports/evidence_scorecard.md"))
    args = parser.parse_args()

    snapshot_dirs = [args.snapshot_dir, *args.extra_snapshot_dir]
    records = load_snapshot_records_from_dirs(snapshot_dirs)
    single_leg_ledger = args.single_leg_ledger or args.ledger
    campaign_artifacts = load_campaign_artifacts(args.snapshot_dir, args.output.parent)
    signal_funnel = load_signal_funnel_summary(
        resolve_optional_diagnostic_path(args.signal_funnel, args.output, "signal_funnel.csv")
    )
    shadow_clv = load_shadow_clv_summary(
        resolve_optional_diagnostic_path(
            args.shadow_clv, args.output, "shadow_clv_observations.csv"
        )
    )
    threshold_sensitivity = load_threshold_sensitivity_summary(
        resolve_optional_diagnostic_path(
            args.threshold_sensitivity, args.output, "threshold_sensitivity.csv"
        )
    )
    scorecard = build_scorecard(
        records,
        single_leg_ledger,
        relative_value_ledger_path=args.relative_value_ledger,
        campaign_artifacts=campaign_artifacts,
        signal_funnel=signal_funnel,
        shadow_clv=shadow_clv,
        threshold_sensitivity=threshold_sensitivity,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        render_scorecard(scorecard, records, args.snapshot_dir), encoding="utf-8"
    )

    print(f"total_snapshots={scorecard.total_snapshots}")
    print(f"readiness={scorecard.readiness_classification}")
    print(f"average_clv={format_optional(scorecard.average_clv)}")
    print(f"live_trading_status={scorecard.live_trading_status}")
    print(f"report={args.output}")


def load_snapshot_records(snapshot_dir: Path) -> list[SnapshotRecord]:
    summary_records = load_summary_records(snapshot_dir)
    if summary_records:
        return summary_records
    return load_sqlite_records(snapshot_dir)


def load_snapshot_records_from_dirs(snapshot_dirs: list[Path]) -> list[SnapshotRecord]:
    records: list[SnapshotRecord] = []
    seen_keys: set[tuple[str, str]] = set()
    for snapshot_dir in snapshot_dirs:
        for record in load_snapshot_records(snapshot_dir):
            key = (record.snapshot_id, record.timestamp)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            records.append(record)
    return sorted(records, key=lambda row: row.timestamp)


def load_summary_records(snapshot_dir: Path) -> list[SnapshotRecord]:
    records: list[SnapshotRecord] = []
    if not snapshot_dir.exists():
        return records
    for summary_path in sorted(snapshot_dir.glob("iteration_*/summary.json")):
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        iteration_dir = summary_path.parent
        signals = read_csv_rows(iteration_dir / "signals.csv")
        match_audit = read_csv_rows(iteration_dir / "match_audit.csv")
        totals = summary.get("totals", {}) if isinstance(summary, dict) else {}
        records.append(
            SnapshotRecord(
                snapshot_id=iteration_dir.name,
                timestamp=str(summary.get("timestamp", "")),
                categories=[str(item) for item in summary.get("categories", [])],
                markets_scanned=int(totals.get("markets_scanned") or 0),
                matched_markets=int(totals.get("matched_markets") or 0),
                apparent_edges=int(totals.get("apparent_edges") or 0),
                risk_approved_paper_signals=int(totals.get("risk_approved_paper_signals") or 0),
                signals=signals,
                match_audit=match_audit,
            )
        )
    return records


def load_sqlite_records(snapshot_dir: Path) -> list[SnapshotRecord]:
    records: list[SnapshotRecord] = []
    if not snapshot_dir.exists():
        return records
    for db_path in sorted(snapshot_dir.glob("**/snapshots.sqlite")):
        with sqlite3.connect(db_path) as conn:
            snapshots = conn.execute(
                """
                SELECT id, timestamp, category, markets_count, matches_count, signals_count
                FROM snapshots
                ORDER BY id ASC
                """
            ).fetchall()
            for snapshot_id, timestamp, categories, markets, matches, _signals_count in snapshots:
                signals = load_sqlite_signal_rows(conn, int(snapshot_id))
                match_audit = load_sqlite_match_rows(conn, int(snapshot_id))
                records.append(
                    SnapshotRecord(
                        snapshot_id=f"{db_path}:{snapshot_id}",
                        timestamp=str(timestamp),
                        categories=[part for part in str(categories).split(",") if part],
                        markets_scanned=int(markets or 0),
                        matched_markets=sum(
                            1 for row in match_audit if row.get("decision") == "MATCH"
                        )
                        or int(matches or 0),
                        apparent_edges=sum(
                            1 for row in signals if parse_float(row.get("adjusted_edge")) > 0
                        ),
                        risk_approved_paper_signals=sum(
                            1
                            for row in signals
                            if row.get("risk_approved") in {"True", "true", "1"}
                        ),
                        signals=signals,
                        match_audit=match_audit,
                    )
                )
    return records


def load_sqlite_signal_rows(conn: sqlite3.Connection, snapshot_id: int) -> list[dict[str, str]]:
    rows = []
    cursor = conn.execute("SELECT raw_json FROM signals WHERE snapshot_id = ?", (snapshot_id,))
    for (payload,) in cursor.fetchall():
        try:
            row = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(
                {str(key): "" if value is None else str(value) for key, value in row.items()}
            )
    return rows


def load_sqlite_match_rows(conn: sqlite3.Connection, snapshot_id: int) -> list[dict[str, str]]:
    rows = []
    cursor = conn.execute("SELECT raw_json FROM match_audit WHERE snapshot_id = ?", (snapshot_id,))
    for (payload,) in cursor.fetchall():
        try:
            row = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(
                {str(key): "" if value is None else str(value) for key, value in row.items()}
            )
    return rows


def build_scorecard(
    records: list[SnapshotRecord],
    ledger_path: Path,
    relative_value_ledger_path: Path | None = None,
    campaign_artifacts: CampaignArtifactsSummary | None = None,
    signal_funnel: SignalFunnelSummary | None = None,
    shadow_clv: ShadowCLVSummary | None = None,
    threshold_sensitivity: ThresholdSensitivitySummary | None = None,
) -> EvidenceScorecard:
    signal_funnel = signal_funnel or SignalFunnelSummary()
    shadow_clv = shadow_clv or ShadowCLVSummary()
    threshold_sensitivity = threshold_sensitivity or ThresholdSensitivitySummary()
    campaign_artifacts = campaign_artifacts or CampaignArtifactsSummary()
    all_signals = [row for record in records for row in record.signals]
    all_matches = [row for record in records for row in record.match_audit]
    raw_edges = [
        parse_float(row.get("raw_edge"))
        for row in all_signals
        if parse_float(row.get("raw_edge")) > 0
    ]
    apparent_signal_edges = [
        parse_float(row.get("best_adjusted_edge") or row.get("adjusted_edge"))
        for row in all_signals
        if parse_float(row.get("best_adjusted_edge") or row.get("adjusted_edge")) > 0
    ]
    paper_trades, clv_values, clv_by_category = collect_clv(ledger_path, all_signals)
    rv_paper_trades, rv_clv_values = collect_relative_value_clv(relative_value_ledger_path)
    rejection_reasons = collect_rejection_reasons(all_signals, all_matches)
    liquidity_values = [
        parse_float(row.get("liquidity") or row.get("available_size"))
        for row in all_signals
        if parse_float(row.get("liquidity") or row.get("available_size")) > 0
    ]
    spread_values = collect_spreads(all_signals)
    markets_scanned = sum(record.markets_scanned for record in records)
    matched_markets = sum(record.matched_markets for record in records)
    apparent_edges = sum(record.apparent_edges for record in records) or len(apparent_signal_edges)
    legacy_risk_candidates = sum(record.risk_approved_paper_signals for record in records)
    single_leg_candidates = campaign_artifacts.single_leg_paper_candidates or legacy_risk_candidates
    total_candidates = (
        single_leg_candidates + campaign_artifacts.relative_value_paper_eligible_candidates
    )
    total_paper_trades = paper_trades + rv_paper_trades
    # Package CLV is dollars; single-leg markout is probability points. Never pool units.
    total_clv_values = clv_values
    average_clv = average(total_clv_values)
    median_clv = median(total_clv_values) if total_clv_values else None
    clv_win_rate = positive_rate(total_clv_values)
    readiness = classify_readiness(
        total_snapshots=len(records),
        matched_markets=matched_markets,
        apparent_edges=apparent_edges,
        candidates=total_candidates,
        paper_trades=total_paper_trades,
        clv_count=len(total_clv_values),
        average_clv=average_clv,
        median_clv=median_clv,
        clv_win_rate=clv_win_rate,
        total_signals=len(all_signals),
        sanity_warning_count=sum((campaign_artifacts.sanity_warning_counts or {}).values()),
        blocking_warning_count=sum((campaign_artifacts.top_blockers or {}).values()),
        shadow_clv=shadow_clv,
    )
    return EvidenceScorecard(
        total_snapshots=len(records),
        markets_scanned=markets_scanned,
        matched_markets=matched_markets,
        total_signals=len(all_signals),
        apparent_edges=apparent_edges,
        positive_raw_edge_count=len(raw_edges),
        positive_adjusted_edge_count=len(apparent_signal_edges),
        rejected_directional_edge_count=campaign_artifacts.rejected_directional_edges,
        single_leg_paper_candidates=single_leg_candidates,
        relative_value_executable_candidates=campaign_artifacts.relative_value_executable_candidates,
        relative_value_paper_eligible_candidates=campaign_artifacts.relative_value_paper_eligible_candidates,
        risk_approved_paper_trades=paper_trades,
        single_leg_paper_trades=paper_trades,
        relative_value_paper_trades=rv_paper_trades,
        average_adjusted_edge=average(apparent_signal_edges),
        average_clv=average_clv,
        median_clv=median_clv,
        clv_win_rate=clv_win_rate,
        clv_observations=len(clv_values),
        relative_value_clv_observations=len(rv_clv_values),
        average_clv_by_trade_type={
            "single_leg": average(clv_values),
            "relative_value": average(rv_clv_values),
        },
        median_clv_by_trade_type={
            "single_leg": median(clv_values) if clv_values else None,
            "relative_value": median(rv_clv_values) if rv_clv_values else None,
        },
        clv_by_category=clv_by_category,
        rejection_reasons=dict(rejection_reasons.most_common(10)),
        liquidity_distribution=distribution(liquidity_values),
        spread_distribution=distribution(spread_values),
        sanity_warning_counts=campaign_artifacts.sanity_warning_counts or {},
        top_blockers=campaign_artifacts.top_blockers or {},
        readiness_classification=readiness,
        live_trading_status=READINESS_LIVE_TRADING_NOT_APPROVED,
        signal_funnel=signal_funnel,
        shadow_clv=shadow_clv,
        threshold_sensitivity=threshold_sensitivity,
    )


def classify_readiness(
    total_snapshots: int,
    matched_markets: int,
    apparent_edges: int,
    candidates: int,
    paper_trades: int,
    clv_count: int,
    average_clv: float | None,
    median_clv: float | None = None,
    clv_win_rate: float | None = None,
    total_signals: int = 0,
    sanity_warning_count: int = 0,
    blocking_warning_count: int = 0,
    shadow_clv: ShadowCLVSummary | None = None,
) -> str:
    if total_snapshots < 20:
        return READINESS_INFRASTRUCTURE_ONLY
    if candidates == 0:
        return READINESS_INFRASTRUCTURE_ONLY
    if paper_trades == 0:
        if shadow_clv and shadow_clv.observed_rows >= 20 and shadow_clv.positive:
            return READINESS_RESEARCH_PROMISING
        return READINESS_INFRASTRUCTURE_ONLY
    if clv_count < 20:
        return READINESS_INFRASTRUCTURE_ONLY
    warning_ratio = sanity_warning_count / total_signals if total_signals else 0.0
    blocking_ratio = blocking_warning_count / total_signals if total_signals else 0.0
    if warning_ratio >= 0.50 or blocking_ratio >= 0.20:
        return READINESS_NEEDS_DATA_QUALITY_FIXES
    if (
        clv_count >= 100
        and average_clv is not None
        and average_clv > 0
        and median_clv is not None
        and median_clv > 0
        and clv_win_rate is not None
        and clv_win_rate > 0.5
    ):
        # Snapshot/row counts cannot establish independent out-of-sample evidence.
        return READINESS_RESEARCH_PROMISING
    if median_clv is not None and median_clv <= 0:
        return READINESS_WEAK_OR_NEGATIVE
    if apparent_edges > 0:
        return READINESS_RESEARCH_PROMISING
    if matched_markets == 0:
        return READINESS_INFRASTRUCTURE_ONLY
    return READINESS_WEAK_OR_NEGATIVE


def collect_clv(
    ledger_path: Path,
    signals: list[dict[str, str]],
) -> tuple[int, list[float], dict[str, dict[str, float | int | None]]]:
    market_category = {
        row.get("market_id", ""): row.get("category", "unknown") or "unknown"
        for row in signals
        if row.get("market_id")
    }
    values: list[float] = []
    by_category: dict[str, list[float]] = defaultdict(list)
    if not ledger_path.exists():
        return 0, values, {}
    paper_trades = 0
    with ledger_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not is_executed_paper_trade(row):
                continue
            paper_trades += 1
            category = market_category.get(row.get("market_id", ""), "unknown")
            for column in ("clv_5m",):
                parsed = float_or_none(row.get(column))
                if parsed is None:
                    continue
                values.append(parsed)
                by_category[category].append(parsed)
    return (
        paper_trades,
        values,
        {
            category: {
                "count": len(category_values),
                "average": average(category_values),
                "median": median(category_values) if category_values else None,
            }
            for category, category_values in sorted(by_category.items())
        },
    )


def collect_relative_value_clv(ledger_path: Path | None) -> tuple[int, list[float]]:
    if ledger_path is None or not ledger_path.exists():
        return 0, []
    trades = 0
    values: list[float] = []
    with ledger_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if (
                row.get("risk_decision") != "APPROVED"
                or row.get("execution_status") != "FILLED_PAPER"
                or row.get("clv_status") == "skipped"
            ):
                continue
            trades += 1
            for column in ("clv_5m",):
                parsed = float_or_none(row.get(column))
                if parsed is not None:
                    values.append(parsed)
    return trades, values


def load_campaign_artifacts(snapshot_dir: Path, report_dir: Path) -> CampaignArtifactsSummary:
    index_rows = read_csv_rows(snapshot_dir / "snapshot_index.csv")
    if index_rows:
        rv_candidates = read_relative_value_candidate_rows(snapshot_dir, report_dir)
        rv_executable_candidates = (
            sum(1 for row in rv_candidates if is_fully_priced_rv_candidate(row))
            if rv_candidates
            else 0
        )
        rv_paper_eligible_candidates = (
            sum(1 for row in rv_candidates if is_paper_eligible_rv_candidate(row))
            if rv_candidates
            else sum(
                parse_int(row.get("paper_eligible_relative_value_packages")) for row in index_rows
            )
        )
        warning_counts: Counter[str] = Counter()
        blocker_counts: Counter[str] = Counter()
        for path in artifact_paths(index_rows, "edge_sanity_warnings_path"):
            for row in read_csv_rows(path):
                code = row.get("warning_code", "")
                if code:
                    warning_counts[code] += 1
                    if row.get("blocks_risk_approval") == "true":
                        blocker_counts[code] += 1
        return CampaignArtifactsSummary(
            rejected_directional_edges=sum(
                parse_int(row.get("rejected_directional_edges")) for row in index_rows
            ),
            single_leg_paper_candidates=sum(
                parse_int(row.get("risk_approved_candidates")) for row in index_rows
            ),
            relative_value_executable_candidates=rv_executable_candidates,
            relative_value_paper_eligible_candidates=rv_paper_eligible_candidates,
            sanity_warning_counts=dict(warning_counts.most_common(10)),
            top_blockers=dict(blocker_counts.most_common(10)),
        )

    rejected_directional = read_csv_rows(report_dir / "rejected_directional_edges.csv")
    paper_candidates = read_csv_rows(report_dir / "paper_candidates.csv")
    rv_candidates = read_relative_value_candidate_rows(snapshot_dir, report_dir)
    warning_rows = read_warning_rows(snapshot_dir, report_dir)
    warning_counts = Counter(
        row.get("warning_code", "") for row in warning_rows if row.get("warning_code")
    )
    blocker_counts = Counter(
        row.get("warning_code", "")
        for row in warning_rows
        if row.get("warning_code") and row.get("blocks_risk_approval") == "true"
    )
    return CampaignArtifactsSummary(
        rejected_directional_edges=len(rejected_directional),
        single_leg_paper_candidates=len(paper_candidates),
        relative_value_executable_candidates=sum(
            1 for row in rv_candidates if is_fully_priced_rv_candidate(row)
        ),
        relative_value_paper_eligible_candidates=sum(
            1 for row in rv_candidates if is_paper_eligible_rv_candidate(row)
        ),
        sanity_warning_counts=dict(warning_counts.most_common(10)),
        top_blockers=dict(blocker_counts.most_common(10)),
    )


def artifact_paths(index_rows: list[dict[str, str]], column: str) -> list[Path]:
    paths: list[Path] = []
    for row in index_rows:
        value = row.get(column, "")
        if value:
            path = Path(value)
            if path.exists():
                paths.append(path)
    return paths


def read_relative_value_candidate_rows(
    snapshot_dir: Path, report_dir: Path
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(snapshot_dir.glob("iteration_*/relative_value_candidates.csv")):
        rows.extend(read_csv_rows(path))
    if rows:
        return rows
    return read_csv_rows(report_dir / "relative_value_candidates.csv")


def is_fully_priced_rv_candidate(row: dict[str, str]) -> bool:
    return is_empirical_fully_priced_relative_value_candidate(row)


def is_paper_eligible_rv_candidate(row: dict[str, str]) -> bool:
    return (
        row.get("relative_value_source") == "live_snapshot"
        and row.get("empirical_evidence_eligible", "true") != "false"
        and row.get("paper_trade_eligible") == "true"
        and row.get("status") == "PAPER_ELIGIBLE"
    )


def read_warning_rows(snapshot_dir: Path, report_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(snapshot_dir.glob("iteration_*/edge_sanity_warnings.csv")):
        rows.extend(read_csv_rows(path))
    if rows:
        return rows
    return read_csv_rows(report_dir / "edge_sanity_warnings.csv")


def is_executed_paper_trade(row: dict[str, str]) -> bool:
    if row.get("clv_status") == "skipped":
        return False
    if row.get("side") not in {"BUY_YES", "SELL_YES"}:
        return False
    return float_or_none(row.get("execution_price") or row.get("price")) is not None


def collect_rejection_reasons(
    signals: list[dict[str, str]],
    matches: list[dict[str, str]],
) -> Counter[str]:
    reasons: Counter[str] = Counter()
    for row in signals:
        for reason in split_reasons(
            row.get("rejection_reason") or row.get("risk_reasons") or row.get("reason") or ""
        ):
            reasons[reason] += 1
    for row in matches:
        reason = row.get("rejection_reason") or ""
        if reason:
            reasons[reason] += 1
    return reasons


def collect_spreads(signals: list[dict[str, str]]) -> list[float]:
    values = []
    for row in signals:
        bid = float_or_none(row.get("bid") or row.get("prediction_bid"))
        ask = float_or_none(row.get("ask") or row.get("prediction_ask"))
        if bid is None or ask is None:
            continue
        values.append(max(0.0, ask - bid))
    return values


def load_signal_funnel_summary(path: Path) -> SignalFunnelSummary:
    if not path.exists():
        return SignalFunnelSummary()
    rows = read_csv_rows(path)
    if not rows:
        return SignalFunnelSummary()
    overall = next((row for row in rows if row.get("scope") == "overall"), rows[0])
    category_rows = [row for row in rows if row.get("scope") != "overall"]
    closest = max(
        category_rows,
        key=lambda row: parse_float(row.get("closest_score")),
        default={},
    )
    return SignalFunnelSummary(
        total_signals=parse_int(overall.get("total_signals")),
        paper_candidates=parse_int(overall.get("paper_candidate")),
        biggest_rejection_reason=overall.get("biggest_rejection_reason", ""),
        biggest_rejection_count=parse_int(overall.get("biggest_rejection_count")),
        closest_category=closest.get("scope", ""),
        closest_score=float_or_none(closest.get("closest_score")),
    )


def load_shadow_clv_summary(path: Path) -> ShadowCLVSummary:
    if not path.exists():
        return ShadowCLVSummary()
    rows = read_csv_rows(path)
    values = [
        value
        for row in rows
        if row.get("status") == "observed"
        for value in [float_or_none(row.get("shadow_clv"))]
        if value is not None
    ]
    avg = average(values)
    return ShadowCLVSummary(
        observed_rows=len(values),
        average_clv=avg,
        median_clv=median(values) if values else None,
        positive=avg is not None and avg > 0,
    )


def load_threshold_sensitivity_summary(path: Path) -> ThresholdSensitivitySummary:
    if not path.exists():
        return ThresholdSensitivitySummary()
    rows = read_csv_rows(path)
    if not rows:
        return ThresholdSensitivitySummary()
    best = max(rows, key=lambda row: parse_int(row.get("candidates")))
    return ThresholdSensitivitySummary(
        settings_tested=len(rows),
        max_candidates=parse_int(best.get("candidates")),
        settings_with_candidates=sum(1 for row in rows if parse_int(row.get("candidates")) > 0),
        average_shadow_clv_at_best=float_or_none(best.get("average_shadow_clv")),
    )


def resolve_optional_diagnostic_path(
    explicit_path: Path | None, output_path: Path, default_name: str
) -> Path:
    if explicit_path is not None:
        return explicit_path
    report_dir = output_path.parent
    if output_path.stem == "evidence_scorecard":
        return report_dir / default_name
    prefixed = report_dir / f"{output_path.stem}_{default_name}"
    if prefixed.exists():
        return prefixed
    return report_dir / f"__missing_{output_path.stem}_{default_name}"


def render_scorecard(
    scorecard: EvidenceScorecard,
    records: list[SnapshotRecord],
    snapshot_dir: Path,
) -> str:
    lines = [
        "# EdgeLab Evidence Scorecard",
        "",
        "Offline scorecard built from research snapshots and paper-ledger CLV fields. It does not "
        "fetch providers, place trades, or approve live trading.",
        "",
        "## Summary",
        "",
        f"- total snapshots: {scorecard.total_snapshots}",
        f"- markets scanned: {scorecard.markets_scanned}",
        f"- matched markets: {scorecard.matched_markets}",
        f"- total signals: {scorecard.total_signals}",
        f"- apparent edges: {scorecard.apparent_edges}",
        f"- positive raw-edge count: {scorecard.positive_raw_edge_count}",
        f"- positive adjusted-edge count: {scorecard.positive_adjusted_edge_count}",
        f"- rejected directional edge count: {scorecard.rejected_directional_edge_count}",
        f"- single-leg paper candidates: {scorecard.single_leg_paper_candidates}",
        f"- relative-value executable candidates: {scorecard.relative_value_executable_candidates}",
        f"- relative-value paper-eligible candidates: {scorecard.relative_value_paper_eligible_candidates}",
        f"- single-leg paper trades: {scorecard.single_leg_paper_trades}",
        f"- relative-value paper trades: {scorecard.relative_value_paper_trades}",
        f"- average adjusted edge: {format_optional(scorecard.average_adjusted_edge)}",
        f"- single-leg tradable CLV observations: {scorecard.clv_observations}",
        f"- relative-value package CLV observations: {scorecard.relative_value_clv_observations}",
        f"- average CLV across tradable evidence: {format_optional(scorecard.average_clv)}",
        f"- median CLV across tradable evidence: {format_optional(scorecard.median_clv)}",
        f"- CLV win rate across tradable evidence: {format_optional(scorecard.clv_win_rate)}",
        f"- readiness classification: `{scorecard.readiness_classification}`",
        f"- live trading status: `{scorecard.live_trading_status}`",
        f"- snapshot directory: `{snapshot_dir}`",
        "",
        "## CLV By Trade Type",
        "",
        "| trade_type | observations | average | median |",
        "| --- | --- | --- | --- |",
        (
            f"| single_leg | {scorecard.clv_observations} | "
            f"{format_optional(scorecard.average_clv_by_trade_type.get('single_leg'))} | "
            f"{format_optional(scorecard.median_clv_by_trade_type.get('single_leg'))} |"
        ),
        (
            f"| relative_value | {scorecard.relative_value_clv_observations} | "
            f"{format_optional(scorecard.average_clv_by_trade_type.get('relative_value'))} | "
            f"{format_optional(scorecard.median_clv_by_trade_type.get('relative_value'))} |"
        ),
        "",
        "## Signal Funnel",
        "",
        f"- total signals analyzed: {scorecard.signal_funnel.total_signals}",
        f"- paper candidates: {scorecard.signal_funnel.paper_candidates}",
        f"- biggest rejection reason: {scorecard.signal_funnel.biggest_rejection_reason or 'n/a'}",
        f"- biggest rejection count: {scorecard.signal_funnel.biggest_rejection_count}",
        f"- category closest to tradability: {scorecard.signal_funnel.closest_category or 'n/a'}",
        "",
        "## Shadow CLV",
        "",
        "Shadow CLV is research-only and does not count as paper-trade evidence.",
        "",
        f"- observed shadow CLV rows: {scorecard.shadow_clv.observed_rows}",
        f"- average shadow CLV: {format_optional(scorecard.shadow_clv.average_clv)}",
        f"- median shadow CLV: {format_optional(scorecard.shadow_clv.median_clv)}",
        "",
        "## Threshold Sensitivity",
        "",
        f"- settings tested: {scorecard.threshold_sensitivity.settings_tested}",
        f"- settings with candidates: {scorecard.threshold_sensitivity.settings_with_candidates}",
        f"- maximum candidates under any setting: {scorecard.threshold_sensitivity.max_candidates}",
        "- production thresholds changed: no",
        "",
        "## Tradable CLV By Category",
        "",
    ]
    if scorecard.clv_by_category:
        lines.extend(
            markdown_table(
                ["category", "observations", "average", "median"],
                [
                    [
                        category,
                        str(values["count"]),
                        format_optional(values["average"]),
                        format_optional(values["median"]),
                    ]
                    for category, values in scorecard.clv_by_category.items()
                ],
            )
        )
    else:
        lines.append("No CLV observations are available.")
    lines.extend(["", "## Rejection Reasons", ""])
    if scorecard.rejection_reasons:
        lines.extend(
            markdown_table(
                ["reason", "count"],
                [[reason, str(count)] for reason, count in scorecard.rejection_reasons.items()],
            )
        )
    else:
        lines.append("No rejection reasons were recorded.")
    lines.extend(
        [
            "",
            "## Sanity Warnings",
            "",
        ]
    )
    if scorecard.sanity_warning_counts:
        lines.extend(
            markdown_table(
                ["warning", "count"],
                [[key, str(value)] for key, value in scorecard.sanity_warning_counts.items()],
            )
        )
    else:
        lines.append("No sanity warnings were loaded.")
    lines.extend(["", "## Top Blockers", ""])
    if scorecard.top_blockers:
        lines.extend(
            markdown_table(
                ["blocker", "count"],
                [[key, str(value)] for key, value in scorecard.top_blockers.items()],
            )
        )
    else:
        lines.append("No blocking sanity warnings were loaded.")
    lines.extend(
        [
            "",
            "## Liquidity Distribution",
            "",
            render_distribution(scorecard.liquidity_distribution),
            "",
            "## Spread Distribution",
            "",
            render_distribution(scorecard.spread_distribution),
            "",
            "## Snapshot Inputs",
            "",
        ]
    )
    if records:
        lines.extend(
            markdown_table(
                ["snapshot", "timestamp", "categories", "markets", "matches", "edges", "risk"],
                [
                    [
                        record.snapshot_id,
                        record.timestamp,
                        ",".join(record.categories),
                        str(record.markets_scanned),
                        str(record.matched_markets),
                        str(record.apparent_edges),
                        str(record.risk_approved_paper_signals),
                    ]
                    for record in records
                ],
            )
        )
    else:
        lines.append("No snapshots found.")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            interpretation(scorecard),
            "",
            "## Next Action",
            "",
            next_action(scorecard),
        ]
    )
    return "\n".join(lines)


def interpretation(scorecard: EvidenceScorecard) -> str:
    if scorecard.readiness_classification == READINESS_INFRASTRUCTURE_ONLY:
        return "The system is currently infrastructure-only or has insufficient empirical evidence. Real money remains blocked."
    if scorecard.readiness_classification == READINESS_NEEDS_DATA_QUALITY_FIXES:
        return "Data-quality warnings are too concentrated to treat positive average CLV as reliable. Real money remains blocked."
    if scorecard.readiness_classification == READINESS_WEAK_OR_NEGATIVE:
        return "Executable evidence is weak or negative after median/win-rate gates. Real money remains blocked."
    if scorecard.readiness_classification == READINESS_RESEARCH_PROMISING:
        return "Research evidence is improving, but paper trading and tradable CLV are still required. Real money remains blocked."
    if scorecard.readiness_classification == READINESS_READY_FOR_EXTENDED_PAPER:
        return (
            "Extended paper research is justified, but live trading remains explicitly unapproved."
        )
    if scorecard.readiness_classification == READINESS_READY_FOR_PAPER_SCALING:
        return "Paper trading may be worth continuing at larger paper scale, but live trading remains explicitly unapproved."
    return "Research plumbing is producing apparent edges, but paper trading and CLV evidence are still required. Real money remains blocked."


def next_action(scorecard: EvidenceScorecard) -> str:
    if scorecard.total_snapshots < 20:
        return (
            "Collect at least 20 repeated snapshots before treating the empirical status as stable."
        )
    if (
        scorecard.single_leg_paper_candidates + scorecard.relative_value_paper_eligible_candidates
        == 0
    ):
        return (
            "Use the signal funnel, sanity triage, and relative-value candidate files to resolve candidate blockers before paper trading. "
            "Do not loosen thresholds solely to create trades."
        )
    if scorecard.single_leg_paper_trades + scorecard.relative_value_paper_trades == 0:
        return "Run the single-leg and relative-value paper traders on strict candidates and build ledgers at executable bid/ask."
    if scorecard.clv_observations + scorecard.relative_value_clv_observations < 20:
        return "Continue snapshot collection until at least 20 tradable CLV observations exist."
    if scorecard.readiness_classification == READINESS_NEEDS_DATA_QUALITY_FIXES:
        return "Fix missing bid/ask, stale fair values, and suspicious-edge concentrations before interpreting CLV."
    if scorecard.readiness_classification == READINESS_WEAK_OR_NEGATIVE:
        return "Do not scale paper size. Revisit fair-value calibration and signal construction before collecting more fills."
    if (
        scorecard.clv_observations + scorecard.relative_value_clv_observations >= 100
        and scorecard.average_clv is not None
        and scorecard.average_clv > 0
        and scorecard.median_clv is not None
        and scorecard.median_clv > 0
        and scorecard.clv_win_rate is not None
        and scorecard.clv_win_rate > 0.5
    ):
        return "Continue paper trading and stress-test robustness; live trading remains unapproved."
    if scorecard.average_clv is not None:
        return (
            "Do not scale paper size yet. Investigate why median CLV or win rate fails the robustness gate "
            "before treating positive average CLV as signal evidence."
        )
    return "Revisit fair-value models and rejection reasons before increasing paper-trading scale."


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def split_reasons(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def distribution(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "median": None, "p75": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": median(ordered),
        "p75": percentile(ordered, 0.75),
        "max": ordered[-1],
    }


def percentile(ordered: list[float], quantile: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    index = quantile * (len(ordered) - 1)
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def render_distribution(values: dict[str, float | int | None]) -> str:
    if not values.get("count"):
        return "No observations."
    return (
        f"count={values['count']}, min={format_optional(values['min'])}, "
        f"median={format_optional(values['median'])}, p75={format_optional(values['p75'])}, "
        f"max={format_optional(values['max'])}"
    )


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def positive_rate(values: list[float]) -> float | None:
    return sum(1 for value in values if value > 0) / len(values) if values else None


def parse_float(value: str | None) -> float:
    parsed = float_or_none(value)
    return 0.0 if parsed is None else parsed


def parse_int(value: str | None) -> int:
    if value in {None, ""}:
        return 0
    try:
        return int(float(value))
    except ValueError:
        return 0


def float_or_none(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def format_optional(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    return f"{value:.4f}"


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
