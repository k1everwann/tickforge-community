from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .config import Settings
from .control_security import (
    NONCE_HEADER,
    AuthenticationError,
    ControlAuthenticator,
)
from .demo import DemoMarket
from .engine import TradingEngine
from .models import Bar


class BarInput(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = Field(default=0, ge=0)


class EmergencyInput(BaseModel):
    confirmation_token: str
    challenge_id: str | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    engine = TradingEngine(config)
    demo = DemoMarket()
    authenticator = ControlAuthenticator(
        config.control_token,
        replay_db_path=config.db_path.with_name(config.db_path.name + ".nonces"),
    )
    app = FastAPI(
        title="TickForge Community",
        version="0.1.0",
        description="Simulation-only trading research API",
    )
    app.state.engine = engine
    app.state.demo = demo

    def authorize(request: Request, x_tickforge_token: str = Header(default="")) -> None:
        """Simple shared-token check, upgraded on demand.

        Send an ``X-TickForge-Nonce`` header and the request is instead validated
        by :class:`~tickforge.control_security.ControlAuthenticator`, which also
        requires ``Authorization: Bearer``, a fresh timestamp, and a nonce that
        has not been used before. Prefer that mode for anything reachable beyond
        a loopback interface.
        """
        expected = config.control_token
        if len(expected) < 32:
            raise HTTPException(503, "control API disabled: configure a 32+ character token")
        if request.headers.get(NONCE_HEADER):
            try:
                authenticator.authenticate(
                    request.headers, request.method, request.url.path
                )
            except AuthenticationError as exc:
                raise HTTPException(401, str(exc)) from exc
            return
        if not secrets.compare_digest(x_tickforge_token, expected):
            raise HTTPException(401, "invalid control token")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")

    @app.get("/api/health")
    def health() -> dict:
        return engine.health()

    @app.get("/api/state")
    def state() -> dict:
        return engine.state()

    @app.post("/api/bars", dependencies=[Depends(authorize)])
    def ingest_bar(item: BarInput) -> dict:
        decision = engine.on_bar(Bar(**item.model_dump()))
        return {"decision": decision.as_dict(), "state": engine.state()}

    @app.post("/api/demo/step")
    def demo_step(count: int = 1) -> dict:
        count = max(1, min(count, 500))
        decision = None
        for _ in range(count):
            decision = engine.on_bar(demo.next_bar())
        return {"decision": decision.as_dict() if decision else None, "state": engine.state()}

    @app.post("/api/control/pause", dependencies=[Depends(authorize)])
    def pause() -> dict:
        engine.pause()
        return engine.state()

    @app.post("/api/control/resume", dependencies=[Depends(authorize)])
    def resume() -> dict:
        try:
            engine.resume()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return engine.state()

    @app.post("/api/emergency/prepare", dependencies=[Depends(authorize)])
    def emergency_prepare() -> dict:
        return engine.prepare_emergency_flat()

    @app.post("/api/emergency/execute", dependencies=[Depends(authorize)])
    def emergency_execute(item: EmergencyInput) -> dict:
        try:
            return engine.execute_emergency_flat(item.confirmation_token, item.challenge_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/reconciliation")
    def reconciliation() -> dict:
        return engine.reconcile().as_dict()

    return app
