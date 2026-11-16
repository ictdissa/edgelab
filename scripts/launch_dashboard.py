#!/usr/bin/env python
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import _bootstrap  # noqa: F401


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch SharpEdge Streamlit dashboard")
    parser.add_argument("--mock", action="store_true", help="Use bundled sample data")
    args = parser.parse_args()
    env = os.environ.copy()
    if args.mock:
        env["SHARPEDGE_MOCK"] = "true"
    app_path = Path(__file__).resolve().parents[1] / "sharpedge" / "dashboard" / "app.py"
    command = [sys.executable, "-m", "streamlit", "run", str(app_path)]
    if args.mock:
        command.extend(["--", "--mock"])
    raise SystemExit(subprocess.call(command, env=env))


if __name__ == "__main__":
    main()
