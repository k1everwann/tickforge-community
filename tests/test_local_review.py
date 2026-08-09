from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tickforge.local_review import LocalModelReviewer, parse_review_verdict
from tickforge.models import Action, Bar, Decision


class StaticRuntime:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls = 0

    def generate(self, prompt: str, timeout_seconds: float) -> str:
        self.calls += 1
        assert "indicators" in prompt
        assert timeout_seconds > 0
        return self.output


class TimeoutRuntime:
    def generate(self, prompt: str, timeout_seconds: float) -> str:
        raise TimeoutError("simulated timeout")


def bars() -> list[Bar]:
    start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    return [
        Bar(
            timestamp=start + timedelta(minutes=5 * index),
            open=100 + index,
            high=102 + index,
            low=99 + index,
            close=101 + index,
            volume=1000 + index,
        )
        for index in range(20)
    ]


def candidate(action: Action = Action.OPEN_LONG) -> Decision:
    return Decision(action, "deterministic setup", 0.91, 20)


def test_accept_returns_original_candidate_unchanged() -> None:
    original = candidate()
    runtime = StaticRuntime('{"decision":"accept","reason":"setup is coherent"}')
    reviewed = LocalModelReviewer(runtime).review(original, bars())
    assert reviewed is original
    assert runtime.calls == 1


def test_reject_can_only_veto_entry() -> None:
    runtime = StaticRuntime('{"decision":"reject","reason":"momentum conflicts"}')
    reviewed = LocalModelReviewer(runtime).review(candidate(), bars())
    assert reviewed.action is Action.HOLD
    assert reviewed.reason == "local model rejected entry: momentum conflicts"


def test_unparseable_output_fails_closed() -> None:
    reviewed = LocalModelReviewer(StaticRuntime("accept")).review(candidate(), bars())
    assert reviewed == Decision(Action.HOLD, "local model review failed closed", 0)


def test_schema_mismatch_fails_closed() -> None:
    output = '{"decision":"accept","reason":"ok","action":"OPEN_LONG"}'
    reviewed = LocalModelReviewer(StaticRuntime(output)).review(candidate(), bars())
    assert reviewed == Decision(Action.HOLD, "local model review failed closed", 0)


def test_timeout_fails_closed() -> None:
    reviewed = LocalModelReviewer(TimeoutRuntime(), timeout_seconds=0.01).review(
        candidate(), bars()
    )
    assert reviewed == Decision(Action.HOLD, "local model review failed closed", 0)


def test_close_is_never_sent_to_model() -> None:
    runtime = StaticRuntime('{"decision":"reject","reason":"irrelevant"}')
    original = candidate(Action.CLOSE)
    assert LocalModelReviewer(runtime).review(original, bars()) is original
    assert runtime.calls == 0


def test_schema_requires_exact_fields_and_nonempty_reason() -> None:
    for raw in (
        "[]",
        '{"decision":"maybe","reason":"x"}',
        '{"decision":"accept","reason":""}',
        '```json {"decision":"accept","reason":"x"} ```',
    ):
        try:
            parse_review_verdict(raw)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid output: {raw}")
