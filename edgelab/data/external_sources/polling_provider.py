"""Polling snapshot schema; no live polling feed is configured."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PollingSnapshot:
    race: str
    candidate: str
    probability: float
    timestamp: datetime
