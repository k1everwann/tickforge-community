from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from .engine import TradingEngine
from .models import Bar

REQUIRED_COLUMNS = {"timestamp", "open", "high", "low", "close"}


def replay_csv(engine: TradingEngine, path: Path) -> dict[str, Any]:
    """Replay timezone-aware completed 1m OHLCV rows in chronological order."""
    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            engine.on_bar(
                Bar(
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                )
            )
            count += 1
    state = engine.state()
    return {
        "bars": count,
        "equity": state["equity"],
        "realized_pnl": state["realized_pnl"],
        "open_position": state["position"],
        "closed_trades": state["closed_trades"],
    }
