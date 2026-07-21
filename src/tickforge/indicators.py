from __future__ import annotations

from datetime import datetime

from .models import Bar


def ema(values: list[float], period: int) -> float:
    if not values:
        raise ValueError("EMA requires at least one value")
    alpha = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


class BarAggregator:
    """Aggregate completed one-minute bars into larger completed bars."""

    def __init__(self, minutes: int) -> None:
        if minutes <= 1:
            raise ValueError("aggregation interval must be greater than one minute")
        self.minutes = minutes
        self._bucket: datetime | None = None
        self._bars: list[Bar] = []

    def _bucket_start(self, value: datetime) -> datetime:
        return value.replace(minute=(value.minute // self.minutes) * self.minutes, second=0,
                             microsecond=0)

    def push(self, bar: Bar) -> Bar | None:
        bucket = self._bucket_start(bar.timestamp)
        if self._bucket is None:
            self._bucket = bucket
        if bucket < self._bucket:
            raise ValueError("bars must arrive in chronological order")
        completed = None
        if bucket > self._bucket:
            completed = self._build()
            self._bucket = bucket
            self._bars = []
        self._bars.append(bar)
        return completed

    def _build(self) -> Bar:
        if not self._bars or self._bucket is None:
            raise RuntimeError("cannot build an empty bar")
        return Bar(
            timestamp=self._bucket,
            open=self._bars[0].open,
            high=max(bar.high for bar in self._bars),
            low=min(bar.low for bar in self._bars),
            close=self._bars[-1].close,
            volume=sum(bar.volume for bar in self._bars),
        )
