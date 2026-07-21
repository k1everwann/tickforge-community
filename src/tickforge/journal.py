from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class OrderJournal:
    """Durable order-intent journal; unresolved intent means fail closed."""

    FINAL_STATES = {"FILLED", "REJECTED", "CANCELLED", "FAILED"}

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS order_intents (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    result TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
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

    def start_intent(self, kind: str, payload: dict[str, Any]) -> str:
        intent_id = uuid.uuid4().hex
        now = self._now()
        with self._lock, self._connection:
            if self.unresolved():
                raise RuntimeError("an unresolved order intent already exists")
            self._connection.execute(
                "INSERT INTO order_intents VALUES (?, ?, 'PENDING', ?, NULL, ?, ?)",
                (intent_id, kind, json.dumps(payload, ensure_ascii=False), now, now),
            )
        return intent_id

    def resolve(self, intent_id: str, state: str, result: dict[str, Any]) -> None:
        if state not in self.FINAL_STATES and state != "UNKNOWN":
            raise ValueError(f"invalid intent state: {state}")
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE order_intents SET state = ?, result = ?, updated_at = ? WHERE id = ?",
                (state, json.dumps(result, ensure_ascii=False), self._now(), intent_id),
            )

    def unresolved(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM order_intents "
                "WHERE state NOT IN ('FILLED','REJECTED','CANCELLED','FAILED')"
            ).fetchall()
        return [dict(row) for row in rows]

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO events(kind, payload, created_at) VALUES (?, ?, ?)",
                (kind, json.dumps(payload, ensure_ascii=False), self._now()),
            )

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT kind, payload, created_at FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"kind": row["kind"], "payload": json.loads(row["payload"]),
             "created_at": row["created_at"]}
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._connection.close()
