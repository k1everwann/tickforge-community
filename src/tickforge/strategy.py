from __future__ import annotations

from .indicators import ema
from .models import Action, Bar, Decision, Position


class ExampleLongOnlyStrategy:
    """Transparent educational strategy, intentionally not an investment recommendation."""

    warmup_bars = 24

    def evaluate(self, bars: list[Bar], position: Position | None) -> Decision:
        if len(bars) < self.warmup_bars:
            return Decision(Action.HOLD, f"warming up ({len(bars)}/{self.warmup_bars})")

        closes = [bar.close for bar in bars]
        current_ema = ema(closes[-20:], 20)
        prior_ema = ema(closes[-23:-3], 20)
        latest = bars[-1]
        previous = bars[-4:-1]
        higher_low = latest.low > min(bar.low for bar in previous)
        breakout = latest.close > max(bar.high for bar in previous)

        if position is None:
            if latest.close > current_ema and current_ema > prior_ema and higher_low and breakout:
                example_stop = max(1, (latest.high - latest.low) * 1.5)
                return Decision(
                    Action.OPEN_LONG,
                    "completed 5m breakout with rising EMA20 and a higher low",
                    confidence=0.70,
                    stop_points=round(example_stop, 2),
                )
            return Decision(Action.HOLD, "no completed long setup")

        structure_failed = latest.close < current_ema and latest.low < min(
            bar.low for bar in bars[-3:-1]
        )
        if structure_failed:
            return Decision(Action.CLOSE, "completed 5m structure failed below EMA20", 0.75)
        return Decision(Action.HOLD, "long structure remains valid")
