from __future__ import annotations

from typing import Protocol

from .models import Bar, Decision


class DecisionReviewer(Protocol):
    """Extension point for a rule engine or an external AI reviewer."""

    def review(self, candidate: Decision, bars: list[Bar]) -> Decision: ...


class PassThroughReviewer:
    """Default reviewer: deterministic, offline, and easy to audit."""

    def review(self, candidate: Decision, bars: list[Bar]) -> Decision:
        return candidate
