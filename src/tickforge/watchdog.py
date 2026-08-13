"""An out-of-process watchdog for a health surface it does not control.

Why a separate process
----------------------
A trading process cannot be trusted to report that it has stopped working. If it
is wedged, deadlocked, or dead, the code that would have raised the alarm is
wedged, deadlocked, or dead with it. So this module is designed to run as its own
OS process, poll a health surface over a boundary it does not own, and treat
"cannot reach it" as a failure rather than as an absence of evidence.

It deliberately knows nothing about the thing it watches beyond a probe result.

What it does
------------
* **Session-aware staleness.** Silence during a session is a problem; the same
  silence outside a session is normal. What counts as a session comes from an
  injectable :class:`SessionCalendar`. :class:`AlwaysOpenCalendar` is a trivial
  example so the module runs out of the box.
* **Maintenance suppression.** A calendar can declare a maintenance window.
  Failures inside one are observed and recorded but never alerted on, and never
  produce a "recovered" notification either - a recovery from an alert that was
  never sent is noise.
* **Escalation and backoff.** A single failed poll is not an incident. Alerting
  waits for a configurable number of consecutive actionable failures, then backs
  off exponentially so a long outage does not turn into a notification flood.
* **Durable escalation state.** Optional, so a restarted watchdog does not
  re-alert about an incident it already reported, or forget an ongoing one.
* **Injectable notifier.** :class:`Notifier` is an extension point with a no-op
  default, exactly like ``DecisionReviewer`` in :mod:`tickforge.review`. This
  project ships no delivery integration; see :class:`NullNotifier`.

About the numbers
-----------------
Every default in :class:`WatchdogPolicy` is a neutral placeholder chosen to be
reasonable for a demo. Staleness budgets are a property of the system being
watched - its bar interval, its decision cadence, its venue's maintenance
schedule - so they are configuration, not a constant, and this repository has no
opinion about yours.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .monitor import fetch_health

# Neutral placeholder defaults. Override them for your own system.
DEFAULT_POLL_SECONDS = 10.0
DEFAULT_STALE_OPEN_SECONDS = 120.0
DEFAULT_STALE_CLOSED_SECONDS = 900.0
DEFAULT_FAILURES_BEFORE_ALERT = 3
DEFAULT_BACKOFF_SECONDS = 60.0
DEFAULT_MAX_BACKOFF_SECONDS = 3600.0


@runtime_checkable
class SessionCalendar(Protocol):
    """When is the watched system expected to be doing anything?"""

    def is_open(self, at: datetime) -> bool: ...

    def in_maintenance(self, at: datetime) -> bool: ...


class AlwaysOpenCalendar:
    """Trivial example calendar: always open, never in maintenance.

    Replace this. A real calendar needs the sessions, holidays and maintenance
    windows of the venue you actually trade.
    """

    def is_open(self, at: datetime) -> bool:
        del at
        return True

    def in_maintenance(self, at: datetime) -> bool:
        del at
        return False


@runtime_checkable
class Notifier(Protocol):
    """Extension point for alert delivery. Implement this yourself.

    The same shape as :class:`~tickforge.review.DecisionReviewer`: a protocol
    with an inert default, so the repository ships no outbound integration and
    no credentials. A real implementation might write to a log drain, a pager,
    or a message queue; whatever it is, it belongs in your deployment, not here.
    """

    def notify(self, subject: str, body: str) -> None: ...


class NullNotifier:
    """Default notifier: records nothing, sends nothing, fails never."""

    def notify(self, subject: str, body: str) -> None:
        del subject, body


class PrintNotifier:
    """Example notifier that writes to stdout, for local runs."""

    def notify(self, subject: str, body: str) -> None:
        print(json.dumps({"subject": subject, "body": body}, ensure_ascii=False))


@runtime_checkable
class HealthProbe(Protocol):
    """A read-only view of a health surface owned by someone else."""

    def probe(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class HttpHealthProbe:
    """Poll a health endpoint over HTTP. Built on :func:`tickforge.monitor.fetch_health`."""

    url: str
    timeout_seconds: float = 5.0

    def probe(self) -> dict[str, Any]:
        return fetch_health(self.url, timeout=self.timeout_seconds)


@dataclass(frozen=True, slots=True)
class WatchdogPolicy:
    poll_seconds: float = DEFAULT_POLL_SECONDS
    stale_open_seconds: float = DEFAULT_STALE_OPEN_SECONDS
    stale_closed_seconds: float = DEFAULT_STALE_CLOSED_SECONDS
    failures_before_alert: int = DEFAULT_FAILURES_BEFORE_ALERT
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS
    max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS

    def stale_budget(self, session_open: bool) -> float:
        return self.stale_open_seconds if session_open else self.stale_closed_seconds


@dataclass(frozen=True, slots=True)
class Observation:
    at: datetime
    reachable: bool
    healthy: bool
    session_open: bool
    maintenance: bool
    failures: tuple[str, ...] = field(default_factory=tuple)
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        """A failure worth escalating: broken, and not inside maintenance."""
        return not self.healthy and not self.maintenance

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "reachable": self.reachable,
            "healthy": self.healthy,
            "session_open": self.session_open,
            "maintenance": self.maintenance,
            "failures": list(self.failures),
        }


@dataclass(frozen=True, slots=True)
class EscalationState:
    consecutive_failures: int = 0
    alerted: bool = False
    alerts_sent: int = 0
    next_alert_after: str | None = None
    last_subject: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "consecutive_failures": self.consecutive_failures,
            "alerted": self.alerted,
            "alerts_sent": self.alerts_sent,
            "next_alert_after": self.next_alert_after,
            "last_subject": self.last_subject,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EscalationState:
        return cls(
            consecutive_failures=int(payload.get("consecutive_failures") or 0),
            alerted=bool(payload.get("alerted")),
            alerts_sent=int(payload.get("alerts_sent") or 0),
            next_alert_after=payload.get("next_alert_after") or None,
            last_subject=payload.get("last_subject") or None,
        )


class HealthWatchdog:
    """Poll a health surface, escalate with backoff, suppress maintenance noise."""

    def __init__(
        self,
        probe: HealthProbe,
        *,
        calendar: SessionCalendar | None = None,
        notifier: Notifier | None = None,
        policy: WatchdogPolicy | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.probe = probe
        self.calendar = calendar or AlwaysOpenCalendar()
        self.notifier = notifier or NullNotifier()
        self.policy = policy or WatchdogPolicy()
        self.state_path = Path(state_path) if state_path else None
        self.state = self._load_state()
        self.last_observation: Observation | None = None

    # -- durable escalation state -----------------------------------------

    def _load_state(self) -> EscalationState:
        if not self.state_path or not self.state_path.exists():
            return EscalationState()
        try:
            return EscalationState.from_dict(
                json.loads(self.state_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError):
            return EscalationState()

    def _save_state(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.state.as_dict(), ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(self.state_path)

    # -- observation -------------------------------------------------------

    def observe(self, at: datetime | None = None) -> Observation:
        at = at or datetime.now(UTC)
        session_open = bool(self.calendar.is_open(at))
        maintenance = bool(self.calendar.in_maintenance(at))
        try:
            payload = dict(self.probe.probe())
        except Exception as exc:
            observation = Observation(
                at=at,
                reachable=False,
                healthy=False,
                session_open=session_open,
                maintenance=maintenance,
                failures=("unreachable: " + str(exc)[:160],),
            )
            self.last_observation = observation
            return observation

        failures: list[str] = []
        if str(payload.get("status")) != "healthy":
            failures.append(f"reported status {payload.get('status')!r}")
        budget = self.policy.stale_budget(session_open)
        age = payload.get("last_bar_age_seconds")
        if isinstance(age, int | float) and float(age) > budget:
            failures.append(f"health surface is stale ({float(age):.0f}s > {budget:.0f}s budget)")
        unresolved = payload.get("unresolved_order_count")
        if isinstance(unresolved, int) and unresolved > 0:
            failures.append(f"{unresolved} unresolved order intent(s)")

        observation = Observation(
            at=at,
            reachable=True,
            healthy=not failures,
            session_open=session_open,
            maintenance=maintenance,
            failures=tuple(failures),
            payload=payload,
        )
        self.last_observation = observation
        return observation

    # -- escalation --------------------------------------------------------

    def build_message(self, observation: Observation) -> tuple[str, str]:
        subject = "health watchdog: unreachable" if not observation.reachable else (
            "health watchdog: degraded"
        )
        body = json.dumps(observation.as_dict(), ensure_ascii=False, sort_keys=True, indent=2)
        return subject, body

    def _backoff_delay(self) -> float:
        exponent = max(0, self.state.alerts_sent - 1)
        return min(
            self.policy.max_backoff_seconds, self.policy.backoff_seconds * (2**exponent)
        )

    def escalate(self, observation: Observation) -> bool:
        """Update escalation state and send at most one notification.

        Returns ``True`` when a notification was sent.
        """
        if observation.maintenance:
            # Observed, counted for the record, never alerted, never "recovered".
            self.state = replace(self.state, consecutive_failures=0)
            self._save_state()
            return False

        if observation.healthy:
            recovered = self.state.alerted
            self.state = EscalationState()
            self._save_state()
            if recovered:
                self.notifier.notify(
                    "health watchdog: recovered",
                    json.dumps(observation.as_dict(), ensure_ascii=False, sort_keys=True),
                )
                return True
            return False

        failures = self.state.consecutive_failures + 1
        self.state = replace(self.state, consecutive_failures=failures)
        if failures < self.policy.failures_before_alert:
            self._save_state()
            return False

        if self.state.alerted and self.state.next_alert_after:
            try:
                if observation.at < datetime.fromisoformat(self.state.next_alert_after):
                    self._save_state()
                    return False
            except ValueError:
                pass

        subject, body = self.build_message(observation)
        self.state = replace(
            self.state,
            alerted=True,
            alerts_sent=self.state.alerts_sent + 1,
            last_subject=subject,
        )
        next_alert = observation.at + timedelta(seconds=self._backoff_delay())
        self.state = replace(self.state, next_alert_after=next_alert.isoformat())
        self._save_state()
        self.notifier.notify(subject, body)
        return True

    def check(self, at: datetime | None = None) -> Observation:
        observation = self.observe(at)
        self.escalate(observation)
        return observation

    def run(self, *, once: bool = False, max_cycles: int | None = None) -> int:
        """Poll forever (or a bounded number of cycles) in this process.

        Returns 0 if the final observation was healthy, 2 otherwise, so a
        supervisor or a shell script can act on the exit status.
        """
        cycles = 0
        while True:
            observation = self.check()
            cycles += 1
            if once or (max_cycles is not None and cycles >= max_cycles):
                return 0 if observation.healthy else 2
            time.sleep(max(0.1, self.policy.poll_seconds))

    def status(self) -> dict[str, Any]:
        return {
            "policy": {
                "poll_seconds": self.policy.poll_seconds,
                "stale_open_seconds": self.policy.stale_open_seconds,
                "stale_closed_seconds": self.policy.stale_closed_seconds,
                "failures_before_alert": self.policy.failures_before_alert,
            },
            "escalation": self.state.as_dict(),
            "last_observation": (
                self.last_observation.as_dict() if self.last_observation else None
            ),
        }
