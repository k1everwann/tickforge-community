"""Deterministic gate chain: rules run before the model, and only ever narrow.

The architecture this project exists to demonstrate has one ordering rule:

    deterministic gates -> (model review) -> deterministic gates -> submission

Concretely:

* **Pre-model gates** run before a trading candidate is even produced, and
  therefore before any model call. They answer questions that do not depend on
  the candidate: is the position view reconciled, is there an unresolved order
  intent, is the process in a state where acting is meaningful at all. If one
  rejects, no model is consulted and nothing is submitted.
* **Post-model gates** run once a candidate exists but before submission. They
  answer questions about *this* candidate: does risk permit a new position, is
  new exposure currently paused.

The chain stops at the first rejection and a rejection is sticky:
:meth:`GateOutcome.narrow` can turn ``allowed`` from ``True`` to ``False`` but
never the other way round. That is the "one-way convergence" property the tests
assert over randomised gate orderings — no later gate, no reviewer, and no
downstream code can re-admit something an earlier gate rejected.

Example gates
-------------
The gates whose behaviour depends on a *number* are shipped as clearly-labelled
examples, and **every number in them is invented for illustration.** They are
not enabled by default. This repository is a governance skeleton: it ships the
mechanism, not anybody's parameters. Pick your own values, justify them, and
write your own gates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Protocol, runtime_checkable

from .models import Action, Bar, Decision, Position
from .reconcile import ReconciliationReport

# --- Deliberately fictional example values -------------------------------
# These exist so the example gates below are runnable and testable. They are
# illustrative placeholders, not a recommendation and not in use anywhere.
EXAMPLE_SESSION_OPEN = time(1, 0)
EXAMPLE_SESSION_CLOSE = time(23, 0)
EXAMPLE_REENTRY_COOLDOWN = timedelta(minutes=11)
EXAMPLE_MAX_SPREAD_POINTS = 7.0


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's verdict. ``allowed=False`` is final for the whole chain."""

    allowed: bool
    reason: str
    gate_name: str

    def as_dict(self) -> dict[str, Any]:
        return {"gate": self.gate_name, "allowed": self.allowed, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class GateContext:
    """Everything a gate is allowed to look at.

    Pre-model evaluation leaves ``candidate`` as ``None``: a pre-model gate
    structurally cannot depend on the candidate, because the candidate does not
    exist yet.
    """

    at: datetime
    candidate: Decision | None = None
    position: Position | None = None
    unresolved_orders: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    reconciliation: ReconciliationReport | None = None
    paused: bool = False
    bars: tuple[Bar, ...] = field(default_factory=tuple)
    last_exit_at: datetime | None = None
    spread_points: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def action(self) -> Action | None:
        return self.candidate.action if self.candidate else None


@runtime_checkable
class Gate(Protocol):
    """A deterministic, side-effect-free check."""

    name: str

    def check(self, context: GateContext) -> GateResult: ...


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """Result of running a chain. Rejection is sticky."""

    allowed: bool
    reason: str
    results: tuple[GateResult, ...] = field(default_factory=tuple)
    rejected_by: str | None = None

    def narrow(self, other: GateOutcome) -> GateOutcome:
        """Combine two outcomes. This can only ever tighten the verdict.

        If ``self`` already rejected, the rejection and its reason survive
        untouched no matter what ``other`` says.
        """
        if not self.allowed:
            return GateOutcome(
                allowed=False,
                reason=self.reason,
                results=self.results + other.results,
                rejected_by=self.rejected_by,
            )
        return GateOutcome(
            allowed=other.allowed,
            reason=other.reason,
            results=self.results + other.results,
            rejected_by=other.rejected_by,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "rejected_by": self.rejected_by,
            "results": [item.as_dict() for item in self.results],
        }


class GateChain:
    """Evaluate gates in order and stop at the first rejection.

    Stopping early is not an optimisation, it is the point: a gate that rejects
    ends the evaluation, so a later gate never gets the opportunity to overturn
    it, and any work a later gate would have done (including calling a model)
    never happens.
    """

    def __init__(self, gates: tuple[Gate, ...] | list[Gate] = (), *, label: str = "gates") -> None:
        self.gates: tuple[Gate, ...] = tuple(gates)
        self.label = label

    @property
    def names(self) -> list[str]:
        return [gate.name for gate in self.gates]

    def evaluate(self, context: GateContext) -> GateOutcome:
        results: list[GateResult] = []
        for gate in self.gates:
            result = gate.check(context)
            results.append(result)
            if not result.allowed:
                return GateOutcome(
                    allowed=False,
                    reason=result.reason,
                    results=tuple(results),
                    rejected_by=result.gate_name,
                )
        return GateOutcome(allowed=True, reason="ok", results=tuple(results))


def allow(gate: str, reason: str = "ok") -> GateResult:
    return GateResult(allowed=True, reason=reason, gate_name=gate)


def block(gate: str, reason: str) -> GateResult:
    return GateResult(allowed=False, reason=reason, gate_name=gate)


# --------------------------------------------------------------------------
# Pre-model gates: no candidate exists yet, so no model has been called yet.
# --------------------------------------------------------------------------


class NoUnresolvedOrderIntentGate:
    """Refuse to do anything while an order intent is unresolved.

    An unresolved intent means this process does not know whether an order it
    created reached the venue. Producing another candidate in that state risks
    duplicate exposure, so the run stops here.
    """

    name = "no_unresolved_order_intent"

    def check(self, context: GateContext) -> GateResult:
        if context.unresolved_orders:
            return block(self.name, "unresolved order intent; fail closed")
        return allow(self.name)


class StateReconciledGate:
    """Refuse to act unless the local and external position views agree.

    Note that this gate looks at ``state``, not at ``can_open``. "One position
    is already held" is a reconciled, healthy state; whether a *new* position is
    permitted is a risk question, decided after a candidate exists.
    """

    name = "state_reconciled"

    def check(self, context: GateContext) -> GateResult:
        report = context.reconciliation
        if report is None:
            return block(self.name, "position view has not been reconciled yet")
        if not report.in_sync:
            return block(self.name, f"position view is {report.state.value}: {report.reason}")
        return allow(self.name)


# --------------------------------------------------------------------------
# Post-model gates: a candidate exists; it may only be narrowed from here.
# --------------------------------------------------------------------------


class NotPausedGate:
    """Block new exposure while the engine is paused. Exits stay available."""

    name = "not_paused"

    def check(self, context: GateContext) -> GateResult:
        if context.paused and context.action is Action.OPEN_LONG:
            return block(self.name, "engine paused; new positions are disabled")
        return allow(self.name)


class RiskGate:
    """Apply :class:`~tickforge.risk.RiskManager` to an entry candidate."""

    name = "risk_limits"

    def __init__(self, risk: Any, broker: Any) -> None:
        self.risk = risk
        self.broker = broker

    def check(self, context: GateContext) -> GateResult:
        if context.action is not Action.OPEN_LONG or context.candidate is None:
            return allow(self.name, "not an entry candidate")
        allowed, reason = self.risk.can_open(
            self.broker, context.candidate.stop_points, context.at.date()
        )
        return GateResult(allowed=allowed, reason=reason, gate_name=self.name)


# --------------------------------------------------------------------------
# Example gates. Every value here is fictional and none are enabled by default.
# --------------------------------------------------------------------------


class ExampleSessionWindowGate:
    """EXAMPLE ONLY: allow entries inside a fictional session window.

    The window below (01:00-23:00 in whatever timezone the bars carry) is made
    up to be obviously unrealistic. A real deployment needs a real calendar,
    including holidays, maintenance breaks and per-instrument sessions.
    """

    name = "example_session_window"

    def __init__(
        self, opens: time = EXAMPLE_SESSION_OPEN, closes: time = EXAMPLE_SESSION_CLOSE
    ) -> None:
        self.opens = opens
        self.closes = closes

    def check(self, context: GateContext) -> GateResult:
        current = context.at.timetz().replace(tzinfo=None)
        if self.opens <= current <= self.closes:
            return allow(self.name)
        return block(self.name, f"outside the example session window {self.opens}-{self.closes}")


class ExampleReentryCooldownGate:
    """EXAMPLE ONLY: require a fictional pause after the previous exit.

    Eleven minutes is an arbitrary number chosen to look arbitrary.
    """

    name = "example_reentry_cooldown"

    def __init__(self, cooldown: timedelta = EXAMPLE_REENTRY_COOLDOWN) -> None:
        self.cooldown = cooldown

    def check(self, context: GateContext) -> GateResult:
        if context.last_exit_at is None:
            return allow(self.name)
        elapsed = context.at - context.last_exit_at
        if elapsed < self.cooldown:
            return block(self.name, f"example cooldown active ({elapsed} < {self.cooldown})")
        return allow(self.name)


class ExampleSpreadGate:
    """EXAMPLE ONLY: reject entries when a quoted spread looks wide.

    Seven points is a placeholder. The useful part is the shape: a missing
    measurement is treated as a rejection, not as a pass.
    """

    name = "example_spread"

    def __init__(self, maximum_points: float = EXAMPLE_MAX_SPREAD_POINTS) -> None:
        self.maximum_points = maximum_points

    def check(self, context: GateContext) -> GateResult:
        if context.spread_points is None:
            return block(self.name, "no spread measurement available; fail closed")
        if context.spread_points > self.maximum_points:
            return block(
                self.name,
                f"example spread limit exceeded ({context.spread_points} > {self.maximum_points})",
            )
        return allow(self.name)


EXAMPLE_GATES: tuple[Gate, ...] = (
    ExampleSessionWindowGate(),
    ExampleReentryCooldownGate(),
    ExampleSpreadGate(),
)
