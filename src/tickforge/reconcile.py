"""Reconciliation between local intent and an external position surface.

The rule this module encodes is deliberately blunt: **an execution venue is
authoritative about positions, and anything other than a fully reconciled view
forbids new exposure.** The engine calls this before it spends any effort on a
trading candidate, so a disagreement can never be "reasoned away" downstream.

Two properties are worth reading carefully, because they are the reason the
module exists:

* Reconciliation never *creates* permission. Every state except ``IN_SYNC``
  sets ``can_open=False``; ``IN_SYNC`` only reports that the two views agree,
  and a separate risk gate still decides whether a new position is allowed.
* In strict mode an orphan position (something the venue reports that this
  process did not create) is **never auto-liquidated.** Automatically closing a
  position you do not understand is a way to turn a bookkeeping bug into a
  realised loss. Strict mode blocks new exposure and waits for a human.

The external surface is an injectable protocol. This project ships no venue
integration; :class:`MirroredPositions` reflects the in-process simulated
broker so the default configuration is always ``IN_SYNC``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

SIMULATED_SYMBOL = "SIMULATED"


class ReconciliationState(StrEnum):
    """Outcome of comparing the local view with the external view."""

    IN_SYNC = "IN_SYNC"
    MISMATCH = "MISMATCH"
    ORPHAN_POSITION = "ORPHAN_POSITION"
    ORPHAN_CLOSED = "ORPHAN_CLOSED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class ExternalPosition:
    """One position as reported by an external surface.

    ``symbol`` is an opaque identifier. This project never interprets it and
    never ships a real one.
    """

    symbol: str
    quantity: int
    direction: str = "long"
    price: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "direction": self.direction,
            "price": self.price,
        }


@dataclass(frozen=True, slots=True)
class LocalPosition:
    """The position this process believes it holds."""

    symbol: str
    quantity: int
    direction: str = "long"


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    state: ReconciliationState
    can_open: bool
    reason: str
    checked_at: str
    action: str | None = None
    external_positions: tuple[ExternalPosition, ...] = field(default_factory=tuple)

    @property
    def in_sync(self) -> bool:
        return self.state is ReconciliationState.IN_SYNC

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "can_open": self.can_open,
            "reason": self.reason,
            "checked_at": self.checked_at,
            "action": self.action,
            "external_positions": [item.as_dict() for item in self.external_positions],
        }


@runtime_checkable
class ExternalPositionSource(Protocol):
    """Extension point for whatever holds the authoritative position view.

    Implementations may raise. A raising source is reported as ``ERROR``, which
    blocks new exposure; it is never treated as "no positions".
    """

    def positions(self) -> list[ExternalPosition]: ...


@runtime_checkable
class PositionFlattener(Protocol):
    """Optional non-strict helper able to close an orphan position."""

    def flatten(self, position: ExternalPosition) -> bool: ...


class NoExternalPositions:
    """Default source: an empty external book."""

    def positions(self) -> list[ExternalPosition]:
        return []


class MirroredPositions:
    """Reflect the in-process simulated broker as if it were external.

    Used as the default so that a stock checkout reconciles cleanly. Replace it
    with a real adapter to get any value out of reconciliation at all.
    """

    def __init__(self, broker: Any, symbol: str = SIMULATED_SYMBOL) -> None:
        self.broker = broker
        self.symbol = symbol

    def positions(self) -> list[ExternalPosition]:
        position = getattr(self.broker, "position", None)
        if position is None:
            return []
        return [
            ExternalPosition(
                symbol=self.symbol,
                quantity=int(position.quantity),
                direction="long",
                price=float(position.entry_price),
            )
        ]


class Reconciler:
    """Compare the local position view against an external surface.

    ``strict=True`` (the default) is the posture intended for anything holding
    real exposure: orphan positions are reported and block new exposure, and
    nothing is closed automatically.
    """

    def __init__(
        self,
        source: ExternalPositionSource | None = None,
        *,
        strict: bool = True,
        flattener: PositionFlattener | None = None,
    ) -> None:
        self.source = source or NoExternalPositions()
        self.strict = strict
        self.flattener = flattener
        self.last_report: ReconciliationReport | None = None

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def reconcile(self, local: LocalPosition | None) -> ReconciliationReport:
        checked_at = self._now()
        try:
            raw = list(self.source.positions())
        except Exception as exc:
            return self._store(
                ReconciliationReport(
                    state=ReconciliationState.ERROR,
                    can_open=False,
                    reason=f"external position lookup failed: {exc}",
                    checked_at=checked_at,
                )
            )

        external = tuple(item for item in raw if int(item.quantity) > 0)
        report = self._classify(external, local, checked_at)
        if (
            report.state is ReconciliationState.ORPHAN_POSITION
            and not self.strict
            and self.flattener is not None
            and len(external) == 1
        ):
            report = self._attempt_flatten(external[0], report)
        return self._store(report)

    def _classify(
        self,
        external: tuple[ExternalPosition, ...],
        local: LocalPosition | None,
        checked_at: str,
    ) -> ReconciliationReport:
        if not external and local is None:
            return ReconciliationReport(
                state=ReconciliationState.IN_SYNC,
                can_open=True,
                reason="local and external views are both flat",
                checked_at=checked_at,
                external_positions=external,
            )
        if not external and local is not None:
            return ReconciliationReport(
                state=ReconciliationState.MISMATCH,
                can_open=False,
                reason="local book holds a position the external view does not report",
                checked_at=checked_at,
                external_positions=external,
            )
        if external and local is None:
            return self._orphan(external, checked_at)
        if len(external) > 1:
            return ReconciliationReport(
                state=ReconciliationState.MISMATCH,
                can_open=False,
                reason="external view reports more positions than the local book",
                checked_at=checked_at,
                external_positions=external,
            )

        assert local is not None
        held = external[0]
        matches = (
            held.direction.lower() == local.direction.lower()
            and int(held.quantity) == int(local.quantity)
            and str(held.symbol) == str(local.symbol)
        )
        if matches:
            return ReconciliationReport(
                state=ReconciliationState.IN_SYNC,
                can_open=False,
                reason="one reconciled position is already held",
                checked_at=checked_at,
                external_positions=external,
            )
        return ReconciliationReport(
            state=ReconciliationState.MISMATCH,
            can_open=False,
            reason="local and external position details disagree",
            checked_at=checked_at,
            external_positions=external,
        )

    def _orphan(
        self, external: tuple[ExternalPosition, ...], checked_at: str
    ) -> ReconciliationReport:
        if self.strict:
            return ReconciliationReport(
                state=ReconciliationState.ORPHAN_POSITION,
                can_open=False,
                reason=(
                    "external view reports a position this process did not create; "
                    "new exposure is blocked and nothing is closed automatically"
                ),
                checked_at=checked_at,
                action="MANUAL_INTERVENTION_REQUIRED",
                external_positions=external,
            )
        return ReconciliationReport(
            state=ReconciliationState.ORPHAN_POSITION,
            can_open=False,
            reason="untracked external position; new exposure is blocked",
            checked_at=checked_at,
            action="WAITING_FOR_OPERATOR",
            external_positions=external,
        )

    def _attempt_flatten(
        self, orphan: ExternalPosition, report: ReconciliationReport
    ) -> ReconciliationReport:
        assert self.flattener is not None
        try:
            closed = bool(self.flattener.flatten(orphan))
        except Exception as exc:
            return ReconciliationReport(
                state=ReconciliationState.ORPHAN_POSITION,
                can_open=False,
                reason=f"orphan flatten failed: {exc}",
                checked_at=report.checked_at,
                action="CLOSE_FAILED",
                external_positions=report.external_positions,
            )
        if not closed:
            return ReconciliationReport(
                state=ReconciliationState.ORPHAN_POSITION,
                can_open=False,
                reason="orphan flatten was submitted but is unconfirmed",
                checked_at=report.checked_at,
                action="CLOSE_UNCONFIRMED",
                external_positions=report.external_positions,
            )
        return ReconciliationReport(
            state=ReconciliationState.ORPHAN_CLOSED,
            can_open=False,
            reason="orphan position was closed; the next reconciliation may re-admit entries",
            checked_at=self._now(),
            action="CLOSED",
            external_positions=report.external_positions,
        )

    def _store(self, report: ReconciliationReport) -> ReconciliationReport:
        self.last_report = report
        return report
