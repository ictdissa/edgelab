#!/usr/bin/env python
"""Credential-free reproducible run or future protocol registration; no networking."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.data.external_sources.mock_provider import SAMPLES_DIR, MockProvider
from edgelab.execution.paper_trader import write_ledger
from edgelab.matching.event_resolver import write_event_match_audit
from edgelab.research.experiment import ROOT, SAFE_SETTINGS, ResearchProtocol, seal_run, verify_run
from edgelab.research.pipeline import MockResearchRun, write_signals_csv


def reproduce_mock(directory: Path, protocol_path: Path, replay_from: Path | None = None) -> Path:
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("run directory must be empty; never overwrite a sealed experiment")
    directory.mkdir(parents=True, exist_ok=True)
    prior = verify_run(replay_from) if replay_from else None
    if prior and prior["evidence_type"] != "synthetic_integration":
        raise ValueError("this replay command accepts only synthetic integration runs")
    protocol = (
        ResearchProtocol.model_validate(prior["protocol"])
        if prior
        else ResearchProtocol.model_validate_json(protocol_path.read_text())
    )
    # Explicit defaults prevent a private .env from silently changing a public example.
    defaults = {name: Settings.model_fields[name].default for name in SAFE_SETTINGS}
    settings = Settings(_env_file=None, **(prior["settings"] if prior else defaults))
    samples = replay_from / "inputs" if replay_from else SAMPLES_DIR
    run = MockResearchRun(settings, MockProvider(samples))
    signals = run.signals()
    decisions = run.paper_trades(signals)
    (directory / "inputs").mkdir()
    for source in sorted(samples.glob("*.json")):
        shutil.copyfile(source, directory / "inputs" / source.name)
    (directory / "protocol.json").write_text(protocol.model_dump_json(indent=2))
    write_signals_csv(directory / "signals.csv", signals)
    write_ledger(directory / "decisions.csv", decisions, append=False)
    write_event_match_audit(directory / "event_match_audit.csv", run.matches())
    report = run.report().replace("reports/event_match_audit.csv", "event_match_audit.csv")
    report += "\n\n## Evidence Status\n\nSYNTHETIC_INTEGRATION_ONLY. This retrospective smoke replay is not preregistered research.\n\n"
    report += "No validated live CLV, calibration, capacity or net-profitability result exists. A future registration requires explicit dates before evaluation starts.\n"
    (directory / "report.md").write_text(report)
    artifacts = sorted(
        p for p in directory.rglob("*") if p.is_file() and not p.name.endswith(".csv.lock")
    )
    return seal_run(directory, protocol, settings, "synthetic_integration", artifacts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true")
    mode.add_argument("--register", action="store_true")
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--replay-from", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, default=ROOT / "research/protocol_soccer_1x2.json")
    parser.add_argument(
        "--archive-only",
        action="store_true",
        help="Verify archived integrity without approving a different source tree for replay",
    )
    args = parser.parse_args()
    if args.verify:
        manifest = verify_run(args.output_dir, check_source=not args.archive_only)
        print(f"Verified {manifest['evidence_type']}; no live trading or profitability claim.")
        if args.archive_only:
            print(
                "Archived integrity only; replay must use the recorded source_code.zip and environment."
            )
    elif args.mock or args.replay_from:
        print(reproduce_mock(args.output_dir, args.protocol, args.replay_from))
    else:
        if args.output_dir.exists() and any(args.output_dir.iterdir()):
            raise ValueError("registration directory must be empty")
        protocol = ResearchProtocol.model_validate_json(args.protocol.read_text())
        print(seal_run(args.output_dir, protocol, Settings(), "prospective_registration", []))
    print(json.dumps({"live_trading": False, "network_requests": 0}))


if __name__ == "__main__":
    main()
