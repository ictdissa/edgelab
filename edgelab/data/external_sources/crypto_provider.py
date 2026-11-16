"""Crypto fair-value anchor interfaces."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from edgelab.config.settings import Settings
from edgelab.data.cache import CachedHttpClient, HttpGet, RollingRateLimiter
from edgelab.data.schemas import ExternalAnchor, PredictionMarket
from edgelab.matching.payoffs import crypto_payoff


@dataclass(frozen=True)
class CryptoSnapshot:
    asset: str
    spot: float
    volatility: float
    timestamp: datetime


LOGGER = logging.getLogger(__name__)

ASSET_IDS = {"btc": "bitcoin", "bitcoin": "bitcoin", "eth": "ethereum", "ethereum": "ethereum"}
DEFAULT_VOLATILITY = {"bitcoin": 0.60, "ethereum": 0.75}


class CryptoSpotProvider:
    def __init__(
        self,
        settings: Settings,
        http_get: HttpGet | None = None,
        refresh: bool = False,
    ) -> None:
        self.settings = settings
        self.refresh = refresh
        self.observed_at = datetime.fromtimestamp(0, UTC)
        self.available_at = datetime.fromtimestamp(0, UTC)
        self.observed_by_asset: dict[str, datetime] = {}
        self.cache_verified = False
        limiter = RollingRateLimiter(
            settings.cache_dir / "rate_limits" / "crypto.json",
            settings.crypto_rate_limit_per_minute,
        )
        self.client = CachedHttpClient(settings.cache_dir, "crypto", limiter, http_get)

    def fetch_anchors_for_markets(self, markets: list[PredictionMarket]) -> list[ExternalAnchor]:
        parsed = [
            item
            for market in markets
            if market.category == "crypto_threshold"
            and (item := _parse_crypto_market(market)) is not None
        ]
        if not parsed:
            return []
        asset_ids = sorted({item["asset_id"] for item in parsed})
        prices = self._fetch_prices(asset_ids)
        anchors: list[ExternalAnchor] = []
        for item in parsed:
            market = item["market"]
            asset_id = item["asset_id"]
            spot = prices.get(asset_id)
            if spot is None:
                continue
            threshold = float(item["threshold"])
            expiry = item["expiry"]
            anchors.append(
                ExternalAnchor(
                    source_id=f"crypto:{asset_id}:{market.market_id}",
                    event_id=market.event_id,
                    source="coingecko_spot_default_vol",
                    category="crypto_threshold",
                    outcome="Yes",
                    market_id=market.market_id,
                    timestamp=self.observed_by_asset.get(asset_id, datetime.fromtimestamp(0, UTC)),
                    available_at=self.available_at,
                    metadata={
                        "asset": asset_id,
                        "cache_verified": self.cache_verified,
                        "spot": spot,
                        "threshold": threshold,
                        "direction": item["direction"],
                        "volatility": DEFAULT_VOLATILITY.get(asset_id, 0.70),
                        "volatility_provenance": "assumed_unvalidated",
                        "expiry": expiry.isoformat()
                        if isinstance(expiry, datetime)
                        else str(expiry),
                    },
                )
            )
        LOGGER.info("crypto_anchors_collected count=%d", len(anchors))
        return anchors

    def _fetch_prices(self, asset_ids: list[str]) -> dict[str, float]:
        response = self.client.get_json(
            f"{self.settings.crypto_api_base.rstrip('/')}/simple/price",
            params={
                "ids": ",".join(asset_ids),
                "vs_currencies": "usd",
                "include_last_updated_at": "true",
            },
            refresh=self.refresh,
            timeout=15,
        )
        payload = response.payload
        self.available_at = response.fetched_at
        self.cache_verified = response.verified
        self.observed_at = response.fetched_at
        if not isinstance(payload, Mapping):
            return {}
        prices: dict[str, float] = {}
        for asset_id in asset_ids:
            row = payload.get(asset_id)
            if isinstance(row, Mapping) and row.get("usd") is not None:
                try:
                    prices[asset_id] = float(row["usd"])
                    source_time = datetime.fromtimestamp(float(row.get("last_updated_at", 0)), UTC)
                    self.observed_by_asset[asset_id] = source_time
                    self.observed_at = min(self.observed_at, source_time)
                except (TypeError, ValueError):
                    continue
        return prices


def _parse_crypto_market(market: PredictionMarket) -> dict[str, Any] | None:
    payoff = crypto_payoff(market)
    if payoff is None or payoff.kind != "terminal":
        return None
    text = market.title.lower().replace(",", "")
    asset_match = re.search(r"\b(btc|bitcoin|eth|ethereum)\b", text)
    threshold_match = re.search(
        r"(?:above|over|below|under|hit|reach)\s+\$?([0-9]+(?:\.[0-9]+)?)(k)?", text
    )
    if not asset_match or not threshold_match or not market.end_time:
        return None
    asset = ASSET_IDS.get(asset_match.group(1))
    if not asset:
        return None
    threshold = float(threshold_match.group(1)) * (1000.0 if threshold_match.group(2) else 1.0)
    return {
        "market": market,
        "asset_id": asset,
        "threshold": threshold,
        "expiry": market.end_time,
        "direction": payoff.direction,
    }
