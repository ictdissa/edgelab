#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.research.clv import ObservationStore, PaperTrade, SnapshotSignal
from edgelab.research.ledger_io import ledger_lock
from scripts.run_paper_trader import (
    write_csv as atomic_write_csv,
    LEDGER_COLUMNS as PAPER_LEDGER_COLUMNS,
)

LOGGER = logging.getLogger(__name__)

HORIZONS = {
    "5m": timedelta(minutes=5),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
}

OBSERVATION_COLUMNS = [
    "paper_trade_id",
    "market_id",
    "category",
    "side",
    "horizon",
    "entry_timestamp",
    "observed_timestamp",
    "execution_price",
    "future_bid",
    "future_ask",
    "future_mid",
    "future_fair_probability",
    "later_value",
    "later_value_source",
    "clv",
    "clv_type",
    "shadow_value",
    "shadow_value_source",
    "shadow_clv",
    "status",
]
OBSERVATION_COLUMNS += [
    "outcome",
    "token_id",
    "target_timestamp",
    "actual_horizon_seconds",
    "quote_timestamp",
    "exit_depth",
    "net_markout_estimate",
    "cost_model",
]

RELATIVE_VALUE_OBSERVATION_COLUMNS = [
    "paper_trade_id",
    "candidate_id",
    "relation_type",
    "horizon",
    "entry_timestamp",
    "observed_timestamp",
    "package_entry_cost",
    "package_exit_value",
    "package_clv",
    "status",
    "missing_legs",
]

LEDGER_COLUMNS = PAPER_LEDGER_COLUMNS

RELATIVE_VALUE_LEDGER_COLUMNS = [
    "paper_trade_id",
    "timestamp",
    "candidate_id",
    "snapshot_timestamp",
    "relation_type",
    "leg_count",
    "legs_json",
    "package_entry_cost",
    "package_expected_payoff",
    "package_expected_edge",
    "max_trade_size",
    "executable_violation",
    "theoretical_violation",
    "risk_decision",
    "risk_reasons",
    "execution_status",
    "clv_5m",
    "clv_30m",
    "clv_1h",
    "clv_status",
]


@dataclass(frozen=True)
class TradeRow:
    signal_id: str
    market_id: str
    side: str
    price: float
    timestamp: datetime


@dataclass(frozen=True)
class CLVObservation:
    signal_id: str
    market_id: str
    horizon: str
    entry_price: float
    later_fair_value: float
    side: str
    observed_at: datetime

    @property
    def clv(self) -> float:
        if self.side == "BUY_YES":
            return self.later_fair_value - self.entry_price
        if self.side == "SELL_YES":
            return self.entry_price - self.later_fair_value
        return 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update EdgeLab CLV report")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--single-leg-ledger", type=Path)
    parser.add_argument(
        "--relative-value-ledger",
        type=Path,
        default=Path("data/paper_trades/relative_value_ledger.csv"),
    )
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    parser.add_argument(
        "--extra-snapshot-dir",
        type=Path,
        action="append",
        default=[],
        help="Additional snapshot directories to search for follow-up CLV observations.",
    )
    parser.add_argument("--output", type=Path, default=Path("reports/clv_report.md"))
    parser.add_argument("--observations-output", type=Path)
    parser.add_argument(
        "--relative-value-observations-output",
        type=Path,
    )
    parser.add_argument("--snapshot-db", type=Path)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parse_args()
    args.observations_output = args.observations_output or Path(
        "reports/mock_clv_observations.csv" if args.mock else "reports/clv_observations.csv"
    )
    args.relative_value_observations_output = args.relative_value_observations_output or Path(
        "reports/mock_relative_value_clv_observations.csv"
        if args.mock
        else "reports/relative_value_clv_observations.csv"
    )
    if args.mock and args.output == Path("reports/clv_report.md"):
        args.output = Path("reports/mock_clv_report.md")
    single_leg_ledger = (
        args.single_leg_ledger
        or args.ledger
        or Path(
            "data/paper_trades/mock_ledger.csv" if args.mock else "data/paper_trades/ledger.csv"
        )
    )
    if args.mock:
        observations: list[dict[str, str]] = []
        relative_value_observations: list[dict[str, str]] = []
        rows = read_ledger_rows(single_leg_ledger)
        relative_value_rows = []
    else:
        snapshot_dirs = [args.snapshot_dir, *args.extra_snapshot_dir]
        snapshot_signals = load_snapshot_signals_from_dirs(
            snapshot_dirs, args.output.with_name(args.output.stem + "_quarantine.csv")
        )
        rows, observations = update_single_leg_ledger(single_leg_ledger, snapshot_signals)
        with ledger_lock(args.relative_value_ledger):
            relative_value_rows = read_relative_value_ledger_rows(args.relative_value_ledger)
            relative_value_rows, relative_value_observations = update_relative_value_ledger_clv(
                relative_value_rows,
                snapshot_signals,
            )
            if relative_value_rows or args.relative_value_ledger.exists():
                write_csv(
                    args.relative_value_ledger, relative_value_rows, RELATIVE_VALUE_LEDGER_COLUMNS
                )
    args.observations_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.observations_output, observations, OBSERVATION_COLUMNS)
    args.relative_value_observations_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(
        args.relative_value_observations_output,
        relative_value_observations,
        RELATIVE_VALUE_OBSERVATION_COLUMNS,
    )
    report = render_scorecard_clv_report(
        rows,
        observations,
        args.snapshot_dir,
        relative_value_rows=relative_value_rows,
        relative_value_observations=relative_value_observations,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(
        f"clv_observations={len([row for row in observations if row.get('status') == 'observed'])}"
    )
    print(
        "relative_value_clv_observations="
        f"{len([row for row in relative_value_observations if row.get('status') == 'observed'])}"
    )
    print(f"Wrote {args.output}")
    print(f"Wrote {args.observations_output}")
    print(f"Wrote {args.relative_value_observations_output}")


def read_ledger_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [normalize_ledger_row(row) for row in rows]


def normalize_ledger_row(row: dict[str, str]) -> dict[str, str]:
    if "paper_trade_id" in row:
        return {column: row.get(column, "") for column in LEDGER_COLUMNS}
    approved = row.get("approved") in {"True", "true", "1"}
    side = row.get("side", "")
    return {
        "paper_trade_id": row.get("signal_id", ""),
        "timestamp": row.get("timestamp", ""),
        "category": row.get("category", ""),
        "market_id": row.get("market_id", ""),
        "outcome": row.get("outcome", ""),
        "side": side,
        "execution_price": row.get("price", ""),
        "execution_price_source": (row.get("execution_price_source") or "none").upper(),
        "size": row.get("size", ""),
        "fair_probability_at_entry": row.get("fair_probability", ""),
        "adjusted_edge_at_entry": row.get("adjusted_edge", ""),
        "bid_at_entry": row.get("prediction_bid", ""),
        "ask_at_entry": row.get("prediction_ask", ""),
        "spread_at_entry": "",
        "liquidity_at_entry": row.get("available_size", ""),
        "fair_value_source": row.get("fair_value_source", ""),
        "risk_reason": row.get("reason", ""),
        "clv_5m": row.get("clv_5m", ""),
        "clv_30m": row.get("clv_30m", ""),
        "clv_1h": row.get("clv_1h", ""),
        "clv_status": "pending" if approved and side in {"BUY_YES", "SELL_YES"} else "skipped",
        **{
            key: row.get(key, "")
            for key in (
                "token_id",
                "event_id",
                "risk_event_id",
                "fees",
                "slippage",
                "cash_change",
                "realized_pnl",
                "fair_value_timestamp",
                "orderbook_timestamp",
                "net_entry_price",
                "fee_bps_at_entry",
                "slippage_bps_at_entry",
            )
        },
    }


def update_ledger_clv(
    ledger_rows: list[dict[str, str]],
    snapshot_signals: list[SnapshotSignal],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    observations: list[dict[str, str]] = []
    rows_by_market: dict[str, list[SnapshotSignal]] = {}
    for signal in snapshot_signals:
        rows_by_market.setdefault(signal.market_id, []).append(signal)
    for rows in rows_by_market.values():
        rows.sort(key=lambda row: row.timestamp)

    updated_rows: list[dict[str, str]] = []
    for row in ledger_rows:
        updated = dict(row)
        trade = parse_paper_trade(updated)
        if trade is None:
            if updated.get("clv_status") != "skipped":
                updated["clv_status"] = updated.get("clv_status") or "skipped"
            updated_rows.append(updated)
            continue
        any_observed = False
        for horizon, delta in HORIZONS.items():
            updated[f"clv_{horizon}"] = ""
            observation = observe_horizon(
                trade, rows_by_market.get(trade.market_id, []), horizon, delta
            )
            observations.append(observation)
            if observation["status"] == "observed":
                updated[f"clv_{horizon}"] = observation["clv"]
                any_observed = True
        updated["clv_status"] = "partial" if any_observed else "insufficient_followup_data"
        if all(updated.get(f"clv_{horizon}") not in {None, ""} for horizon in HORIZONS):
            updated["clv_status"] = "complete"
        updated_rows.append(updated)
    return updated_rows, observations


def read_relative_value_ledger_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [
        {column: row.get(column, "") for column in RELATIVE_VALUE_LEDGER_COLUMNS} for row in rows
    ]


def update_relative_value_ledger_clv(
    ledger_rows: list[dict[str, str]],
    snapshot_signals: list[SnapshotSignal],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    observations: list[dict[str, str]] = []
    rows_by_key: dict[tuple[str, str], list[SnapshotSignal]] = {}
    for signal in snapshot_signals:
        rows_by_key.setdefault((signal.market_id, signal.outcome), []).append(signal)
        rows_by_key.setdefault((signal.market_id, ""), []).append(signal)
    for rows in rows_by_key.values():
        rows.sort(key=lambda row: row.timestamp)

    updated_rows: list[dict[str, str]] = []
    for row in ledger_rows:
        updated = dict(row)
        if (
            updated.get("execution_status") != "FILLED_PAPER"
            or updated.get("clv_status") == "skipped"
            or updated.get("risk_decision") != "APPROVED"
        ):
            updated_rows.append(updated)
            continue
        timestamp = parse_time(updated.get("timestamp"))
        entry_cost = float_or_none(updated.get("package_entry_cost"))
        legs = parse_legs(updated.get("legs_json"))
        if timestamp is None or entry_cost is None or len(legs) < 2:
            updated["clv_status"] = "partial_or_missing_followup"
            updated_rows.append(updated)
            continue
        any_observed = False
        for horizon, delta in HORIZONS.items():
            observation = observe_relative_value_horizon(
                updated, legs, timestamp, entry_cost, rows_by_key, horizon, delta
            )
            observations.append(observation)
            if observation["status"] == "observed":
                updated[f"clv_{horizon}"] = observation["package_clv"]
                any_observed = True
        updated["clv_status"] = "partial" if any_observed else "partial_or_missing_followup"
        if all(updated.get(f"clv_{horizon}") not in {None, ""} for horizon in HORIZONS):
            updated["clv_status"] = "complete"
        updated_rows.append(updated)
    return updated_rows, observations


def observe_relative_value_horizon(
    row: dict[str, str],
    legs: list[dict[str, object]],
    timestamp: datetime,
    entry_cost: float,
    rows_by_key: dict[tuple[str, str], list[SnapshotSignal]],
    horizon: str,
    delta: timedelta,
) -> dict[str, str]:
    target = timestamp + delta
    package_exit = 0.0
    missing_legs: list[str] = []
    size = parse_float(row.get("max_trade_size")) or 1.0
    observed_at: datetime | None = None
    observation_times: list[datetime] = []
    unverified_depth = False
    for leg in legs:
        market_id = str(leg.get("market_id", ""))
        outcome = str(leg.get("outcome", ""))
        follow_up = first_follow_up(rows_by_key.get((market_id, outcome), []), target)
        if follow_up is None:
            missing_legs.append(f"{market_id}:{outcome}")
            continue
        if not leg.get("token_id") or follow_up.token_id != str(leg["token_id"]):
            missing_legs.append(f"{market_id}:{outcome}:wrong_token")
            continue
        observation_times.append(follow_up.quote_timestamp or follow_up.timestamp)
        observed_at = max(observed_at, follow_up.timestamp) if observed_at else follow_up.timestamp
        side = str(leg.get("side", ""))
        leg_size = parse_float(str(leg.get("size", ""))) or size * (
            parse_float(str(leg.get("size_ratio", ""))) or 1.0
        )
        depth = follow_up.bid_size if side == "BUY_YES" else follow_up.ask_size
        unverified_depth = unverified_depth or depth is None or depth < leg_size
        if side == "BUY_YES":
            if follow_up.bid is None:
                missing_legs.append(f"{market_id}:{outcome}:missing_bid")
                continue
            package_exit += follow_up.bid * leg_size
        elif side == "SELL_YES":
            if follow_up.ask is None:
                missing_legs.append(f"{market_id}:{outcome}:missing_ask")
                continue
            package_exit -= follow_up.ask * leg_size
        else:
            missing_legs.append(f"{market_id}:{outcome}:invalid_side")
    if observation_times and (max(observation_times) - min(observation_times)).total_seconds() > 5:
        missing_legs.append("asynchronous_leg_quotes_over_5s")
    if missing_legs:
        return relative_value_observation_row(
            row,
            horizon,
            observed_at,
            entry_cost,
            None,
            None,
            "partial_or_missing_followup",
            missing_legs,
        )
    package_clv = package_exit - entry_cost
    return relative_value_observation_row(
        row,
        horizon,
        observed_at,
        entry_cost,
        package_exit,
        package_clv,
        "quote_observed" if unverified_depth else "observed",
        [],
    )


def first_follow_up(rows: list[SnapshotSignal], target: datetime) -> SnapshotSignal | None:
    return next(
        (
            row
            for row in sorted(rows, key=lambda r: r.timestamp)
            if target <= row.timestamp <= target + timedelta(seconds=60)
            and target <= (row.quote_timestamp or row.timestamp) <= target + timedelta(seconds=60)
        ),
        None,
    )


def relative_value_observation_row(
    row: dict[str, str],
    horizon: str,
    observed_at: datetime | None,
    entry_cost: float,
    package_exit: float | None,
    package_clv: float | None,
    status: str,
    missing_legs: list[str],
) -> dict[str, str]:
    return {
        "paper_trade_id": row.get("paper_trade_id", ""),
        "candidate_id": row.get("candidate_id", ""),
        "relation_type": row.get("relation_type", ""),
        "horizon": horizon,
        "entry_timestamp": row.get("timestamp", ""),
        "observed_timestamp": "" if observed_at is None else observed_at.isoformat(),
        "package_entry_cost": format_float(entry_cost),
        "package_exit_value": format_float(package_exit),
        "package_clv": format_float(package_clv),
        "status": status,
        "missing_legs": "; ".join(missing_legs),
    }


def observe_horizon(
    trade: PaperTrade,
    rows: list[SnapshotSignal],
    horizon: str,
    delta: timedelta,
) -> dict[str, str]:
    target = trade.timestamp + delta
    if not trade.token_id:
        return observation_row(
            trade,
            horizon,
            None,
            None,
            "",
            None,
            "unverified_identity",
            None,
            "",
            None,
            "unverified_contract_identity",
        )
    exact = [
        r
        for r in rows
        if r.market_id == trade.market_id
        and r.outcome.casefold() == trade.outcome.casefold()
        and r.token_id == trade.token_id
    ]
    follow_up = first_follow_up(exact, target)
    if follow_up is None:
        return observation_row(
            trade,
            horizon,
            None,
            None,
            "",
            None,
            "unknown",
            None,
            "",
            None,
            "insufficient_followup_data",
        )
    later_value, source = follow_up.conservative_exit(trade.side)
    depth = follow_up.bid_size if trade.side == "BUY_YES" else follow_up.ask_size
    executable_depth = depth is not None and depth >= trade.size
    shadow_value, shadow_source = follow_up.clv_reference
    shadow_clv = compute_single_leg_clv(trade.side, trade.execution_price, shadow_value)
    if later_value is None:
        status = "shadow_observed" if shadow_clv is not None else "missing_followup_value"
        return observation_row(
            trade,
            horizon,
            follow_up.timestamp,
            None,
            "",
            None,
            "shadow_midpoint_clv" if shadow_clv is not None else "unknown",
            shadow_value,
            shadow_source,
            shadow_clv,
            status,
            follow_up=follow_up,
        )
    clv = compute_single_leg_clv(trade.side, trade.execution_price, later_value)
    return observation_row(
        trade,
        horizon,
        follow_up.timestamp,
        later_value,
        source,
        clv,
        "tradable_bid_ask" if executable_depth else "quote_only_no_depth",
        shadow_value,
        shadow_source,
        shadow_clv,
        "observed" if executable_depth else "quote_observed",
        follow_up=follow_up,
    )


def observation_row(
    trade: PaperTrade,
    horizon: str,
    observed_at: datetime | None,
    later_value: float | None,
    source: str,
    clv: float | None,
    clv_type: str,
    shadow_value: float | None,
    shadow_source: str,
    shadow_clv: float | None,
    status: str,
    follow_up: SnapshotSignal | None = None,
) -> dict[str, str]:
    net_markout = None
    if (
        clv is not None
        and trade.fees is not None
        and trade.slippage is not None
        and trade.exit_fee_bps is not None
        and trade.exit_slippage_bps is not None
        and trade.size > 0
        and later_value is not None
    ):
        net_markout = (
            clv
            - (trade.fees + trade.slippage) / trade.size
            - later_value * trade.exit_fee_bps / 10000
            - trade.exit_slippage_bps / 10000
        )
    return {
        "paper_trade_id": trade.paper_trade_id,
        "market_id": trade.market_id,
        "category": trade.category,
        "side": trade.side,
        "horizon": horizon,
        "entry_timestamp": trade.timestamp.isoformat(),
        "observed_timestamp": "" if observed_at is None else observed_at.isoformat(),
        "execution_price": format_float(trade.execution_price),
        "future_bid": format_float(None if follow_up is None else follow_up.bid),
        "future_ask": format_float(None if follow_up is None else follow_up.ask),
        "future_mid": format_float(None if follow_up is None else follow_up.mid),
        "future_fair_probability": format_float(
            None if follow_up is None else follow_up.fair_probability
        ),
        "later_value": format_float(later_value),
        "later_value_source": source,
        "clv": format_float(clv),
        "clv_type": clv_type,
        "shadow_value": format_float(shadow_value),
        "shadow_value_source": shadow_source,
        "shadow_clv": format_float(shadow_clv),
        "status": status,
        "outcome": trade.outcome,
        "token_id": trade.token_id,
        "target_timestamp": (trade.timestamp + HORIZONS[horizon]).isoformat(),
        "actual_horizon_seconds": ""
        if observed_at is None
        else str((observed_at - trade.timestamp).total_seconds()),
        "quote_timestamp": ""
        if follow_up is None
        else (follow_up.quote_timestamp or follow_up.timestamp).isoformat(),
        "exit_depth": format_float(
            None
            if follow_up is None
            else follow_up.bid_size
            if trade.side == "BUY_YES"
            else follow_up.ask_size
        ),
        "net_markout_estimate": format_float(net_markout),
        "cost_model": "entry-frozen fee/slippage placeholders, not realized PnL"
        if net_markout is not None
        else "missing entry cost provenance",
    }


def compute_single_leg_clv(
    side: str, entry_price: float, later_value: float | None
) -> float | None:
    if later_value is None:
        return None
    if side == "BUY_YES":
        return later_value - entry_price
    if side == "SELL_YES":
        return entry_price - later_value
    return None


def parse_paper_trade(row: dict[str, str]) -> PaperTrade | None:
    if row.get("clv_status") == "skipped":
        return None
    if row.get("side") not in {"BUY_YES", "SELL_YES"}:
        return None
    timestamp = parse_time(row.get("timestamp"))
    price = float_or_none(row.get("execution_price"))
    if timestamp is None or price is None:
        return None
    try:
        return PaperTrade(
            paper_trade_id=row.get("paper_trade_id", ""),
            timestamp=timestamp,
            category=row.get("category", ""),
            market_id=row.get("market_id", ""),
            outcome=row.get("outcome", ""),
            side=row.get("side", ""),
            execution_price=price,
            token_id=row.get("token_id", ""),
            size=strict_optional_float(row.get("size")) if row.get("size") else 1.0,
            fees=strict_optional_float(row.get("fees")),
            slippage=strict_optional_float(row.get("slippage")),
            exit_fee_bps=strict_optional_float(row.get("fee_bps_at_entry")),
            exit_slippage_bps=strict_optional_float(row.get("slippage_bps_at_entry")),
        )
    except (ValueError, TypeError) as exc:
        LOGGER.warning("invalid_paper_trade id=%s reason=%s", row.get("paper_trade_id"), exc)
        return None


def load_snapshot_signals(snapshot_dir: Path) -> list[SnapshotSignal]:
    return load_snapshot_signals_from_dirs([snapshot_dir])


def load_snapshot_signals_from_dirs(
    snapshot_dirs: list[Path],
    quarantine_path: Path | None = None,
) -> list[SnapshotSignal]:
    signals: list[SnapshotSignal] = []
    invalid: list[dict[str, str]] = []
    seen_paths: set[Path] = set()
    for snapshot_dir in snapshot_dirs:
        for path in snapshot_signal_paths(snapshot_dir):
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            for number, row in enumerate(read_csv_rows(path), start=2):
                try:
                    timestamp = parse_time(row.get("timestamp"))
                    if timestamp is None:
                        raise ValueError("missing observation timestamp")
                    signal = SnapshotSignal(
                        timestamp=timestamp,
                        category=row.get("category", ""),
                        market_id=row.get("market_id", ""),
                        outcome=row.get("outcome", ""),
                        fair_probability=strict_optional_float(row.get("fair_probability")),
                        mid=strict_optional_float(row.get("mid")),
                        bid=strict_optional_float(row.get("bid") or row.get("prediction_bid")),
                        ask=strict_optional_float(row.get("ask") or row.get("prediction_ask")),
                        token_id=row.get("token_id", ""),
                        quote_timestamp=parse_time(row.get("orderbook_timestamp"))
                        or datetime.fromtimestamp(0, UTC),
                        bid_size=strict_optional_float(row.get("book_bid_size")),
                        ask_size=strict_optional_float(row.get("book_ask_size")),
                    )
                    signals.append(signal)
                except (ValueError, TypeError) as exc:
                    invalid.append({"path": str(path), "row": str(number), "reason": str(exc)})
                    LOGGER.warning(
                        "quarantined_snapshot path=%s row=%d reason=%s", path, number, exc
                    )
    if quarantine_path:
        quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        write_csv(quarantine_path, invalid, ["path", "row", "reason"])
    signals.sort(key=lambda row: row.timestamp)
    return signals


def snapshot_signal_paths(snapshot_dir: Path) -> list[Path]:
    index_path = snapshot_dir / "snapshot_index.csv"
    if index_path.exists():
        rows = read_csv_rows(index_path)
        paths = [
            resolve_snapshot_path(snapshot_dir, row["signals_path"])
            for row in rows
            if row.get("signals_path")
        ]
        return [path for path in paths if path.exists()]
    return sorted(snapshot_dir.glob("iteration_*/signals.csv"))


def resolve_snapshot_path(snapshot_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or path.exists():
        return path
    return snapshot_dir / path


def render_scorecard_clv_report(
    ledger_rows: list[dict[str, str]],
    observations: list[dict[str, str]],
    snapshot_dir: Path,
    relative_value_rows: list[dict[str, str]] | None = None,
    relative_value_observations: list[dict[str, str]] | None = None,
) -> str:
    observed = [row for row in observations if row.get("status") == "observed"]
    values = [parse_float(row.get("clv")) for row in observed if row.get("horizon") == "5m"]
    trades = [row for row in ledger_rows if parse_paper_trade(row) is not None]
    relative_value_rows = relative_value_rows or []
    relative_value_observations = relative_value_observations or []
    rv_observed = [row for row in relative_value_observations if row.get("status") == "observed"]
    rv_values = [parse_float(row.get("package_clv")) for row in rv_observed]
    rv_trades = [
        row
        for row in relative_value_rows
        if row.get("risk_decision") == "APPROVED" and row.get("execution_status") == "FILLED_PAPER"
    ]
    lines = [
        "# EdgeLab CLV Report",
        "",
        "CLV is separated by evidence type. Tradable single-leg CLV uses paper fills at bid/ask. "
        "Follow-up single-leg CLV uses conservative executable exits: BUY exits at future bid and SELL exits at future ask. "
        "Midpoint/model-fair follow-ups are recorded only as shadow fields and are not mixed into tradable CLV. "
        "Relative-value package CLV uses conservative package exits. Shadow and theoretical-only CLV are not mixed into these metrics.",
        "Overall single-leg averages use only the declared 5m horizon. Other horizons are descriptive and not independent samples. Gross markouts are not net realized PnL; net_markout_estimate uses configured placeholder exit costs.",
        "",
        f"- Paper trades parsed: {len(trades)}",
        f"- Relative-value paper trades parsed: {len(rv_trades)}",
        f"- Snapshot directory: `{snapshot_dir}`",
        f"- Single-leg tradable CLV observations: {len(observed)}",
        f"- Single-leg average CLV: {format_optional(average(values))}",
        f"- Single-leg median CLV: {format_optional(median(values) if values else None)}",
        f"- Relative-value package CLV observations: {len(rv_observed)}",
        f"- Relative-value average package CLV: {format_optional(average(rv_values))}",
        f"- Relative-value median package CLV: {format_optional(median(rv_values) if rv_values else None)}",
        "",
        "## Single-Leg Tradable CLV",
        "",
    ]
    if not trades:
        lines.extend(
            [
                "No single-leg CLV is reported yet.",
                "",
                "Reason: no single-leg paper trades exist in the ledger.",
            ]
        )
    elif not observed:
        lines.extend(
            [
                "No single-leg CLV is reported yet.",
                "",
                "Reason: insufficient_followup_data.",
                "",
                "Executable markout requires a later exact-token bid/ask with valid timing and sufficient depth; model fair value and midpoint are shadow diagnostics only.",
            ]
        )
    else:
        by_horizon: dict[str, list[float]] = {}
        for row in observed:
            by_horizon.setdefault(row["horizon"], []).append(parse_float(row.get("clv")))
        lines.extend(["| horizon | observations | average_clv |", "| --- | --- | --- |"])
        for horizon in HORIZONS:
            values_for_horizon = by_horizon.get(horizon, [])
            lines.append(
                f"| {horizon} | {len(values_for_horizon)} | {format_optional(average(values_for_horizon))} |"
            )
        lines.extend(
            [
                "",
                "Executable gross markouts use the SAME outcome/token, observed within 60 seconds of the target horizon, at bid for buys / ask for sales with sufficient exit depth. They are not net realized PnL. Quote-only and midpoint/fair-value shadow diagnostics are separate. Missing horizons are not inferred.",
            ]
        )
    lines.extend(["", "## Relative-Value Package CLV", ""])
    if not rv_trades:
        lines.append(
            "No relative-value package CLV exists because no package passed leg validation and paper execution."
        )
    elif not rv_observed:
        lines.extend(
            [
                "No relative-value package CLV is reported yet.",
                "",
                "Reason: partial_or_missing_followup.",
                "",
                "The report refuses to fabricate package CLV when any leg lacks a conservative follow-up quote.",
            ]
        )
    else:
        by_horizon = {}
        for row in rv_observed:
            by_horizon.setdefault(row["horizon"], []).append(parse_float(row.get("package_clv")))
        lines.extend(["| horizon | observations | average_package_clv |", "| --- | --- | --- |"])
        for horizon in HORIZONS:
            values_for_horizon = by_horizon.get(horizon, [])
            lines.append(
                f"| {horizon} | {len(values_for_horizon)} | {format_optional(average(values_for_horizon))} |"
            )
    lines.extend(
        [
            "",
            "## Excluded Evidence Types",
            "",
            "- shadow_clv: research-only watchlist movement, not included here",
            "- theoretical_only_clv: not computed as tradable evidence",
        ]
    )
    return "\n".join(lines)


def render_live_clv_report(
    approved_trade_count: int,
    trades: list[TradeRow],
    observations: list[CLVObservation],
    snapshot_dbs: list[Path],
) -> str:
    lines = [
        "# EdgeLab CLV Report",
        "",
        f"- Approved paper trades in ledger: {approved_trade_count}",
        f"- Approved paper trades parsed: {len(trades)}",
        f"- Snapshot DBs searched: {len([path for path in snapshot_dbs if path and path.exists()])}",
        f"- Follow-up CLV observations: {len(observations)}",
        "",
    ]
    if not trades:
        lines.extend(
            [
                "No CLV is reported yet.",
                "",
                "Reason: no approved BUY_YES or SELL_YES paper trades exist in the ledger.",
            ]
        )
        return "\n".join(lines)
    if not observations:
        lines.extend(
            [
                "No CLV is reported yet.",
                "",
                "Reason: insufficient follow-up data.",
                "",
                "The report refuses to fabricate CLV without a later fair-value snapshot.",
            ]
        )
        return "\n".join(lines)
    values = [observation.clv for observation in observations]
    lines.extend(
        [
            f"- Average CLV: {average(values):.4f}",
            f"- Median CLV: {median(values):.4f}",
        ]
    )
    return "\n".join(lines)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    atomic_write_csv(path, rows, columns)


def write_ledger_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_lock(path):
        existing = {r["paper_trade_id"]: r for r in read_ledger_rows(path)}
        for row in rows:
            key = row["paper_trade_id"]
            if key in existing:
                existing[key].update(
                    {c: row.get(c, "") for c in LEDGER_COLUMNS if c.startswith("clv_")}
                )
            else:
                existing[key] = row
        write_csv(path, list(existing.values()), LEDGER_COLUMNS)


def update_single_leg_ledger(
    path: Path,
    snapshots: list[SnapshotSignal],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    with ledger_lock(path):
        rows, observations = update_ledger_clv(read_ledger_rows(path), snapshots)
        store = ObservationStore(path.with_suffix(".clv.sqlite"))
        try:
            store.append(observations)
        finally:
            store.close()
        write_csv(path, rows, LEDGER_COLUMNS)
        return rows, observations


def write_relative_value_ledger_rows(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows and not path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_lock(path):
        existing = {r["paper_trade_id"]: r for r in read_relative_value_ledger_rows(path)}
        for row in rows:
            key = row["paper_trade_id"]
            if key in existing:
                existing[key].update(
                    {
                        c: row.get(c, "")
                        for c in RELATIVE_VALUE_LEDGER_COLUMNS
                        if c.startswith("clv_")
                    }
                )
            else:
                existing[key] = row
        write_csv(path, list(existing.values()), RELATIVE_VALUE_LEDGER_COLUMNS)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def parse_float(value: str | None) -> float:
    parsed = float_or_none(value)
    return 0.0 if parsed is None else parsed


def float_or_none(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def strict_optional_float(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite observation value")
    return result


def parse_legs(value: str | None) -> list[dict[str, object]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def format_float(value: float | None) -> str:
    return "" if value is None else f"{value:.10g}"


def format_optional(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


if __name__ == "__main__":
    main()
