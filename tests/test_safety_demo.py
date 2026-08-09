from __future__ import annotations

from tickforge.safety_demo import (
    DeterministicGatePipeline,
    GateContext,
    run_demo,
)


def test_nine_gates_have_one_real_block() -> None:
    results = DeterministicGatePipeline().evaluate(
        GateContext(entry_not_extended=False)
    )
    assert len(results) == 9
    assert sum(not result.passed for result in results) == 1
    assert results[-1].name == "禁止追價"


def test_demo_is_byte_for_byte_repeatable() -> None:
    first = run_demo()
    second = run_demo()
    assert first == second


def test_demo_exercises_all_four_safety_claims() -> None:
    output = run_demo()
    assert "模型呼叫次數：0" in output
    assert "模型回應：reject；最終決策：HOLD" in output
    assert "schema 不符；最終決策：HOLD" in output
    assert "委託結果轉為 UNKNOWN" in output
    assert output.count("稽核紀錄 ") == 4
    assert "4/4 場景通過" in output
