from __future__ import annotations

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
