"""Fail-closed live trader."""

from __future__ import annotations

from edgelab.config.settings import Settings


class LiveTrader:
    def __init__(self, settings: Settings) -> None:
        if not settings.enable_live_trading:
            raise RuntimeError("ENABLE_LIVE_TRADING=false; live trader cannot be constructed")
        raise RuntimeError("Live trading is not implemented in EdgeLab v0.1")
