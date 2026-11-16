#!/usr/bin/env python
from __future__ import annotations

import argparse
from pathlib import Path

import _bootstrap  # noqa: F401

from edgelab.research.pipeline import MockResearchRun


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect EdgeLab mock snapshots")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("reports/mock_snapshot_summary.md"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.mock:
        raise SystemExit("Only --mock mode is enabled in EdgeLab v0.1")
    run = MockResearchRun()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(
            [
                "# Mock Snapshot Summary",
                "",
                f"- Markets: {len(run.markets)}",
                f"- Order books: {len(run.orderbooks)}",
                f"- External anchors: {len(run.anchors)}",
            ]
        ),
        encoding="utf-8",
    )
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
