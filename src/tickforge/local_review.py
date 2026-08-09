from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .models import Action, Bar, Decision

REVIEW_GBNF = r'''
root ::= "{" ws "\"decision\"" ws ":" ws decision "," ws "\"reason\"" ws ":" ws string ws "}"
decision ::= "\"accept\"" | "\"reject\""
string ::= "\"" char+ "\""
char ::= [^"\\\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F]{4})
ws ::= [ \t\n\r]*
'''.strip()


class ReviewChoice(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    decision: ReviewChoice
    reason: str


class LocalModelRuntime(Protocol):
    def generate(self, prompt: str, timeout_seconds: float) -> str: ...


class ModelRuntimeError(RuntimeError):
    """A local inference runtime failed without exposing its raw output."""


@dataclass(frozen=True, slots=True)
class LlamaCppRuntime:
    """Minimal llama.cpp CLI adapter for an untracked local GGUF model."""

    model_path: Path
    executable: str = "llama-cli"
    extra_args: tuple[str, ...] = ()

    def generate(self, prompt: str, timeout_seconds: float) -> str:
        command = [
            self.executable,
            "-m",
            str(self.model_path),
            "-n",
            "96",
            "--temp",
            "0",
            "-c",
            "2048",
            "-t",
            "2",
            "--grammar",
            REVIEW_GBNF,
            "--no-display-prompt",
            "--simple-io",
            *self.extra_args,
            "-p",
            prompt,
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ModelRuntimeError("timeout") from exc
        except OSError as exc:
            raise ModelRuntimeError("runtime unavailable") from exc
        if completed.returncode != 0:
            raise ModelRuntimeError("runtime exited unsuccessfully")
        return completed.stdout.strip()


def parse_review_verdict(raw: str) -> ReviewVerdict:
    """Parse the exact two-field schema; any ambiguity is an error."""

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("review output is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"decision", "reason"}:
        raise ValueError("review output does not match the required schema")
    decision = payload["decision"]
    reason = payload["reason"]
    if not isinstance(decision, str) or decision not in {
        choice.value for choice in ReviewChoice
    }:
        raise ValueError("review decision must be accept or reject")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 240:
        raise ValueError("review reason must contain 1 to 240 characters")
    return ReviewVerdict(ReviewChoice(decision), reason.strip())


def indicator_snapshot(bars: list[Bar]) -> dict[str, Any]:
    recent = bars[-20:]
    if not recent:
        return {"bar_count": 0}
    closes = [bar.close for bar in recent]
    last_five = recent[-5:]
    first_close = last_five[0].close
    momentum = ((last_five[-1].close / first_close) - 1) * 100 if first_close else 0
    return {
        "bar_count": len(bars),
        "latest_close": round(recent[-1].close, 4),
        "sma_5": round(sum(closes[-5:]) / min(5, len(closes)), 4),
        "sma_20": round(sum(closes) / len(closes), 4),
        "momentum_5_pct": round(momentum, 4),
        "average_range_5": round(
            sum(bar.high - bar.low for bar in last_five) / len(last_five), 4
        ),
        "latest_volume": round(recent[-1].volume, 4),
    }


def build_review_prompt(candidate: Decision, bars: list[Bar]) -> str:
    payload = {
        "candidate": candidate.as_dict(),
        "indicators": indicator_snapshot(bars),
    }
    return (
        "You are an offline safety reviewer. The deterministic strategy already chose "
        "the candidate. You may only accept it or reject it; never change direction, "
        "position size, stop, or action. Return exactly one JSON object with no markdown "
        "and exactly these fields: "
        '{"decision":"accept|reject","reason":"1-240 characters"}. '
        "Reject when evidence is insufficient or contradictory. Input: "
        + json.dumps(payload, separators=(",", ":"), sort_keys=True)
    )


@dataclass(slots=True)
class LocalModelReviewer:
    """Offline veto-only reviewer; failures reject new entries."""

    runtime: LocalModelRuntime
    timeout_seconds: float = 30

    def review(self, candidate: Decision, bars: list[Bar]) -> Decision:
        if candidate.action is not Action.OPEN_LONG:
            return candidate
        try:
            raw = self.runtime.generate(
                build_review_prompt(candidate, bars), self.timeout_seconds
            )
            verdict = parse_review_verdict(raw)
        except Exception:
            return Decision(Action.HOLD, "local model review failed closed", 0)
        if verdict.decision is ReviewChoice.REJECT:
            return Decision(Action.HOLD, f"local model rejected entry: {verdict.reason}", 0)
        return candidate
