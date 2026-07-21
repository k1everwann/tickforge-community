from __future__ import annotations

from pathlib import Path

import pytest

from tickforge.config import Settings
from tickforge.engine import TradingEngine
from tickforge.replay import replay_csv


def test_sample_csv_replays(tmp_path: Path) -> None:
    engine = TradingEngine(Settings(db_path=tmp_path / "replay.sqlite3"))
    try:
        sample = Path(__file__).parents[1] / "examples" / "sample-bars.csv"
        result = replay_csv(engine, sample)
        assert result["bars"] == 6
        assert result["equity"] == 100_000
    finally:
        engine.close()


def test_replay_rejects_missing_columns(tmp_path: Path) -> None:
    source = tmp_path / "bad.csv"
    source.write_text("timestamp,close\n2026-01-02T09:00:00+08:00,100\n", encoding="utf-8")
    engine = TradingEngine(Settings(db_path=tmp_path / "bad.sqlite3"))
    try:
        with pytest.raises(ValueError, match="missing columns"):
            replay_csv(engine, source)
    finally:
        engine.close()
