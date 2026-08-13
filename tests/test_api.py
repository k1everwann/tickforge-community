from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from tickforge.api import create_app
from tickforge.config import Settings


def test_dashboard_health_and_demo_flow(tmp_path: Path) -> None:
    app = create_app(Settings(db_path=tmp_path / "api.sqlite3", control_token="t" * 32))
    client = TestClient(app)
    assert client.get("/").status_code == 200
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["simulation_only"] is True
    stepped = client.post("/api/demo/step?count=30")
    assert stepped.status_code == 200
    assert stepped.json()["state"]["latest_bar"] is not None


def test_control_api_requires_token(tmp_path: Path) -> None:
    token = "t" * 32
    app = create_app(Settings(db_path=tmp_path / "api.sqlite3", control_token=token))
    client = TestClient(app)
    assert client.post("/api/control/pause").status_code == 401
    response = client.post("/api/control/pause", headers={"X-TickForge-Token": token})
    assert response.status_code == 200
    assert response.json()["paused"] is True


def test_control_api_is_disabled_without_configured_token(tmp_path: Path) -> None:
    app = create_app(Settings(db_path=tmp_path / "api.sqlite3", control_token=""))
    client = TestClient(app)
    response = client.post("/api/control/pause", headers={"X-TickForge-Token": "anything"})
    assert response.status_code == 503


def test_replay_protected_mode_rejects_a_repeated_nonce(tmp_path: Path) -> None:
    token = "t" * 32
    app = create_app(Settings(db_path=tmp_path / "api.sqlite3", control_token=token))
    client = TestClient(app)
    request = {
        "Authorization": f"Bearer {token}",
        "X-TickForge-Timestamp": str(int(time.time())),
        "X-TickForge-Nonce": "api-nonce-abcdefghij",
    }
    assert client.post("/api/control/pause", headers=request).status_code == 200
    assert client.post("/api/control/pause", headers=request).status_code == 401


def test_reconciliation_endpoint_reports_a_clean_default(tmp_path: Path) -> None:
    app = create_app(Settings(db_path=tmp_path / "api.sqlite3", control_token="t" * 32))
    client = TestClient(app)
    payload = client.get("/api/reconciliation").json()
    assert payload["state"] == "IN_SYNC"
    assert payload["can_open"] is True


def test_emergency_flat_is_two_step_over_the_api(tmp_path: Path) -> None:
    token = "t" * 32
    app = create_app(Settings(db_path=tmp_path / "api.sqlite3", control_token=token))
    client = TestClient(app)
    auth = {"X-TickForge-Token": token}
    client.post("/api/demo/step?count=60", headers=auth)
    prepared = client.post("/api/emergency/prepare", headers=auth).json()
    assert prepared["expires_at"]
    stale = client.post(
        "/api/emergency/execute", headers=auth, json={"confirmation_token": "CONFIRM WHATEVER"}
    )
    assert stale.status_code == 409
    accepted = client.post(
        "/api/emergency/execute",
        headers=auth,
        json={
            "confirmation_token": prepared["confirmation_token"],
            "challenge_id": prepared["challenge_id"],
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["paused"] is True
