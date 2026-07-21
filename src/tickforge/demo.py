from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

from .models import Bar


class DemoMarket:
    """Repeatable synthetic one-minute market data for the dashboard and examples."""

    def __init__(self, seed: int = 7, start_price: float = 1_000) -> None:
        self.random = random.Random(seed)
        self.price = start_price
        self.index = 0
        self.timestamp = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=4)

    def next_bar(self) -> Bar:
        cycle = math.sin(self.index / 18) * 5
        trend = 1.5 if 45 <= self.index % 120 <= 95 else -0.3
        change = trend + cycle + self.random.gauss(0, 8)
        open_price = self.price
        close = max(1, open_price + change)
        high = max(open_price, close) + abs(self.random.gauss(4, 3))
        low = min(open_price, close) - abs(self.random.gauss(4, 3))
        self.timestamp += timedelta(minutes=1)
        self.index += 1
        self.price = close
        return Bar(
            timestamp=self.timestamp,
            open=round(open_price, 2),
            high=round(high, 2),
            low=round(low, 2),
            close=round(close, 2),
            volume=round(max(1, self.random.gauss(250, 70))),
        )
