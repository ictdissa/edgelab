"""Fail-closed weather adapter interface pending settlement-aligned calibration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from edgelab.config.settings import Settings
from edgelab.data.cache import HttpGet
from edgelab.data.schemas import ExternalAnchor, PredictionMarket

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForecastSnapshot:
    location: str
    forecast_mean: float
    forecast_std: float
    unit: str
    timestamp: datetime


class WeatherForecastProvider:
    """No live weather probabilities are issued from arbitrary forecast-day values.

    Enabling this adapter requires verified settlement station/date/statistic/units
    and out-of-sample forecast errors for the exact target. The current implementation
    returns no anchors. Rainfall additionally requires a nonnegative distribution.
    """

    def __init__(
        self, settings: Settings, http_get: HttpGet | None = None, refresh: bool = False
    ) -> None:
        self.settings = settings

    def fetch_anchors_for_markets(self, markets: list[PredictionMarket]) -> list[ExternalAnchor]:
        LOGGER.warning(
            "weather_pricing_disabled station/date/unit mapping and error calibration required"
        )
        return []
