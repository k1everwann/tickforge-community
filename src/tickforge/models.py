from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class Action(StrEnum):
    HOLD = "HOLD"
    OPEN_LONG = "OPEN_LONG"
    CLOSE = "CLOSE"


@dataclass(frozen=True, slots=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("bar timestamp must be timezone-aware")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("bar high/low does not contain open and close")
        if self.high < self.low:
            raise ValueError("bar high must be greater than or equal to low")
        if self.volume < 0:
            raise ValueError("bar volume cannot be negative")

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["timestamp"] = self.timestamp.isoformat()
        return value


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    reason: str
    confidence: float = 0
    stop_points: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "action": self.action.value}


@dataclass(slots=True)
class Position:
    quantity: int
    entry_price: float
    entry_time: datetime
    stop_price: float
    highest_price: float

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["entry_time"] = self.entry_time.isoformat()
        return value


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    quantity: int
    gross_pnl: float
    net_pnl: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["entry_time"] = self.entry_time.isoformat()
        value["exit_time"] = self.exit_time.isoformat()
        return value
