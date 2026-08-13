"""Authenticated, replay-resistant control requests.

A control API that can pause an engine or flatten a position needs more than a
shared secret, because a captured request can be replayed. Three checks together
make a captured request useless:

1. **Bearer token**, compared with :func:`hmac.compare_digest`.
2. **Timestamp** within a bounded clock skew, so an old capture is stale.
3. **Nonce**, single use within the skew window, so a fresh capture cannot be
   replayed even once.

The nonce store can be in memory or on disk. On disk (``synchronous=FULL``,
primary key on the nonce hash) replay protection survives a process restart,
which is when a naive in-memory implementation quietly forgets everything it was
protecting against.

Failure is always an exception. There is no "allow if unsure" branch, and there
is no network-location shortcut: this module deliberately ships **no** helper
that trusts a request because of the address it came from. Network topology is
not authentication, and a helper like that also leaks the operator's topology
into a public repository.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIN_TOKEN_LENGTH = 32
MIN_NONCE_LENGTH = 16
MAX_NONCE_LENGTH = 128
MIN_CLOCK_SKEW_SECONDS = 10
DEFAULT_CLOCK_SKEW_SECONDS = 60

TIMESTAMP_HEADER = "X-TickForge-Timestamp"
NONCE_HEADER = "X-TickForge-Nonce"
ACTOR_HEADER = "X-TickForge-Actor"

REDACTED_KEYS = frozenset({"token", "authorization", "control_token", "secret"})


class AuthenticationError(ValueError):
    """The request was not authenticated. Never treat this as a maybe."""


@dataclass(frozen=True, slots=True)
class AuthResult:
    actor: str
    nonce: str
    timestamp: int


class ControlAuthenticator:
    """Bearer authentication with timestamp and one-time nonce replay protection."""

    def __init__(
        self,
        token: str,
        max_clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS,
        clock: Callable[[], float] = time.time,
        replay_db_path: Path | None = None,
    ) -> None:
        self.token = str(token or "")
        self.max_clock_skew_seconds = max(MIN_CLOCK_SKEW_SECONDS, int(max_clock_skew_seconds))
        self.clock = clock
        self.replay_db_path = Path(replay_db_path) if replay_db_path else None
        self._seen: dict[str, float] = {}
        self._lock = threading.RLock()
        if self.replay_db_path:
            self.replay_db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.replay_db_path)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS control_nonces ("
                    "nonce_hash TEXT PRIMARY KEY, seen_at INTEGER NOT NULL)"
                )
                connection.commit()

    @property
    def configured(self) -> bool:
        """A token shorter than the minimum disables the control surface."""
        return len(self.token) >= MIN_TOKEN_LENGTH

    def authenticate(
        self, headers: Mapping[str, str], method: str, path: str, body: bytes = b""
    ) -> AuthResult:
        if not self.configured:
            raise AuthenticationError(
                f"control API disabled: configure a token of at least {MIN_TOKEN_LENGTH} characters"
            )
        authorization = str(headers.get("Authorization") or "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, self.token):
            raise AuthenticationError("control API authentication failed")
        try:
            timestamp = int(headers.get(TIMESTAMP_HEADER) or "0")
        except ValueError as exc:
            raise AuthenticationError("control API timestamp is malformed") from exc
        nonce = str(headers.get(NONCE_HEADER) or "").strip()
        if not MIN_NONCE_LENGTH <= len(nonce) <= MAX_NONCE_LENGTH:
            raise AuthenticationError(
                f"control API nonce must be {MIN_NONCE_LENGTH}-{MAX_NONCE_LENGTH} characters"
            )
        current = int(self.clock())
        if abs(current - timestamp) > self.max_clock_skew_seconds:
            raise AuthenticationError("control API request timestamp is outside the allowed skew")
        self._claim_nonce(nonce, current)
        actor = str(headers.get(ACTOR_HEADER) or "owner").strip()[:80] or "owner"
        del method, path, body  # bound by transport; reserved for signed-payload schemes
        return AuthResult(actor=actor, nonce=nonce, timestamp=timestamp)

    def _claim_nonce(self, nonce: str, current: int) -> None:
        replay_key = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        cutoff = current - self.max_clock_skew_seconds * 2
        with self._lock:
            if self.replay_db_path:
                try:
                    with closing(
                        sqlite3.connect(self.replay_db_path, timeout=30)
                    ) as connection:
                        connection.execute(
                            "DELETE FROM control_nonces WHERE seen_at < ?", (cutoff,)
                        )
                        connection.execute(
                            "INSERT INTO control_nonces(nonce_hash, seen_at) VALUES (?, ?)",
                            (replay_key, current),
                        )
                        connection.commit()
                except sqlite3.IntegrityError as exc:
                    raise AuthenticationError("control API detected a replayed request") from exc
                return
            self._seen = {
                key: seen_at for key, seen_at in self._seen.items() if seen_at >= cutoff
            }
            if replay_key in self._seen:
                raise AuthenticationError("control API detected a replayed request")
            self._seen[replay_key] = current


def audit_payload(auth: AuthResult, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Build an audit record for an authenticated control action.

    Credential-shaped keys are dropped rather than masked, so an audit log can
    never become the place a secret ends up.
    """
    safe = {key: value for key, value in (payload or {}).items() if key not in REDACTED_KEYS}
    return {
        "actor": auth.actor,
        "action": str(action),
        "nonce": auth.nonce,
        "request_timestamp": auth.timestamp,
        "payload": json.loads(json.dumps(safe, ensure_ascii=False, default=str)),
    }
