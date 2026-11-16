"""Calibration helpers for realized outcomes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CalibrationBin:
    lower: float
    upper: float
    count: int
    predicted: float
    realized: float


def calibration_bins(
    predicted: list[float], realized: list[float], bins: int = 10
) -> list[CalibrationBin]:
    if len(predicted) != len(realized):
        raise ValueError("predicted and realized must have same length")
    output: list[CalibrationBin] = []
    for idx in range(bins):
        lower = idx / bins
        upper = (idx + 1) / bins
        rows = [
            (p, r)
            for p, r in zip(predicted, realized, strict=False)
            if lower <= p < upper or (idx == bins - 1 and p == 1.0)
        ]
        if not rows:
            continue
        output.append(
            CalibrationBin(
                lower=lower,
                upper=upper,
                count=len(rows),
                predicted=sum(p for p, _ in rows) / len(rows),
                realized=sum(r for _, r in rows) / len(rows),
            )
        )
    return output
