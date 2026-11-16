"""Calibration helpers."""

from __future__ import annotations
import math


def brier_score(probability: float, outcome: int) -> float:
    if not math.isfinite(probability) or not 0 <= probability <= 1 or outcome not in {0, 1}:
        raise ValueError("Brier score requires a finite probability and binary resolution")
    return (probability - outcome) ** 2
