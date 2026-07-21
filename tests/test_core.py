from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tickforge.config import Settings
from tickforge.engine import TradingEngine
from tickforge.indicators import BarAggregator
from tickforge.journal import OrderJournal
from tickforge.models import Action, Bar, Decision


def bar(at: datetime, price: float, *, low: float | None = None) -> Bar:
    low = price - 2 if low is None else low
    return Bar(
        timestamp=at,
        open=price,
        high=price + 2,
        low=low,
        close=price,
        volume=100,
    )


def settings(tmp_path, **changes) -> Settings:
    base = Settings(db_path=tmp_path / "test.sqlite3", control_token="x" * 32)
    return replace(base, **changes)


class AlwaysOpen:
    def evaluate(self, bars, position):
        if position is None:
            return Decision(Action.OPEN_LONG, "test entry", 0.9, 20)
        return Decision(Action.HOLD, "test hold")


def warm_to_first_five(engine: TradingEngine, start: datetime) -> Decision:
    decision = Decision(Action.HOLD, "not started")
    for minute in range(6):
        decision = engine.on_bar(bar(start + timedelta(minutes=minute), 20_000 + minute))
    return decision


def test_bar_requires_timezone() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        bar(datetime(2026, 1, 1), 100)


def test_aggregator_emits_only_after_bucket_closes() -> None:
    aggregate = BarAggregator(5)
    start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    for minute in range(5):
        assert aggregate.push(bar(start + timedelta(minutes=minute), 100 + minute)) is None
    completed = aggregate.push(bar(start + timedelta(minutes=5), 110))
    assert completed is not None
    assert completed.timestamp == start
    assert completed.open == 100
    assert completed.close == 104


def test_non_loopback_binding_requires_strong_control_token(tmp_path) -> None:
    value = settings(tmp_path, host="0.0.0.0", control_token="")
    with pytest.raises(ValueError, match="32"):
        value.validate()


def test_public_config_never_exposes_control_token(tmp_path) -> None:
    value = settings(tmp_path)
    public = value.public_dict()
    assert "control_token" not in public
    assert public["control_token_configured"] is True


def test_order_journal_persists_unknown_state_and_fails_closed(tmp_path) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = OrderJournal(path)
    intent = journal.start_intent("OPEN_LONG", {"price": 100})
    journal.resolve(intent, "UNKNOWN", {"error": "timeout"})
    reopened = OrderJournal(path)
    assert reopened.unresolved()[0]["id"] == intent
    with pytest.raises(RuntimeError, match="unresolved"):
        reopened.start_intent("OPEN_LONG", {"price": 101})


def test_engine_opens_one_simulated_long_and_hard_stop_closes_it(tmp_path) -> None:
    engine = TradingEngine(settings(tmp_path))
    engine.strategy = AlwaysOpen()
    start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    decision = warm_to_first_five(engine, start)
    assert decision.action is Action.OPEN_LONG
    assert engine.broker.position is not None

    stop = engine.broker.position.stop_price
    stopped = Bar(
        timestamp=start + timedelta(minutes=6),
        open=stop - 5,
        high=stop - 1,
        low=stop - 8,
        close=stop - 4,
        volume=200,
    )
    decision = engine.on_bar(stopped)
    assert decision.action is Action.CLOSE
    assert engine.broker.position is None
    assert engine.broker.closed_trades[-1].reason == "hard stop reached"
    assert engine.broker.closed_trades[-1].net_pnl < 0


def test_unresolved_intent_blocks_strategy_entry(tmp_path) -> None:
    engine = TradingEngine(settings(tmp_path))
    engine.strategy = AlwaysOpen()
    engine.journal.start_intent("OPEN_LONG", {"test": True})
    start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    decision = warm_to_first_five(engine, start)
    assert decision.action is Action.HOLD
    assert "fail closed" in decision.reason
    assert engine.broker.position is None


def test_emergency_flat_requires_fresh_two_step_token(tmp_path) -> None:
    engine = TradingEngine(settings(tmp_path))
    engine.strategy = AlwaysOpen()
    start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    warm_to_first_five(engine, start)
    with pytest.raises(ValueError, match="invalid or expired"):
        engine.execute_emergency_flat("wrong")
    prepared = engine.prepare_emergency_flat()
    result = engine.execute_emergency_flat(prepared["confirmation_token"])
    assert result == {"closed": True, "paused": True}
    assert engine.broker.position is None
    with pytest.raises(ValueError, match="invalid or expired"):
        engine.execute_emergency_flat(prepared["confirmation_token"])
