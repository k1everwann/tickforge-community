from __future__ import annotations

from datetime import date

from .broker import SimulatedBroker
from .config import Settings


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def daily_realized(self, broker: SimulatedBroker, trading_day: date) -> float:
        return sum(
            trade.net_pnl for trade in broker.closed_trades if trade.exit_time.date() == trading_day
        )

    def can_open(
        self, broker: SimulatedBroker, stop_points: float | None, trading_day: date
    ) -> tuple[bool, str]:
        if broker.position is not None:
            return False, "maximum one long position"
        if stop_points is None or not 0 < stop_points <= self.settings.max_stop_points:
            return False, "stop distance is missing or exceeds the configured maximum"
        if self.daily_realized(broker, trading_day) <= -self.settings.max_daily_loss:
            return False, "daily loss limit reached"
        if broker.equity <= 0:
            return False, "account equity is not positive"
        return True, "ok"
