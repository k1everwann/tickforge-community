from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import replace
from pathlib import Path

import uvicorn

from .api import create_app
from .config import Settings
from .demo import DemoMarket
from .engine import TradingEngine
from .monitor import monitor
from .replay import replay_csv
from .watchdog import (
    HealthWatchdog,
    HttpHealthProbe,
    PrintNotifier,
    WatchdogPolicy,
)

DEFAULT_HEALTH_URL = "http://127.0.0.1:8080/api/health"


def run_demo(count: int) -> None:
    with tempfile.TemporaryDirectory() as directory:
        settings = replace(
            Settings.from_env(), db_path=Path(directory) / "demo.sqlite3", control_token=""
        )
        engine = TradingEngine(settings)
        market = DemoMarket()
        for _ in range(count):
            engine.on_bar(market.next_bar())
        state = engine.state()
        print(json.dumps({
            "mode": state["mode"],
            "equity": state["equity"],
            "realized_pnl": state["realized_pnl"],
            "position": state["position"],
            "closed_trades": state["closed_trades"],
        }, ensure_ascii=False, indent=2))
        engine.close()


def run_replay(path: Path) -> None:
    with tempfile.TemporaryDirectory() as directory:
        settings = replace(
            Settings.from_env(), db_path=Path(directory) / "replay.sqlite3", control_token=""
        )
        engine = TradingEngine(settings)
        try:
            print(json.dumps(replay_csv(engine, path), ensure_ascii=False, indent=2))
        finally:
            engine.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="TickForge Community simulation tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="start the local dashboard and API")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    demo = subparsers.add_parser("demo", help="run a deterministic simulation")
    demo.add_argument("--bars", type=int, default=600)
    replay = subparsers.add_parser("replay", help="replay completed one-minute bars from CSV")
    replay.add_argument("path", type=Path)
    health = subparsers.add_parser("monitor", help="poll health independently from the service")
    health.add_argument("--url", default="http://127.0.0.1:5003/api/health")
    health.add_argument("--interval", type=float, default=10)
    health.add_argument("--once", action="store_true")
    watch = subparsers.add_parser(
        "watchdog",
        help="run the escalating out-of-process health watchdog (run it as its own process)",
    )
    watch.add_argument("--url", default=DEFAULT_HEALTH_URL)
    watch.add_argument("--interval", type=float, default=WatchdogPolicy().poll_seconds)
    watch.add_argument(
        "--stale-open-seconds", type=float, default=WatchdogPolicy().stale_open_seconds
    )
    watch.add_argument(
        "--failures-before-alert", type=int, default=WatchdogPolicy().failures_before_alert
    )
    watch.add_argument("--state-path", type=Path, default=None)
    watch.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.command == "demo":
        run_demo(max(1, args.bars))
        return
    if args.command == "replay":
        run_replay(args.path)
        return
    if args.command == "monitor":
        raise SystemExit(monitor(args.url, args.interval, args.once))
    if args.command == "watchdog":
        watchdog = HealthWatchdog(
            HttpHealthProbe(args.url),
            notifier=PrintNotifier(),
            policy=WatchdogPolicy(
                poll_seconds=args.interval,
                stale_open_seconds=args.stale_open_seconds,
                failures_before_alert=args.failures_before_alert,
            ),
            state_path=args.state_path,
        )
        raise SystemExit(watchdog.run(once=args.once))

    settings = Settings.from_env()
    if args.host or args.port:
        settings = replace(
            settings,
            host=args.host or settings.host,
            port=args.port or settings.port,
        )
    settings.validate()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
