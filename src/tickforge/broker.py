from __future__ import annotations

from datetime import datetime

from .models import ClosedTrade, Position


class SimulatedBroker:
    """Deterministic one-position broker used by the public project."""

    def __init__(
        self,
        initial_equity: float,
        point_value: float,
        fee_per_side: float,
        slippage_points: float = 1,
    ) -> None:
        self.initial_equity = initial_equity
        self.cash = initial_equity
        self.point_value = point_value
        self.fee_per_side = fee_per_side
        self.slippage_points = slippage_points
        self.position: Position | None = None
        self.closed_trades: list[ClosedTrade] = []
        self.last_price: float | None = None

    def mark(self, price: float) -> None:
        self.last_price = price
        if self.position:
            self.position.highest_price = max(self.position.highest_price, price)

    @property
    def equity(self) -> float:
        unrealized = 0.0
        if self.position and self.last_price is not None:
            unrealized = (
                (self.last_price - self.position.entry_price)
                * self.point_value
                * self.position.quantity
            )
        return self.cash + unrealized

    @property
    def realized_pnl(self) -> float:
        return self.cash - self.initial_equity

    def open_long(self, price: float, timestamp: datetime, stop_points: float) -> Position:
        if self.position is not None:
            raise RuntimeError("a position is already open")
        fill = round(price + self.slippage_points, 2)
        self.cash -= self.fee_per_side
        self.position = Position(
            quantity=1,
            entry_price=fill,
            entry_time=timestamp,
            stop_price=round(fill - stop_points, 2),
            highest_price=fill,
        )
        self.last_price = fill
        return self.position

    def close_long(self, price: float, timestamp: datetime, reason: str) -> ClosedTrade:
        if self.position is None:
            raise RuntimeError("there is no long position to close")
        position = self.position
        fill = round(price - self.slippage_points, 2)
        gross = (fill - position.entry_price) * self.point_value * position.quantity
        self.cash += gross - self.fee_per_side
        trade = ClosedTrade(
            entry_price=position.entry_price,
            exit_price=fill,
            entry_time=position.entry_time,
            exit_time=timestamp,
            quantity=position.quantity,
            gross_pnl=gross,
            net_pnl=gross - self.fee_per_side * 2,
            reason=reason,
        )
        self.closed_trades.append(trade)
        self.position = None
        self.last_price = fill
        return trade
