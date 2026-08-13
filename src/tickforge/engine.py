"""Simulation engine wired in the intended order: rules first, model second.

Ordering, in :meth:`TradingEngine.on_bar`:

1. Bookkeeping and the deterministic protective exit (a hard stop is not a
   decision and is never gated behind anything that could delay it).
2. **Reconciliation** against the external position view.
3. **Pre-model gates.** If one rejects, the run ends here: no strategy
   evaluation, no reviewer call, no submission.
4. Strategy produces a candidate.
5. **Reviewer** may narrow the candidate to HOLD. It may not change it into
   anything else; :func:`narrow_only` enforces that structurally.
6. **Post-model gates** (risk, pause).
7. Submission, through the durable order journal.

The reviewer sits in the middle of that pipeline on purpose. It is the only
component that is not deterministic, so it is given the least authority: it runs
only on candidates the rules already permitted, and its sole power is to say no.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

from .broker import SimulatedBroker
from .config import Settings
from .emergency import EmergencyCoordinator, EmergencyFlowError
from .gates import (
    GateChain,
    GateContext,
    GateOutcome,
    NotPausedGate,
    NoUnresolvedOrderIntentGate,
    RiskGate,
    StateReconciledGate,
)
from .indicators import BarAggregator
from .journal import OrderJournal
from .models import Action, Bar, Decision
from .reconcile import (
    SIMULATED_SYMBOL,
    LocalPosition,
    MirroredPositions,
    Reconciler,
    ReconciliationReport,
)
from .review import DecisionReviewer, PassThroughReviewer
from .risk import RiskManager
from .strategy import ExampleLongOnlyStrategy


def narrow_only(candidate: Decision, reviewed: Decision) -> Decision:
    """Allow a reviewer to reject a candidate, never to replace it.

    A reviewer may return the candidate unchanged or a HOLD. Anything else -
    a different action, a different direction, a widened stop - is treated as a
    HOLD, because a component whose only sanctioned power is veto has just tried
    to use a power it does not have.
    """
    if reviewed.action is candidate.action:
        return reviewed
    if reviewed.action is Action.HOLD:
        return reviewed
    return Decision(
        Action.HOLD,
        f"reviewer may only narrow a candidate, not change it to {reviewed.action.value}",
        0,
    )


class TradingEngine:
    """Single-position simulation engine with durable fail-closed order handling."""

    def __init__(
        self,
        settings: Settings | None = None,
        reviewer: DecisionReviewer | None = None,
        *,
        reconciler: Reconciler | None = None,
        pre_model_gates: GateChain | None = None,
        post_model_gates: GateChain | None = None,
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
        self.reconciler = reconciler or Reconciler(MirroredPositions(self.broker))
        self.emergency = EmergencyCoordinator(
            self.settings.db_path.with_name(self.settings.db_path.name + ".emergency")
        )
        self.pre_model_gates = pre_model_gates or GateChain(
            (NoUnresolvedOrderIntentGate(), StateReconciledGate()), label="pre_model"
        )
        self.post_model_gates = post_model_gates or GateChain(
            (NotPausedGate(), RiskGate(self.risk, self.broker)), label="post_model"
        )
        self.five_minute = BarAggregator(5)
        self.five_minute_bars: list[Bar] = []
        self.recent_bars: deque[Bar] = deque(maxlen=120)
        self.recent_decisions: deque[dict[str, Any]] = deque(maxlen=50)
        self.paused = False
        self.last_bar_at: datetime | None = None
        self.last_exit_at: datetime | None = None
        self.started_at = datetime.now(UTC)
        self.reconciliation: ReconciliationReport | None = None
        self.last_gate_outcome: GateOutcome | None = None
        self._pending_challenge_id: str | None = None
        self._lock = threading.RLock()

    # -- gate plumbing -----------------------------------------------------

    def local_position(self) -> LocalPosition | None:
        position = self.broker.position
        if position is None:
            return None
        return LocalPosition(
            symbol=SIMULATED_SYMBOL, quantity=int(position.quantity), direction="long"
        )

    def reconcile(self) -> ReconciliationReport:
        self.reconciliation = self.reconciler.reconcile(self.local_position())
        return self.reconciliation

    def gate_context(self, at: datetime, candidate: Decision | None = None) -> GateContext:
        return GateContext(
            at=at,
            candidate=candidate,
            position=self.broker.position,
            unresolved_orders=tuple(self.journal.unresolved()),
            reconciliation=self.reconciliation,
            paused=self.paused,
            bars=tuple(self.five_minute_bars[-40:]),
            last_exit_at=self.last_exit_at,
        )

    # -- main loop ---------------------------------------------------------

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

            self.reconcile()
            pre = self.pre_model_gates.evaluate(self.gate_context(bar.timestamp))
            self.last_gate_outcome = pre
            if not pre.allowed:
                # No strategy evaluation and no reviewer call happen from here.
                return self._record(Decision(Action.HOLD, pre.reason), bar.close)

            candidate = self.strategy.evaluate(self.five_minute_bars, self.broker.position)
            reviewed = self.reviewer.review(candidate, self.five_minute_bars)
            decision = narrow_only(candidate, reviewed)

            post = self.post_model_gates.evaluate(self.gate_context(bar.timestamp, decision))
            self.last_gate_outcome = pre.narrow(post)
            if not post.allowed:
                return self._record(Decision(Action.HOLD, post.reason), bar.close)

            executed = self._execute(decision, bar.close, bar.timestamp)
            return self._record(executed, bar.close)

    def _execute(self, decision: Decision, price: float, timestamp: datetime) -> Decision:
        """Submit through the journal. Re-checks the same invariants on purpose.

        The gate chain already covered these. They are re-checked here because
        this method is also reachable from the protective stop and the emergency
        path, and because a final check immediately before submission is the one
        place a bug in the ordering above cannot get past.
        """
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
            self.journal.mark_submitting(intent)
            try:
                position = self.broker.open_long(
                    price, timestamp, float(decision.stop_points or 0)
                )
            except Exception as exc:
                self.journal.mark_unknown(intent, str(exc))
                raise
            self.journal.resolve(intent, "FILLED", position.as_dict())
            self.journal.event("POSITION_OPENED", position.as_dict())
        elif decision.action is Action.CLOSE and self.broker.position:
            intent = self.journal.start_intent("CLOSE_LONG", decision.as_dict())
            self.journal.mark_submitting(intent)
            try:
                trade = self.broker.close_long(price, timestamp, decision.reason)
            except Exception as exc:
                self.journal.mark_unknown(intent, str(exc))
                raise
            self.journal.resolve(intent, "FILLED", trade.as_dict())
            self.journal.event("POSITION_CLOSED", trade.as_dict())
            self.last_exit_at = timestamp
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

    # -- emergency ---------------------------------------------------------

    def position_snapshot(self) -> dict[str, Any]:
        """Identity of the current position, for the emergency fingerprint.

        Deliberately excludes marks that move with price, so an ordinary tick
        does not invalidate a confirmation the operator is still typing. A
        change in existence, size, or entry does invalidate it.
        """
        position = self.broker.position
        if position is None:
            return {}
        return {
            "quantity": position.quantity,
            "entry_price": position.entry_price,
            "entry_time": position.entry_time.isoformat(),
        }

    def prepare_emergency_flat(self, actor: str = "owner") -> dict[str, Any]:
        prepared = self.emergency.prepare(actor, self.position_snapshot())
        self._pending_challenge_id = prepared["challenge_id"]
        self.journal.event(
            "EMERGENCY_FLAT_PREPARED",
            {"challenge_id": prepared["challenge_id"], "expires_at": prepared["expires_at"]},
        )
        return {
            "challenge_id": prepared["challenge_id"],
            "confirmation_phrase": prepared["confirmation_phrase"],
            # Retained name for existing clients; carries the confirmation phrase.
            "confirmation_token": prepared["confirmation_phrase"],
            "expires_at": prepared["expires_at"],
        }

    def execute_emergency_flat(
        self, token: str, challenge_id: str | None = None, actor: str = "owner"
    ) -> dict[str, Any]:
        challenge = challenge_id or self._pending_challenge_id
        if not challenge or not token:
            raise ValueError("invalid or expired emergency confirmation token")
        try:
            self.emergency.consume(challenge, actor, token, self.position_snapshot())
        except EmergencyFlowError as exc:
            raise ValueError(f"invalid or expired emergency confirmation token: {exc}") from exc
        self._pending_challenge_id = None
        self.paused = True
        if not self.broker.position:
            self.journal.event("EMERGENCY_FLAT_NO_POSITION", {})
            return {"closed": False, "reason": "no open position", "paused": True}
        price = self.broker.last_price
        if price is None:
            raise RuntimeError("cannot flatten without a market price")
        decision = Decision(Action.CLOSE, "two-step emergency flat", 1.0)
        self._execute(decision, price, datetime.now(UTC))
        return {"closed": True, "paused": True}

    # -- observability -----------------------------------------------------

    def health(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        age = (now - self.last_bar_at).total_seconds() if self.last_bar_at else None
        unresolved = self.journal.unresolved()
        reconciled = self.reconciliation is None or self.reconciliation.in_sync
        healthy = not unresolved and reconciled and (age is None or age <= 180)
        return {
            "status": "healthy" if healthy else "degraded",
            "simulation_only": True,
            "paused": self.paused,
            "last_bar_age_seconds": age,
            "unresolved_order_count": len(unresolved),
            "unresolved_order_states": sorted({row["state"] for row in unresolved}),
            "reconciliation_state": (
                self.reconciliation.state.value if self.reconciliation else None
            ),
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
            "reconciliation": self.reconciliation.as_dict() if self.reconciliation else None,
            "gates": {
                "pre_model": self.pre_model_gates.names,
                "post_model": self.post_model_gates.names,
                "last_outcome": (
                    self.last_gate_outcome.as_dict() if self.last_gate_outcome else None
                ),
            },
            "health": self.health(),
            "config": self.settings.public_dict(),
        }
