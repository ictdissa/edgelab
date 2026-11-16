#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a reusable SQLite research dataset from EdgeLab artifacts")
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        action="append",
        default=[],
        help="Snapshot directory to include. Can be passed more than once.",
    )
    parser.add_argument("--ledger", type=Path, default=Path("data/paper_trades/ledger.csv"))
    parser.add_argument(
        "--relative-value-ledger",
        type=Path,
        default=Path("data/paper_trades/relative_value_ledger.csv"),
    )
    parser.add_argument("--clv-observations", type=Path, default=Path("reports/clv_observations.csv"))
    parser.add_argument(
        "--relative-value-clv-observations",
        type=Path,
        default=Path("reports/relative_value_clv_observations.csv"),
    )
    parser.add_argument("--output", type=Path, default=Path("data/research_dataset.sqlite"))
    parser.add_argument("--report", type=Path, default=Path("reports/research_dataset.md"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    snapshot_dirs = args.snapshot_dir or [Path("data/snapshots_campaign")]
    counts = build_dataset(
        snapshot_dirs=snapshot_dirs,
        ledger=args.ledger,
        relative_value_ledger=args.relative_value_ledger,
        clv_observations=args.clv_observations,
        relative_value_clv_observations=args.relative_value_clv_observations,
        output=args.output,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(args.output, snapshot_dirs, counts), encoding="utf-8")
    print(f"dataset={args.output}")
    for key, value in counts.items():
        print(f"{key}={value}")


def build_dataset(
    *,
    snapshot_dirs: list[Path],
    ledger: Path,
    relative_value_ledger: Path,
    clv_observations: Path,
    relative_value_clv_observations: Path,
    output: Path,
) -> dict[str, int]:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    with sqlite3.connect(output) as conn:
        create_schema(conn)
        counts = {
            "snapshots": insert_snapshots(conn, snapshot_dirs),
            "signals": insert_snapshot_csv(conn, snapshot_dirs, "signals", "signals.csv"),
            "freshness": insert_snapshot_csv(conn, snapshot_dirs, "freshness", "freshness.csv"),
            "relative_value_candidates": insert_snapshot_csv(
                conn,
                snapshot_dirs,
                "relative_value_candidates",
                "relative_value_candidates.csv",
            ),
            "edge_sanity_warnings": insert_snapshot_csv(
                conn,
                snapshot_dirs,
                "edge_sanity_warnings",
                "edge_sanity_warnings.csv",
            ),
            "paper_trades": insert_artifact_csv(conn, "paper_trades", ledger),
            "relative_value_paper_trades": insert_artifact_csv(
                conn,
                "relative_value_paper_trades",
                relative_value_ledger,
            ),
            "clv_observations": insert_artifact_csv(conn, "clv_observations", clv_observations),
            "relative_value_clv_observations": insert_artifact_csv(
                conn,
                "relative_value_clv_observations",
                relative_value_clv_observations,
            ),
        }
        conn.execute(
            "INSERT INTO dataset_manifest(created_at, snapshot_dirs_json, counts_json, live_trading_enabled) VALUES (?, ?, ?, ?)",
            (
                datetime.now(UTC).isoformat(),
                json.dumps([str(path) for path in snapshot_dirs], sort_keys=True),
                json.dumps(counts, sort_keys=True),
                "false",
            ),
        )
    return counts


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE dataset_manifest (
            created_at TEXT NOT NULL,
            snapshot_dirs_json TEXT NOT NULL,
            counts_json TEXT NOT NULL,
            live_trading_enabled TEXT NOT NULL
        );

        CREATE TABLE snapshots (
            snapshot_dir TEXT NOT NULL,
            run_id TEXT,
            iteration TEXT,
            timestamp TEXT,
            categories TEXT,
            summary_json TEXT NOT NULL
        );

        CREATE TABLE signals (
            snapshot_dir TEXT NOT NULL,
            source_path TEXT NOT NULL,
            signal_id TEXT,
            timestamp TEXT,
            category TEXT,
            market_id TEXT,
            outcome TEXT,
            best_side TEXT,
            best_adjusted_edge REAL,
            final_decision TEXT,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE freshness (
            snapshot_dir TEXT NOT NULL,
            source_path TEXT NOT NULL,
            category TEXT,
            source TEXT,
            status TEXT,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE relative_value_candidates (
            snapshot_dir TEXT NOT NULL,
            source_path TEXT NOT NULL,
            candidate_id TEXT,
            status TEXT,
            paper_trade_eligible TEXT,
            relative_value_source TEXT,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE edge_sanity_warnings (
            snapshot_dir TEXT NOT NULL,
            source_path TEXT NOT NULL,
            warning_code TEXT,
            blocks_risk_approval TEXT,
            market_id TEXT,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE paper_trades (
            source_path TEXT NOT NULL,
            paper_trade_id TEXT,
            market_id TEXT,
            side TEXT,
            execution_status TEXT,
            clv_status TEXT,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE relative_value_paper_trades (
            source_path TEXT NOT NULL,
            paper_trade_id TEXT,
            candidate_id TEXT,
            execution_status TEXT,
            clv_status TEXT,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE clv_observations (
            source_path TEXT NOT NULL,
            paper_trade_id TEXT,
            market_id TEXT,
            horizon TEXT,
            clv REAL,
            status TEXT,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE relative_value_clv_observations (
            source_path TEXT NOT NULL,
            paper_trade_id TEXT,
            candidate_id TEXT,
            horizon TEXT,
            package_clv REAL,
            status TEXT,
            raw_json TEXT NOT NULL
        );
        """
    )


def insert_snapshots(conn: sqlite3.Connection, snapshot_dirs: list[Path]) -> int:
    count = 0
    for snapshot_dir in snapshot_dirs:
        for summary_path in sorted(snapshot_dir.glob("iteration_*/summary.json")):
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            conn.execute(
                """
                INSERT INTO snapshots(snapshot_dir, run_id, iteration, timestamp, categories, summary_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(summary_path.parent),
                    str(summary.get("run_id", "")),
                    summary_path.parent.name.removeprefix("iteration_"),
                    str(summary.get("timestamp", "")),
                    ",".join(str(item) for item in summary.get("categories", [])),
                    json.dumps(summary, sort_keys=True),
                ),
            )
            count += 1
    return count


def insert_snapshot_csv(conn: sqlite3.Connection, snapshot_dirs: list[Path], table: str, filename: str) -> int:
    count = 0
    for snapshot_dir in snapshot_dirs:
        for path in sorted(snapshot_dir.glob(f"iteration_*/{filename}")):
            for row in read_csv_rows(path):
                insert_snapshot_row(conn, table, path, row)
                count += 1
    return count


def insert_snapshot_row(conn: sqlite3.Connection, table: str, path: Path, row: dict[str, str]) -> None:
    raw_json = json.dumps(row, sort_keys=True)
    if table == "signals":
        conn.execute(
            """
            INSERT INTO signals(snapshot_dir, source_path, signal_id, timestamp, category, market_id, outcome, best_side, best_adjusted_edge, final_decision, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(path.parent),
                str(path),
                row.get("signal_id", ""),
                row.get("timestamp", ""),
                row.get("category", ""),
                row.get("market_id", ""),
                row.get("outcome", ""),
                row.get("best_side", ""),
                parse_optional_float(row.get("best_adjusted_edge") or row.get("adjusted_edge")),
                row.get("final_decision", ""),
                raw_json,
            ),
        )
    elif table == "freshness":
        conn.execute(
            "INSERT INTO freshness(snapshot_dir, source_path, category, source, status, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
            (str(path.parent), str(path), row.get("category", ""), row.get("source", ""), row.get("status", ""), raw_json),
        )
    elif table == "relative_value_candidates":
        conn.execute(
            """
            INSERT INTO relative_value_candidates(snapshot_dir, source_path, candidate_id, status, paper_trade_eligible, relative_value_source, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(path.parent),
                str(path),
                row.get("candidate_id", ""),
                row.get("status", ""),
                row.get("paper_trade_eligible", ""),
                row.get("relative_value_source", ""),
                raw_json,
            ),
        )
    elif table == "edge_sanity_warnings":
        conn.execute(
            """
            INSERT INTO edge_sanity_warnings(snapshot_dir, source_path, warning_code, blocks_risk_approval, market_id, raw_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(path.parent),
                str(path),
                row.get("warning_code", ""),
                row.get("blocks_risk_approval", ""),
                row.get("market_id", ""),
                raw_json,
            ),
        )


def insert_artifact_csv(conn: sqlite3.Connection, table: str, path: Path) -> int:
    rows = read_csv_rows(path)
    for row in rows:
        raw_json = json.dumps(row, sort_keys=True)
        if table == "paper_trades":
            conn.execute(
                """
                INSERT INTO paper_trades(source_path, paper_trade_id, market_id, side, execution_status, clv_status, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(path),
                    row.get("paper_trade_id", ""),
                    row.get("market_id", ""),
                    row.get("side", ""),
                    "FILLED_PAPER" if row.get("clv_status") != "skipped" else "SKIPPED",
                    row.get("clv_status", ""),
                    raw_json,
                ),
            )
        elif table == "relative_value_paper_trades":
            conn.execute(
                """
                INSERT INTO relative_value_paper_trades(source_path, paper_trade_id, candidate_id, execution_status, clv_status, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(path),
                    row.get("paper_trade_id", ""),
                    row.get("candidate_id", ""),
                    row.get("execution_status", ""),
                    row.get("clv_status", ""),
                    raw_json,
                ),
            )
        elif table == "clv_observations":
            conn.execute(
                """
                INSERT INTO clv_observations(source_path, paper_trade_id, market_id, horizon, clv, status, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(path),
                    row.get("paper_trade_id", ""),
                    row.get("market_id", ""),
                    row.get("horizon", ""),
                    parse_optional_float(row.get("clv")),
                    row.get("status", ""),
                    raw_json,
                ),
            )
        elif table == "relative_value_clv_observations":
            conn.execute(
                """
                INSERT INTO relative_value_clv_observations(source_path, paper_trade_id, candidate_id, horizon, package_clv, status, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(path),
                    row.get("paper_trade_id", ""),
                    row.get("candidate_id", ""),
                    row.get("horizon", ""),
                    parse_optional_float(row.get("package_clv")),
                    row.get("status", ""),
                    raw_json,
                ),
            )
    return len(rows)


def render_report(output: Path, snapshot_dirs: list[Path], counts: dict[str, int]) -> str:
    lines = [
        "# EdgeLab Research Dataset",
        "",
        "This SQLite dataset packages research artifacts for reproducible analysis. It contains paper and research evidence only; live trading remains disabled.",
        "",
        f"- dataset: `{output}`",
        f"- snapshot dirs: `{', '.join(str(path) for path in snapshot_dirs)}`",
        "- live trading enabled: `false`",
        "",
        "## Table Counts",
        "",
    ]
    lines.extend(markdown_table(["table", "rows"], [[key, str(value)] for key, value in counts.items()]))
    lines.extend(
        [
            "",
            "## Example Queries",
            "",
            "```sql",
            "SELECT category, COUNT(*) FROM signals GROUP BY category;",
            "SELECT status, COUNT(*) FROM clv_observations GROUP BY status;",
            "SELECT warning_code, COUNT(*) FROM edge_sanity_warnings GROUP BY warning_code ORDER BY COUNT(*) DESC;",
            "```",
        ]
    )
    return "\n".join(lines)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_optional_float(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
