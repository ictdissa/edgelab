#!/usr/bin/env python
"""Register and run bounded, research-only prospective collection cycles."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.research.experiment import ROOT, SAFE_SETTINGS, ResearchProtocol, verify_run
from edgelab.research.study import StudyDesign, StudyRunner, register_study
from edgelab.research.study_sources import LiveStudySource, SyntheticStudySource


def mock_study(directory: Path) -> None:
    """Deliberately manufactured edge to test accounting/follow-ups, not a backtest."""
    start = datetime(2026, 10, 13, 12, tzinfo=UTC)
    protocol = ResearchProtocol.model_validate_json(
        (ROOT / "research/protocol_soccer_1x2.json").read_text()
    ).model_copy(
        update={
            "protocol_id": "synthetic-lifecycle-v1",
            "leagues": ["Synthetic Premier League"],
            "bookmaker_sources": ["SyntheticBook"],
            "evaluation_start": start,
            "evaluation_end": start + timedelta(minutes=10),
        }
    )
    design = StudyDesign(
        provider="synthetic", league=protocol.leagues[0], bookmakers=protocol.bookmaker_sources
    )
    spec = dict(
        review_status="reviewed",
        period="regulation_time",
        includes_overtime=False,
        draw_is_outcome=True,
        cancellation_rule="SYNTHETIC_IDENTICAL_RULES",
        evidence_reference="synthetic lifecycle test, NOT actual bookmaker rules",
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
    # Every field is explicit: .env cannot change this demonstration's numeric defaults.
    settings = Settings(_env_file=None, **{k: f.default for k, f in Settings.model_fields.items()})
    register_study(directory, protocol, design, settings, registry, start - timedelta(seconds=1))
    clock_time = start
    runner = StudyRunner(directory, clock=lambda: clock_time)
    source = SyntheticStudySource(runner)
    for seconds in range(0, 4261, 60):
        clock_time = start + timedelta(seconds=seconds)
        runner.run_once(source)
    clock_time = runner.final_at + timedelta(seconds=1)
    runner.run_once(source)
    print(
        f"Synthetic lifecycle complete. Not performance evidence. Report: {directory / 'report.md'}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--mock", action="store_true")
    modes.add_argument(
        "--pilot", action="store_true", help="One bounded feasibility observation; no trades"
    )
    modes.add_argument("--register", action="store_true")
    modes.add_argument("--run", action="store_true")
    modes.add_argument("--verify", action="store_true")
    modes.add_argument(
        "--reparse-pilot", type=Path, help="Offline only: reparse recorded CLOB responses"
    )
    parser.add_argument("--study-dir", type=Path, required=True)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--design", type=Path)
    parser.add_argument("--settlement-registry", type=Path)
    parser.add_argument(
        "--events-from", type=Path, help="Pilot only: reuse recent hash-bound fixture identities"
    )
    parser.add_argument("--settings-file", type=Path, help="Optional JSON of SAFE_SETTINGS only")
    parser.add_argument(
        "--refresh", action="store_true", help="Explicitly allow budgeted network requests"
    )
    parser.add_argument(
        "--archive-only",
        action="store_true",
        help="Verify historical source archive, not current source",
    )
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING"], default="WARNING")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")
    if args.refresh and not (args.run or args.pilot):
        parser.error("--refresh requires --run or --pilot")
    if args.events_from and not args.pilot:
        parser.error("--events-from requires --pilot")
    if args.reparse_pilot:
        if (
            args.design
            or args.protocol
            or args.settlement_registry
            or args.events_from
            or args.settings_file
            or args.archive_only
        ):
            parser.error("--reparse-pilot uses recorded inputs only")
        from edgelab.research.pilot import reparse_pilot

        print(json.dumps(reparse_pilot(args.reparse_pilot, args.study_dir), indent=2))
    elif args.pilot:
        if args.design is None:
            parser.error("--pilot requires --design")
        if args.protocol or args.settlement_registry or args.settings_file or args.archive_only:
            parser.error("--pilot is a feasibility observation, not a study registration")
        from edgelab.research.pilot import run_pilot

        summary = run_pilot(
            args.study_dir,
            StudyDesign.model_validate_json(args.design.read_text()),
            args.refresh,
            args.events_from,
        )
        print(json.dumps(summary, indent=2))
        print(f"Feasibility only; no orders/trades. Report: {args.study_dir / 'report.md'}")
    elif args.mock:
        mock_study(args.study_dir)
    elif args.register:
        if args.protocol is None or args.design is None:
            parser.error("--register requires --protocol and --design")
        protocol = ResearchProtocol.model_validate_json(args.protocol.read_text())
        design = StudyDesign.model_validate_json(args.design.read_text())
        supplied = json.loads(args.settings_file.read_text()) if args.settings_file else {}
        if not isinstance(supplied, dict) or set(supplied) - set(SAFE_SETTINGS):
            raise ValueError("settings file must use credential-free SAFE_SETTINGS only")
        defaults = {name: field.default for name, field in Settings.model_fields.items()}
        settings = Settings(_env_file=None, **{**defaults, **supplied})
        registry = (
            json.loads(args.settlement_registry.read_text()) if args.settlement_registry else []
        )
        if not isinstance(registry, list):
            raise ValueError("registry must be a list")
        path = register_study(args.study_dir, protocol, design, settings, registry)
        print(f"Registered research-only protocol: {path}. No requests made.")
    elif args.verify:
        verify_run(args.study_dir, check_source=not args.archive_only)
        # Cycles are source-independent hash checks; archive-only must not load current semantics.
        if not args.archive_only:
            StudyRunner(args.study_dir).history()
        print(
            "Registration verified"
            + ("; cycle chain verified" if not args.archive_only else "; archive integrity only")
        )
    else:
        runner = StudyRunner(args.study_dir)
        status = runner.run_once(LiveStudySource(runner, refresh=args.refresh))
        print(f"Study status: {status}. Network refresh: {args.refresh}. Live orders: disabled.")


if __name__ == "__main__":
    main()
