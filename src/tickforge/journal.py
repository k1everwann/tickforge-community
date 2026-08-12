"""Durable order-intent journal with a full lifecycle state machine.

The lifecycle is:

    INTENT_CREATED -> SUBMITTING -> SUBMITTED
                                     |
        +----------------+-----------+-----------+----------------+
        |                |           |           |                |
      FILLED         REJECTED    CANCELLED     UNKNOWN      MANUAL_REVIEW
     (terminal)     (terminal)  (terminal)   (unresolved)    (unresolved)

Three properties matter more than the diagram:

1. **An intent is written down before the order exists.** The row is created,
   with ``synchronous=FULL``, before anything is sent. If the process dies at
   the worst possible moment, restart finds an ``INTENT_CREATED`` or
   ``SUBMITTING`` row and knows it must not assume anything.
2. **Terminal states are immutable.** Once an intent is ``FILLED``,
   ``REJECTED`` or ``CANCELLED``, no later code path can rewrite it. Late,
   duplicated or replayed callbacks cannot resurrect a finished order.
3. **A second intent is refused while one is unresolved.** ``UNKNOWN`` and
   ``MANUAL_REVIEW`` are *not* terminal: they are unresolved, deliberately, so
   that "we do not know" halts the system instead of being swept up. Clearing
   them requires a human.

Every transition is appended to ``order_transitions``, so the journal is an
audit log and not just current state.

Fields named ``external_*`` are opaque strings from whatever executes the
order. This project never interprets them and ships no real values in them.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Nothing may leave these states.
TERMINAL_STATES = frozenset({"FILLED", "REJECTED", "CANCELLED", "FAILED", "RECONCILED"})

#: These states mean "an order may exist and we do not know its fate".
UNRESOLVED_STATES = frozenset(
    {"INTENT_CREATED", "SUBMITTING", "SUBMITTED", "UNKNOWN", "MANUAL_REVIEW"}
)

ALL_STATES = TERMINAL_STATES | UNRESOLVED_STATES

_MAX_JSON = 12_000


class OrderStateError(RuntimeError):
    """An illegal transition, or a second intent while one is unresolved."""


class OrderJournal:
    """Durable order-intent journal; an unresolved intent means fail closed."""

    TERMINAL_STATES = TERMINAL_STATES
    UNRESOLVED_STATES = UNRESOLVED_STATES
    #: Retained name for callers written against the earlier binary model.
    FINAL_STATES = TERMINAL_STATES

    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS order_intents (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    result TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    external_order_id TEXT,
                    external_status TEXT,
                    error_text TEXT
                );
                CREATE INDEX IF NOT EXISTS order_intents_state
                    ON order_intents(state, updated_at);
                CREATE TABLE IF NOT EXISTS order_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id TEXT NOT NULL,
                    at TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS order_transitions_intent
                    ON order_transitions(intent_id, id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _dump(payload: Any) -> str:
        return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)[
            :_MAX_JSON
        ]

    # -- lifecycle ---------------------------------------------------------

    def start_intent(self, kind: str, payload: dict[str, Any]) -> str:
        """Record the intent to send an order, before anything is sent.

        Raises if any intent is still unresolved: refusing the second intent is
        the whole point of writing the first one down.
        """
        intent_id = uuid.uuid4().hex
        now = self._now()
        with self._lock, self._connection:
            unresolved = self.unresolved()
            if unresolved:
                raise OrderStateError(
                    "an unresolved order intent already exists: "
                    f"{unresolved[0]['id']} ({unresolved[0]['state']})"
                )
            self._connection.execute(
                "INSERT INTO order_intents"
                "(id, kind, state, payload, result, created_at, updated_at) "
                "VALUES (?, ?, 'INTENT_CREATED', ?, NULL, ?, ?)",
                (intent_id, kind, self._dump(payload), now, now),
            )
            self._append_transition(intent_id, None, "INTENT_CREATED", {}, now)
        return intent_id

    def transition(
        self,
        intent_id: str,
        to_state: str,
        detail: dict[str, Any] | None = None,
        *,
        result: dict[str, Any] | None = None,
        external_order_id: str | None = None,
        external_status: str | None = None,
        error_text: str | None = None,
    ) -> dict[str, Any]:
        """Move an intent to ``to_state`` and append an audit row.

        Refuses to move an intent out of a terminal state. Re-asserting the
        state an intent is already in is allowed and is a no-op transition, so
        duplicate callbacks are harmless.
        """
        to_state = str(to_state).upper()
        if to_state not in ALL_STATES:
            raise ValueError(f"invalid intent state: {to_state}")
        now = self._now()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM order_intents WHERE id = ?", (intent_id,)
            ).fetchone()
            if row is None:
                raise OrderStateError(f"unknown order intent: {intent_id}")
            if row["state"] in TERMINAL_STATES and to_state != row["state"]:
                raise OrderStateError(
                    f"terminal intent {intent_id} cannot move from {row['state']} to {to_state}"
                )
            assignments = ["state = ?", "updated_at = ?"]
            params: list[Any] = [to_state, now]
            for column, value in (
                ("result", None if result is None else self._dump(result)),
                ("external_order_id", external_order_id),
                ("external_status", external_status),
                ("error_text", error_text),
            ):
                if value is not None:
                    assignments.append(f"{column} = ?")
                    params.append(value)
            params.append(intent_id)
            self._connection.execute(
                f"UPDATE order_intents SET {', '.join(assignments)} WHERE id = ?", params
            )
            self._append_transition(intent_id, row["state"], to_state, detail or {}, now)
        return self.get(intent_id) or {}

    def _append_transition(
        self,
        intent_id: str,
        from_state: str | None,
        to_state: str,
        detail: dict[str, Any],
        at: str,
    ) -> None:
        self._connection.execute(
            "INSERT INTO order_transitions(intent_id, at, from_state, to_state, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (intent_id, at, from_state, to_state, self._dump(detail)),
        )

    def mark_submitting(self, intent_id: str) -> dict[str, Any]:
        """About to hand the order to an executor; the outcome is unknown."""
        return self.transition(intent_id, "SUBMITTING")

    def mark_submitted(
        self, intent_id: str, external_order_id: str | None = None
    ) -> dict[str, Any]:
        """Accepted by the executor, not yet resolved."""
        return self.transition(intent_id, "SUBMITTED", external_order_id=external_order_id)

    def mark_unknown(self, intent_id: str, error: str) -> dict[str, Any]:
        """The executor boundary failed in an ambiguous way. Stays unresolved."""
        return self.transition(intent_id, "UNKNOWN", {"error": str(error)}, error_text=str(error))

    def require_manual_review(
        self, intent_id: str, reason: str, detail: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Escalate to a human. Stays unresolved until a human clears it."""
        return self.transition(
            intent_id, "MANUAL_REVIEW", {"reason": reason, **(detail or {})}, error_text=reason
        )

    def resolve(self, intent_id: str, state: str, result: dict[str, Any]) -> None:
        """Compatibility wrapper over :meth:`transition`."""
        self.transition(intent_id, state, result, result=result)

    # -- queries -----------------------------------------------------------

    def get(self, intent_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM order_intents WHERE id = ?", (intent_id,)
            ).fetchone()
        return dict(row) if row else None

    def unresolved(self) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in UNRESOLVED_STATES)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM order_intents WHERE state IN ({placeholders}) ORDER BY created_at",
                tuple(sorted(UNRESOLVED_STATES)),
            ).fetchall()
        return [dict(row) for row in rows]

    def transitions(self, intent_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT at, from_state, to_state, detail FROM order_transitions "
                "WHERE intent_id = ? ORDER BY id",
                (intent_id,),
            ).fetchall()
        return [
            {
                "at": row["at"],
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "detail": json.loads(row["detail"]),
            }
            for row in rows
        ]

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM order_intents ORDER BY created_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self) -> dict[str, Any]:
        unresolved = self.unresolved()
        return {
            "unresolved_count": len(unresolved),
            "unresolved_states": sorted({row["state"] for row in unresolved}),
            "unresolved": unresolved[:5],
        }

    # -- events ------------------------------------------------------------

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO events(kind, payload, created_at) VALUES (?, ?, ?)",
                (kind, self._dump(payload), self._now()),
            )

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT kind, payload, created_at FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "kind": row["kind"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._connection.close()
