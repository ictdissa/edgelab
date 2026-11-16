"""Resolution collection interface and offline sample implementation."""

from __future__ import annotations

import json
from pathlib import Path

from sharpedge.data.schemas import Resolution

SAMPLES_DIR = Path(__file__).resolve().parent / "samples"


class MockResolutionCollector:
    def __init__(self, sample_path: Path | None = None) -> None:
        self.sample_path = sample_path or SAMPLES_DIR / "resolutions.json"

    def fetch_resolutions(self) -> list[Resolution]:
        with self.sample_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload.get("resolutions", payload) if isinstance(payload, dict) else payload
        return [Resolution(**row) for row in rows]
