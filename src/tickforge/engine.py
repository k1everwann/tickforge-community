from __future__ import annotations

import secrets
import threading
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from .broker import SimulatedBroker
from .config import Settings
from .indicators import BarAggregator
from .journal import OrderJournal
from .models import Action, Bar, Decision
from .review import DecisionReviewer, PassThroughReviewer
from .risk import RiskManager
from .strategy import ExampleLongOnlyStrategy


class TradingEngine:
    """Single-position simulation engine with durable fail-closed order intent handling."""

    def __init__(
        self,
        settings: Settings | None = None,
        reviewer: DecisionReviewer | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.settings.validate()
        self.broker = SimulatedBroker(
            self.settings.initial_equity,
            self.settings.point_value,
            self.settings.fee_per_side,
            self.settings.slippage_points,
        )
        self.journal = OrderJournal(self.settings.db_path)
        self.risk = RiskManager(self.settings)
        self.strategy = ExampleLongOnlyStrategy()
        self.reviewer = reviewer or PassThroughReviewer()
        self.five_minute = BarAggregator(5)
        self.five_minute_bars: list[Bar] = []
        self.recent_bars: deque[Bar] = deque(maxlen=120)
        self.recent_decisions: deque[dict[str, Any]] = deque(maxlen=50)
        self.paused = False
        self.last_bar_at: datetime | None = None
        self.started_at = datetime.now(UTC)
        self._emergency_token: str | None = None
        self._emergency_expires_at: datetime | None = None
        self._lock = threading.RLock()

    def on_bar(self, bar: Bar) -> Decision:
        with self._lock:
            if self.last_bar_at and bar.timestamp <= self.last_bar_at:
                raise ValueError("completed one-minute bars must be strictly chronological")
            self.last_bar_at = bar.timestamp
            self.recent_bars.append(bar)
            self.broker.mark(bar.close)
            completed_five = self.five_minute.push(bar)
            if completed_five is not None:
                self.five_minute_bars.append(completed_five)

            if self.broker.position and bar.low <= self.broker.position.stop_price:
                stop_fill = min(bar.open, self.broker.position.stop_price)
                decision = Decision(Action.CLOSE, "hard stop reached", 1.0)
                executed = self._execute(decision, stop_fill, bar.timestamp)
                return self._record(executed, bar.close)

            if completed_five is None:
                return self._record(
                    Decision(Action.HOLD, "waiting for completed 5m bar"), bar.close
                )

            candidate = self.strategy.evaluate(self.five_minute_bars, self.broker.position)
            decision = self.reviewer.review(candidate, self.five_minute_bars)
            if self.paused and decision.action is Action.OPEN_LONG:
                decision = Decision(Action.HOLD, "engine paused; new positions are disabled")
            executed = self._execute(decision, bar.close, bar.timestamp)
            return self._record(executed, bar.close)

    def _execute(self, decision: Decision, price: float, timestamp: datetime) -> Decision:
        if decision.action is Action.HOLD:
            return decision
        if self.journal.unresolved():
            return Decision(Action.HOLD, "unresolved order intent; fail closed")
        if decision.action is Action.OPEN_LONG:
            allowed, reason = self.risk.can_open(
                self.broker, decision.stop_points, timestamp.date()
            )
            if not allowed:
                return Decision(Action.HOLD, reason)
            intent = self.journal.start_intent("OPEN_LONG", decision.as_dict())
            try:
                position = self.broker.open_long(
                    price, timestamp, float(decision.stop_points or 0)
                )
                self.journal.resolve(intent, "FILLED", position.as_dict())
                self.journal.event("POSITION_OPENED", position.as_dict())
            except Exception as exc:
                self.journal.resolve(intent, "UNKNOWN", {"error": str(exc)})
                raise
        elif decision.action is Action.CLOSE and self.broker.position:
            intent = self.journal.start_intent("CLOSE_LONG", decision.as_dict())
            try:
                trade = self.broker.close_long(price, timestamp, decision.reason)
                self.journal.resolve(intent, "FILLED", trade.as_dict())
                self.journal.event("POSITION_CLOSED", trade.as_dict())
            except Exception as exc:
                self.journal.resolve(intent, "UNKNOWN", {"error": str(exc)})
                raise
        elif decision.action is Action.CLOSE:
            return Decision(Action.HOLD, "no long position to close")
        return decision

    def _record(self, decision: Decision, price: float) -> Decision:
        self.recent_decisions.appendleft(
            {"at": datetime.now(UTC).isoformat(), "price": price, **decision.as_dict()}
        )
        return decision

    def pause(self) -> None:
        self.paused = True
        self.journal.event("ENGINE_PAUSED", {})

    def resume(self) -> None:
        if self.journal.unresolved():
            raise RuntimeError("cannot resume while an order intent is unresolved")
        self.paused = False
        self.journal.event("ENGINE_RESUMED", {})

    def prepare_emergency_flat(self) -> dict[str, Any]:
        token = secrets.token_urlsafe(24)
        expires_at = datetime.now(UTC) + timedelta(seconds=30)
        self._emergency_token = token
        self._emergency_expires_at = expires_at
        self.journal.event("EMERGENCY_FLAT_PREPARED", {"expires_at": expires_at.isoformat()})
        return {"confirmation_token": token, "expires_at": expires_at.isoformat()}

    def execute_emergency_flat(self, token: str) -> dict[str, Any]:
        now = datetime.now(UTC)
        if (
            not token
            or not self._emergency_token
            or not secrets.compare_digest(token, self._emergency_token)
            or not self._emergency_expires_at
            or now > self._emergency_expires_at
        ):
            raise ValueError("invalid or expired emergency confirmation token")
        self._emergency_token = None
        self._emergency_expires_at = None
        self.paused = True
        if not self.broker.position:
            self.journal.event("EMERGENCY_FLAT_NO_POSITION", {})
            return {"closed": False, "reason": "no open position", "paused": True}
        price = self.broker.last_price
        if price is None:
            raise RuntimeError("cannot flatten without a market price")
        decision = Decision(Action.CLOSE, "two-step emergency flat", 1.0)
        self._execute(decision, price, now)
        return {"closed": True, "paused": True}

    def health(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        age = (now - self.last_bar_at).total_seconds() if self.last_bar_at else None
        unresolved = self.journal.unresolved()
        healthy = not unresolved and (age is None or age <= 180)
        return {
            "status": "healthy" if healthy else "degraded",
            "simulation_only": True,
            "paused": self.paused,
            "last_bar_age_seconds": age,
            "unresolved_order_count": len(unresolved),
            "generated_at": now.isoformat(),
        }

    def close(self) -> None:
        self.journal.close()

    def state(self) -> dict[str, Any]:
        return {
            "mode": "SIMULATION_ONLY",
            "paused": self.paused,
            "equity": round(self.broker.equity, 2),
            "realized_pnl": round(self.broker.realized_pnl, 2),
            "position": self.broker.position.as_dict() if self.broker.position else None,
            "closed_trades": [trade.as_dict() for trade in self.broker.closed_trades[-20:]],
            "latest_bar": self.recent_bars[-1].as_dict() if self.recent_bars else None,
            "recent_decisions": list(self.recent_decisions),
            "unresolved_orders": self.journal.unresolved(),
            "health": self.health(),
            "config": self.settings.public_dict(),
        }
