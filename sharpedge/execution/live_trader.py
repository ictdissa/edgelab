"""Legacy execution boundary; order submission is not implemented."""

from __future__ import annotations

from sharpedge.config.settings import Settings
from sharpedge.data.schemas import Signal


class LiveTrader:
    """Validate legacy configuration without providing an order-submission path."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.enable_live_trading:
            raise RuntimeError("LiveTrader cannot be initialized: live trading disabled")
        if not settings.required_live_credentials_present:
            raise RuntimeError("LiveTrader cannot be initialized: credentials missing")

    def place_order(self, signal: Signal) -> None:
        """Reject submission even when legacy credential checks pass."""

        raise NotImplementedError(
            "Live order placement is intentionally not implemented in the research MVP"
        )
