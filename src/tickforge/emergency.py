"""Durable two-step authorisation for emergency position flattening.

An emergency control is the most dangerous button in a trading system: it is
reached for under stress, it is irreversible, and a stray retry can be worse
than the incident. This coordinator makes the button cost two deliberate steps
and binds the second step to the exact situation the operator was looking at.

The mechanism:

* **Prepare** writes a challenge to disk (``synchronous=FULL``) and returns a
  confirmation phrase. Only the hash of the phrase is stored.
* **Consume** requires the same challenge id, the same actor, the correct
  phrase, an unexpired TTL, and an unchanged **position fingerprint**.
* The challenge is **single use**: consuming it marks it ``CONSUMED`` in the
  same transaction, so a replayed confirmation is rejected rather than acted on.
* Because the challenge is on disk, a restart between the two steps does not
  lose it and does not silently widen the window.

The position fingerprint is the part worth copying. It is a SHA-256 of the
position snapshot taken at prepare time. If the position changed at all between
prepare and consume - it closed, it flipped, size changed - the confirmation is
refused and the operator has to look again. Confirming a flatten for a position
that no longer exists is how a flatten becomes a new position.

Comparisons use :func:`hmac.compare_digest`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 300
DEFAULT_TTL_SECONDS = 120


class EmergencyFlowError(ValueError):
    """The two-step authorisation was not satisfied."""


class EmergencyCoordinator:
    """Durable, single-use, position-bound authorisation for a flatten."""

    def __init__(self, path: Path, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(MIN_TTL_SECONDS, min(int(ttl_seconds), MAX_TTL_SECONDS))
        self._lock = threading.RLock()
        self._init_db()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS emergency_challenges (
                    challenge_id TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    phrase_hash TEXT NOT NULL,
                    position_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    consumed_at TEXT
                );
                """
            )

    @staticmethod
    def position_fingerprint(position: Any) -> str:
        """SHA-256 over a canonical JSON rendering of the position snapshot.

        Pass only fields that identify the position, not fields that move with
        every tick, or the fingerprint will invalidate itself.
        """
        canonical = json.dumps(position or {}, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def prepare(
        self, actor: str, position: Any, detail: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        now = self._now()
        challenge_id = secrets.token_urlsafe(18)
        phrase = f"CONFIRM EMERGENCY FLATTEN {challenge_id[-6:]}"
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO emergency_challenges"
                "(challenge_id, actor, created_at, expires_at, phrase_hash,"
                " position_fingerprint, status, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, 'PREPARED', ?)",
                (
                    challenge_id,
                    str(actor),
                    now.isoformat(),
                    expires_at.isoformat(),
                    hashlib.sha256(phrase.encode("utf-8")).hexdigest(),
                    self.position_fingerprint(position),
                    json.dumps(detail or {}, ensure_ascii=False, sort_keys=True, default=str)[
                        :12_000
                    ],
                ),
            )
        return {
            "challenge_id": challenge_id,
            "confirmation_phrase": phrase,
            "expires_at": expires_at.isoformat(),
            "position": position,
        }

    def consume(
        self, challenge_id: str, actor: str, phrase: str, current_position: Any
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM emergency_challenges WHERE challenge_id = ?", (str(challenge_id),)
            ).fetchone()
            if row is None:
                raise EmergencyFlowError("unknown emergency confirmation")
            if row["status"] != "PREPARED":
                raise EmergencyFlowError("emergency confirmation was already used or invalidated")
            if not hmac.compare_digest(str(row["actor"]), str(actor)):
                raise EmergencyFlowError("emergency confirmation belongs to a different actor")
            if self._now() > datetime.fromisoformat(row["expires_at"]):
                connection.execute(
                    "UPDATE emergency_challenges SET status = 'EXPIRED' WHERE challenge_id = ?",
                    (challenge_id,),
                )
                raise EmergencyFlowError("emergency confirmation has expired")
            supplied = hashlib.sha256(str(phrase).encode("utf-8")).hexdigest()
            if not hmac.compare_digest(supplied, str(row["phrase_hash"])):
                raise EmergencyFlowError("emergency confirmation phrase is incorrect")
            if not hmac.compare_digest(
                self.position_fingerprint(current_position), str(row["position_fingerprint"])
            ):
                raise EmergencyFlowError(
                    "position changed after preparation; confirm again against the new position"
                )
            consumed_at = self._now().isoformat()
            connection.execute(
                "UPDATE emergency_challenges SET status = 'CONSUMED', consumed_at = ? "
                "WHERE challenge_id = ? AND status = 'PREPARED'",
                (consumed_at, challenge_id),
            )
        return {"challenge_id": challenge_id, "actor": str(actor), "consumed_at": consumed_at}

    def invalidate_all(self, reason: str = "invalidated") -> int:
        """Drop every outstanding challenge, e.g. after a state change."""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE emergency_challenges SET status = 'INVALIDATED', detail = ? "
                "WHERE status = 'PREPARED'",
                (json.dumps({"reason": str(reason)}, ensure_ascii=False),),
            )
            return int(cursor.rowcount or 0)

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM emergency_challenges GROUP BY status"
            ).fetchall()
        return {"ttl_seconds": self.ttl_seconds, "counts": {r["status"]: r["total"] for r in rows}}
