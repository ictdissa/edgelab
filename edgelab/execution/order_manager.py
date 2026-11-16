"""Order-routing boundary that rejects external submission in research mode."""

from __future__ import annotations


class OrderManager:
    def place_order(self, *args: object, **kwargs: object) -> None:
        _ = args, kwargs
        raise RuntimeError("Order placement is disabled in EdgeLab research mode")
