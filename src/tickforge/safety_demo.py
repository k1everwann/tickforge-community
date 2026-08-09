from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Settings
from .engine import TradingEngine
from .local_review import LocalModelReviewer
from .models import Action, Bar, Decision


@dataclass(frozen=True, slots=True)
class GateContext:
    simulation_mode: bool = True
    bars_complete: bool = True
    bars_chronological: bool = True
    long_only_action: bool = True
    position_slot_available: bool = True
    stop_defined: bool = True
    stop_within_limit: bool = True
    daily_risk_available: bool = True
    entry_not_extended: bool = True


@dataclass(frozen=True, slots=True)
class GateResult:
    number: int
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class AuditRecord:
    sequence: int
    stage: str
    decision: str
    reason: str

    def line(self) -> str:
        return json.dumps(
            {
                "decision": self.decision,
                "reason": self.reason,
                "sequence": self.sequence,
                "stage": self.stage,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


GATES = (
    ("隔離模式", "simulation_mode", "展示環境不是純模擬"),
    ("K 棒完整", "bars_complete", "行情包含未完成 K 棒"),
    ("時間順序", "bars_chronological", "K 棒時間順序異常"),
    ("只做多", "long_only_action", "候選方向超出允許集合"),
    ("單一部位", "position_slot_available", "已有未結束部位"),
    ("停損存在", "stop_defined", "候選缺少停損"),
    ("停損上限", "stop_within_limit", "候選停損超過硬上限"),
    ("當日風險", "daily_risk_available", "當日風險額度不可用"),
    ("禁止追價", "entry_not_extended", "候選已偏離進場參考線"),
)


class DeterministicGatePipeline:
    def evaluate(self, context: GateContext) -> list[GateResult]:
        results = []
        for number, (name, attribute, failure) in enumerate(GATES, start=1):
            passed = bool(getattr(context, attribute))
            results.append(
                GateResult(number, name, passed, "通過" if passed else failure)
            )
        return results


class StaticRuntime:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    def generate(self, prompt: str, timeout_seconds: float) -> str:
        del prompt, timeout_seconds
        self.calls += 1
        return self.response


class AlwaysOpenStrategy:
    def evaluate(self, bars, position) -> Decision:
        del bars
        if position is None:
            return Decision(Action.OPEN_LONG, "合成候選", 0.9, 1)
        return Decision(Action.HOLD, "已有部位")


def synthetic_bar(timestamp: datetime, index: int) -> Bar:
    base = 1_000 + index
    return Bar(
        timestamp=timestamp,
        open=base,
        high=base + 2,
        low=base - 2,
        close=base + 1,
        volume=100,
    )


def append_audit(lines: list[str], record: AuditRecord) -> None:
    lines.append("稽核紀錄 " + record.line())


def run_demo() -> str:
    lines = [
        "TickForge Safety Demo｜固定合成資料｜離線可重跑",
        "安全聲明：本流程只有模擬能力，不連接券商或真實市場。",
    ]
    sequence = 0
    pipeline = DeterministicGatePipeline()
    candidate = Decision(Action.OPEN_LONG, "合成候選", 0.9, 1)

    lines.append("")
    lines.append("[1/4] 九道確定性閘門先於模型")
    blocked_context = GateContext(entry_not_extended=False)
    gate_results = pipeline.evaluate(blocked_context)
    for result in gate_results:
        status = "PASS" if result.passed else "BLOCK"
        lines.append(f"閘門 {result.number}/9｜{result.name}｜{status}｜{result.reason}")
    blocked_runtime = StaticRuntime('{"decision":"accept","reason":"ignored"}')
    if all(result.passed for result in gate_results):
        LocalModelReviewer(blocked_runtime).review(candidate, [])
    decision = Decision(Action.HOLD, "確定性閘門已阻擋候選", 0)
    lines.append(f"模型呼叫次數：{blocked_runtime.calls}（預期 0）")
    lines.append("結論：規則先阻擋，候選未送進模型。")
    sequence += 1
    append_audit(
        lines,
        AuditRecord(sequence, "deterministic_gates", decision.action, decision.reason),
    )

    lines.append("")
    lines.append("[2/4] 模型只能收斂候選")
    pass_results = pipeline.evaluate(GateContext())
    if not all(result.passed for result in pass_results):
        raise AssertionError("all-pass gate fixture is invalid")
    reject_runtime = StaticRuntime('{"decision":"reject","reason":"證據互相矛盾"}')
    reviewed = LocalModelReviewer(reject_runtime).review(candidate, [])
    if reviewed.action is not Action.HOLD:
        raise AssertionError("veto-only reviewer failed to reject the synthetic candidate")
    lines.append("九道閘門：全部 PASS，候選才送交模型。")
    lines.append("模型回應：reject；最終決策：HOLD。")
    lines.append("結論：模型可否決，但不能擴張候選集合。")
    sequence += 1
    append_audit(lines, AuditRecord(sequence, "model_veto", reviewed.action, reviewed.reason))

    lines.append("")
    lines.append("[3/4] 注入額外欄位的模型輸出")
    injected_runtime = StaticRuntime(
        '{"decision":"accept","reason":"ok","action":"OPEN_LONG"}'
    )
    injected = LocalModelReviewer(injected_runtime).review(candidate, [])
    if injected.action is not Action.HOLD:
        raise AssertionError("invalid model output did not fail closed")
    lines.append("攻擊輸出：合法外觀 JSON 加入未授權 action 欄位。")
    lines.append("解析結果：schema 不符；最終決策：HOLD。")
    lines.append("結論：畸形或注入輸出一律 fail closed。")
    sequence += 1
    append_audit(lines, AuditRecord(sequence, "injected_output", injected.action, injected.reason))

    lines.append("")
    lines.append("[4/4] 委託狀態不明時封鎖新倉")
    with tempfile.TemporaryDirectory(prefix="tickforge-safety-demo-") as temporary:
        settings = Settings(
            db_path=Path(temporary) / "journal.sqlite3",
            control_token="demo-control-token-not-used".ljust(32, "x"),
        )
        engine = TradingEngine(settings)
        try:
            engine.strategy = AlwaysOpenStrategy()
            intent = engine.journal.start_intent("OPEN_LONG", {"synthetic": True})
            engine.journal.resolve(intent, "UNKNOWN", {"error": "synthetic timeout"})
            start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
            unresolved_decision = Decision(Action.HOLD, "等待完整 K 棒")
            for index in range(6):
                unresolved_decision = engine.on_bar(
                    synthetic_bar(start + timedelta(minutes=index), index)
                )
            unresolved_count = len(engine.journal.unresolved())
        finally:
            engine.close()
    if unresolved_decision.action is not Action.HOLD or unresolved_count != 1:
        raise AssertionError("unknown order did not block the synthetic entry")
    lines.append("合成事件：委託結果轉為 UNKNOWN。")
    lines.append("下一個進場候選：HOLD；未決委託數：1。")
    lines.append("結論：停止新增曝險，保留未決狀態並要求人工覆核。")
    sequence += 1
    append_audit(
        lines,
        AuditRecord(
            sequence,
            "unknown_order",
            unresolved_decision.action,
            unresolved_decision.reason,
        ),
    )

    lines.append("")
    lines.append("完成：4/4 場景通過；全程使用固定合成資料與模擬元件。")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the deterministic TickForge safety demo.")
    parser.add_argument("--transcript", type=Path, help="Optional backup transcript path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    transcript = run_demo()
    if args.transcript:
        args.transcript.parent.mkdir(parents=True, exist_ok=True)
        args.transcript.write_text(transcript, encoding="utf-8")
    print(transcript, end="")


if __name__ == "__main__":
    main()
