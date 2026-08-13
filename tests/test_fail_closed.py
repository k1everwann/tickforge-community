"""Fail-closed and one-way-convergence tests.

Grouped into ``*Tests`` classes, one per module under test, so a reader can see
at a glance which safety property belongs to which component.

Every fixture here is synthetic and defined in this file. Nothing in this suite
reaches a network, an execution venue, or any deployment.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tickforge.config import Settings
from tickforge.control_security import (
    ACTOR_HEADER,
    NONCE_HEADER,
    TIMESTAMP_HEADER,
    AuthenticationError,
    ControlAuthenticator,
    audit_payload,
)
from tickforge.emergency import (
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    EmergencyCoordinator,
    EmergencyFlowError,
)
from tickforge.engine import TradingEngine
from tickforge.gates import (
    EXAMPLE_GATES,
    ExampleReentryCooldownGate,
    ExampleSessionWindowGate,
    ExampleSpreadGate,
    GateChain,
    GateContext,
    GateOutcome,
    GateResult,
    NotPausedGate,
    NoUnresolvedOrderIntentGate,
    StateReconciledGate,
)
from tickforge.local_review import LocalModelReviewer, ModelRuntimeError
from tickforge.models import Action, Bar, Decision
from tickforge.reconcile import (
    SIMULATED_SYMBOL,
    ExternalPosition,
    LocalPosition,
    Reconciler,
    ReconciliationState,
)
from tickforge.watchdog import (
    AlwaysOpenCalendar,
    EscalationState,
    HealthWatchdog,
    NullNotifier,
    WatchdogPolicy,
)

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


# -- synthetic fixtures ----------------------------------------------------


def bar(at: datetime, price: float) -> Bar:
    return Bar(timestamp=at, open=price, high=price + 2, low=price - 2, close=price, volume=100)


def settings(tmp_path, **changes) -> Settings:
    base = Settings(db_path=tmp_path / "test.sqlite3", control_token="x" * 32)
    return replace(base, **changes)


class AlwaysOpen:
    """Synthetic strategy that always wants a position."""

    def evaluate(self, bars, position):
        if position is None:
            return Decision(Action.OPEN_LONG, "synthetic entry", 0.9, 20)
        return Decision(Action.HOLD, "synthetic hold")


class CountingReviewer:
    """Records whether the model layer was consulted at all."""

    def __init__(self) -> None:
        self.calls = 0

    def review(self, candidate, bars):
        self.calls += 1
        return candidate


class StaticGate:
    """Synthetic gate with a fixed verdict."""

    def __init__(self, name: str, allowed: bool) -> None:
        self.name = name
        self.allowed = allowed
        self.calls = 0

    def check(self, context: GateContext) -> GateResult:
        self.calls += 1
        return GateResult(allowed=self.allowed, reason=f"{self.name}:{self.allowed}",
                          gate_name=self.name)


class RaisingPositions:
    def positions(self):
        raise RuntimeError("synthetic external outage")


class FixedPositions:
    def __init__(self, positions: list[ExternalPosition]) -> None:
        self._positions = positions

    def positions(self):
        return list(self._positions)


class AcceptingFlattener:
    def flatten(self, position: ExternalPosition) -> bool:
        del position
        return True


class ScriptedProbe:
    """Health surface stand-in returning a scripted sequence of payloads."""

    def __init__(self, payloads: list[dict | Exception]) -> None:
        self.payloads = list(payloads)
        self.index = 0

    def probe(self) -> dict:
        payload = self.payloads[min(self.index, len(self.payloads) - 1)]
        self.index += 1
        if isinstance(payload, Exception):
            raise payload
        return dict(payload)


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def notify(self, subject: str, body: str) -> None:
        self.sent.append((subject, body))


class ClosedCalendar:
    def is_open(self, at: datetime) -> bool:
        del at
        return False

    def in_maintenance(self, at: datetime) -> bool:
        del at
        return False


class MaintenanceCalendar:
    def is_open(self, at: datetime) -> bool:
        del at
        return True

    def in_maintenance(self, at: datetime) -> bool:
        del at
        return True


def headers(token: str, timestamp: int, nonce: str, actor: str = "tester") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        TIMESTAMP_HEADER: str(timestamp),
        NONCE_HEADER: nonce,
        ACTOR_HEADER: actor,
    }


# -- gates -----------------------------------------------------------------


class GateChainTests:
    def test_chain_stops_at_the_first_rejection(self) -> None:
        first = StaticGate("first", True)
        blocker = StaticGate("blocker", False)
        never = StaticGate("never", True)
        outcome = GateChain((first, blocker, never)).evaluate(GateContext(at=START))
        assert outcome.allowed is False
        assert outcome.rejected_by == "blocker"
        assert never.calls == 0
        assert [item.gate_name for item in outcome.results] == ["first", "blocker"]

    def test_a_later_gate_cannot_overturn_an_earlier_rejection(self) -> None:
        rejected = GateChain((StaticGate("blocker", False),)).evaluate(GateContext(at=START))
        passing = GateChain((StaticGate("permissive", True),)).evaluate(GateContext(at=START))
        assert rejected.narrow(passing).allowed is False
        assert rejected.narrow(passing).rejected_by == "blocker"
        assert rejected.narrow(passing).reason == rejected.reason

    def test_narrowing_is_monotonic_in_both_directions(self) -> None:
        allowed = GateOutcome(allowed=True, reason="ok")
        blocked = GateOutcome(allowed=False, reason="no", rejected_by="g")
        assert allowed.narrow(blocked).allowed is False
        assert blocked.narrow(allowed).allowed is False
        assert allowed.narrow(allowed).allowed is True

    def test_one_way_convergence_over_randomised_orderings(self) -> None:
        """Only an all-passing chain ever yields allowed=True.

        Exhaustive over every pass/fail combination up to length four, then
        randomised over longer chains with shuffled orderings.
        """
        for length in range(1, 5):
            for flags in itertools.product([True, False], repeat=length):
                gates = [StaticGate(f"g{index}", flag) for index, flag in enumerate(flags)]
                outcome = GateChain(tuple(gates)).evaluate(GateContext(at=START))
                assert outcome.allowed is all(flags)
                if not outcome.allowed:
                    first_failure = flags.index(False)
                    assert len(outcome.results) == first_failure + 1
                    assert all(gate.calls == 0 for gate in gates[first_failure + 1 :])

        rng = random.Random(20260812)
        for _ in range(300):
            flags = [rng.random() > 0.35 for _ in range(rng.randint(1, 9))]
            gates = [StaticGate(f"g{index}", flag) for index, flag in enumerate(flags)]
            rng.shuffle(gates)
            outcome = GateChain(tuple(gates)).evaluate(GateContext(at=START))
            assert outcome.allowed is all(gate.allowed for gate in gates)
            # Folding any further outcome in can never re-admit a rejection.
            for extra in (
                GateOutcome(allowed=True, reason="ok"),
                GateOutcome(allowed=False, reason="no", rejected_by="extra"),
            ):
                combined = outcome.narrow(extra)
                assert combined.allowed is (outcome.allowed and extra.allowed)

    def test_unresolved_intent_and_missing_reconciliation_both_block(self) -> None:
        unresolved = NoUnresolvedOrderIntentGate().check(
            GateContext(at=START, unresolved_orders=({"id": "abc", "state": "UNKNOWN"},))
        )
        assert unresolved.allowed is False
        assert unresolved.reason == "unresolved order intent; fail closed"
        # A reconciliation that has never run is a rejection, not a pass.
        assert StateReconciledGate().check(GateContext(at=START)).allowed is False

    def test_example_gates_are_illustrative_but_still_fail_closed(self) -> None:
        """The example gates ship with invented values and are not enabled by default.

        They are tested because they are shipped code, and because the shape they
        illustrate matters: a missing measurement is a rejection, not a pass.
        """
        session = ExampleSessionWindowGate()
        assert session.check(GateContext(at=START)).allowed is True
        midnight = START.replace(hour=0, minute=30)
        assert session.check(GateContext(at=midnight)).allowed is False

        cooldown = ExampleReentryCooldownGate(cooldown=timedelta(minutes=11))
        assert cooldown.check(GateContext(at=START)).allowed is True
        assert (
            cooldown.check(
                GateContext(at=START, last_exit_at=START - timedelta(minutes=2))
            ).allowed
            is False
        )
        assert (
            cooldown.check(
                GateContext(at=START, last_exit_at=START - timedelta(minutes=30))
            ).allowed
            is True
        )

        spread = ExampleSpreadGate(maximum_points=7)
        assert spread.check(GateContext(at=START, spread_points=1)).allowed is True
        assert spread.check(GateContext(at=START, spread_points=99)).allowed is False
        # No measurement at all is a rejection.
        assert spread.check(GateContext(at=START)).allowed is False

    def test_example_gates_are_not_wired_in_by_default(self, tmp_path) -> None:
        engine = TradingEngine(settings(tmp_path))
        try:
            names = engine.pre_model_gates.names + engine.post_model_gates.names
        finally:
            engine.close()
        assert not [name for name in names if name.startswith("example")]

    def test_example_gates_can_be_composed_into_a_chain(self, tmp_path) -> None:
        engine = TradingEngine(
            settings(tmp_path),
            pre_model_gates=GateChain(
                (NoUnresolvedOrderIntentGate(), StateReconciledGate(), *EXAMPLE_GATES),
                label="pre_model",
            ),
        )
        engine.strategy = AlwaysOpen()
        try:
            decision = Decision(Action.HOLD, "not started")
            for minute in range(6):
                decision = engine.on_bar(bar(START + timedelta(minutes=minute), 20_000 + minute))
        finally:
            engine.close()
        # The example spread gate has no measurement to look at, so it blocks.
        assert decision.action is Action.HOLD
        assert "spread" in decision.reason

    def test_pause_blocks_entries_but_never_exits(self) -> None:
        gate = NotPausedGate()
        entry = Decision(Action.OPEN_LONG, "entry", 0.9, 10)
        exit_ = Decision(Action.CLOSE, "exit", 1.0)
        assert gate.check(GateContext(at=START, candidate=entry, paused=True)).allowed is False
        assert gate.check(GateContext(at=START, candidate=exit_, paused=True)).allowed is True


# -- reconciliation --------------------------------------------------------


class ReconciliationTests:
    def test_lookup_failure_is_error_and_blocks_new_exposure(self) -> None:
        report = Reconciler(RaisingPositions()).reconcile(None)
        assert report.state is ReconciliationState.ERROR
        assert report.can_open is False
        assert report.in_sync is False

    def test_both_flat_is_in_sync(self) -> None:
        report = Reconciler(FixedPositions([])).reconcile(None)
        assert report.state is ReconciliationState.IN_SYNC
        assert report.can_open is True

    def test_local_position_without_external_position_is_mismatch(self) -> None:
        report = Reconciler(FixedPositions([])).reconcile(
            LocalPosition(SIMULATED_SYMBOL, 1)
        )
        assert report.state is ReconciliationState.MISMATCH
        assert report.can_open is False

    def test_matching_single_position_is_in_sync_but_still_forbids_a_second(self) -> None:
        external = [ExternalPosition(SIMULATED_SYMBOL, 1, "long", 100.0)]
        report = Reconciler(FixedPositions(external)).reconcile(
            LocalPosition(SIMULATED_SYMBOL, 1)
        )
        assert report.state is ReconciliationState.IN_SYNC
        assert report.can_open is False

    def test_differing_details_are_a_mismatch(self) -> None:
        external = [ExternalPosition("SOMETHING-ELSE", 1, "long", 100.0)]
        report = Reconciler(FixedPositions(external)).reconcile(
            LocalPosition(SIMULATED_SYMBOL, 1)
        )
        assert report.state is ReconciliationState.MISMATCH

    def test_strict_mode_never_liquidates_an_orphan_position(self) -> None:
        external = [ExternalPosition("UNKNOWN-TO-US", 2, "long", 100.0)]
        flattener = AcceptingFlattener()
        report = Reconciler(
            FixedPositions(external), strict=True, flattener=flattener
        ).reconcile(None)
        assert report.state is ReconciliationState.ORPHAN_POSITION
        assert report.can_open is False
        assert report.action == "MANUAL_INTERVENTION_REQUIRED"

    def test_non_strict_mode_may_close_an_orphan_and_still_blocks_entries(self) -> None:
        external = [ExternalPosition("UNKNOWN-TO-US", 1, "long", 100.0)]
        report = Reconciler(
            FixedPositions(external), strict=False, flattener=AcceptingFlattener()
        ).reconcile(None)
        assert report.state is ReconciliationState.ORPHAN_CLOSED
        assert report.can_open is False

    def test_extra_external_positions_are_a_mismatch(self) -> None:
        external = [
            ExternalPosition(SIMULATED_SYMBOL, 1, "long", 100.0),
            ExternalPosition(SIMULATED_SYMBOL, 1, "long", 101.0),
        ]
        report = Reconciler(FixedPositions(external)).reconcile(
            LocalPosition(SIMULATED_SYMBOL, 1)
        )
        assert report.state is ReconciliationState.MISMATCH


# -- engine ordering -------------------------------------------------------


class EngineOrderingTests:
    def warm(self, engine: TradingEngine, count: int = 6) -> Decision:
        decision = Decision(Action.HOLD, "not started")
        for minute in range(count):
            decision = engine.on_bar(bar(START + timedelta(minutes=minute), 20_000 + minute))
        return decision

    def test_pre_model_gate_rejection_never_calls_the_reviewer(self, tmp_path) -> None:
        reviewer = CountingReviewer()
        engine = TradingEngine(settings(tmp_path), reviewer)
        engine.strategy = AlwaysOpen()
        engine.journal.start_intent("OPEN_LONG", {"synthetic": True})
        try:
            decision = self.warm(engine)
        finally:
            engine.close()
        assert decision.action is Action.HOLD
        assert decision.reason == "unresolved order intent; fail closed"
        assert reviewer.calls == 0

    def test_reviewer_runs_once_a_candidate_is_permitted(self, tmp_path) -> None:
        reviewer = CountingReviewer()
        engine = TradingEngine(settings(tmp_path), reviewer)
        engine.strategy = AlwaysOpen()
        try:
            decision = self.warm(engine)
        finally:
            engine.close()
        assert decision.action is Action.OPEN_LONG
        assert reviewer.calls == 1

    def test_reconciliation_error_blocks_new_exposure(self, tmp_path) -> None:
        reviewer = CountingReviewer()
        engine = TradingEngine(
            settings(tmp_path), reviewer, reconciler=Reconciler(RaisingPositions())
        )
        engine.strategy = AlwaysOpen()
        try:
            decision = self.warm(engine)
            state = engine.state()
        finally:
            engine.close()
        assert decision.action is Action.HOLD
        assert "ERROR" in decision.reason
        assert engine.broker.position is None
        assert reviewer.calls == 0
        assert state["reconciliation"]["state"] == "ERROR"
        assert state["health"]["status"] == "degraded"
        assert state["gates"]["last_outcome"]["rejected_by"] == "state_reconciled"

    def test_orphan_external_position_blocks_new_exposure(self, tmp_path) -> None:
        external = FixedPositions([ExternalPosition("UNKNOWN-TO-US", 1, "long", 100.0)])
        engine = TradingEngine(settings(tmp_path), reconciler=Reconciler(external, strict=True))
        engine.strategy = AlwaysOpen()
        try:
            decision = self.warm(engine)
        finally:
            engine.close()
        assert decision.action is Action.HOLD
        assert "ORPHAN_POSITION" in decision.reason
        assert engine.broker.position is None

    def test_paused_engine_blocks_entries_after_review(self, tmp_path) -> None:
        engine = TradingEngine(settings(tmp_path))
        engine.strategy = AlwaysOpen()
        engine.pause()
        try:
            decision = self.warm(engine)
        finally:
            engine.close()
        assert decision.action is Action.HOLD
        assert decision.reason == "engine paused; new positions are disabled"


# -- model review ----------------------------------------------------------


class ModelReviewFailClosedTests:
    def entry(self) -> Decision:
        return Decision(Action.OPEN_LONG, "synthetic entry", 0.9, 20)

    def test_timeout_holds(self) -> None:
        class TimingOutRuntime:
            def generate(self, prompt, timeout_seconds):
                del prompt, timeout_seconds
                raise ModelRuntimeError("timeout")

        decision = LocalModelReviewer(TimingOutRuntime()).review(self.entry(), [])
        assert decision.action is Action.HOLD
        assert decision.reason == "local model review failed closed"

    def test_unparseable_output_holds(self) -> None:
        class GarbageRuntime:
            def generate(self, prompt, timeout_seconds):
                del prompt, timeout_seconds
                return "certainly! {'decision': maybe}"

        decision = LocalModelReviewer(GarbageRuntime()).review(self.entry(), [])
        assert decision.action is Action.HOLD

    def test_engine_holds_when_the_model_layer_fails(self, tmp_path) -> None:
        class ExplodingRuntime:
            def generate(self, prompt, timeout_seconds):
                del prompt, timeout_seconds
                raise ModelRuntimeError("runtime unavailable")

        engine = TradingEngine(settings(tmp_path), LocalModelReviewer(ExplodingRuntime()))
        engine.strategy = AlwaysOpen()
        try:
            decision = Decision(Action.HOLD, "not started")
            for minute in range(6):
                decision = engine.on_bar(bar(START + timedelta(minutes=minute), 20_000 + minute))
        finally:
            engine.close()
        assert decision.action is Action.HOLD
        assert engine.broker.position is None


# -- emergency flow --------------------------------------------------------


class EmergencyFlowTests:
    def coordinator(self, tmp_path, **kwargs) -> EmergencyCoordinator:
        return EmergencyCoordinator(tmp_path / "emergency.sqlite3", **kwargs)

    def test_ttl_is_clamped_to_documented_bounds(self, tmp_path) -> None:
        assert self.coordinator(tmp_path, ttl_seconds=1).ttl_seconds == MIN_TTL_SECONDS
        assert self.coordinator(tmp_path, ttl_seconds=99_999).ttl_seconds == MAX_TTL_SECONDS

    def test_challenge_is_single_use(self, tmp_path) -> None:
        coordinator = self.coordinator(tmp_path)
        position = {"quantity": 1, "entry_price": 100.0}
        prepared = coordinator.prepare("owner", position)
        coordinator.consume(
            prepared["challenge_id"], "owner", prepared["confirmation_phrase"], position
        )
        with pytest.raises(EmergencyFlowError, match="already used"):
            coordinator.consume(
                prepared["challenge_id"], "owner", prepared["confirmation_phrase"], position
            )

    def test_expired_challenge_is_refused(self, tmp_path) -> None:
        coordinator = self.coordinator(tmp_path, ttl_seconds=MIN_TTL_SECONDS)
        position = {"quantity": 1}
        prepared = coordinator.prepare("owner", position)
        # Rewrite the stored expiry rather than sleeping.
        with coordinator._connect() as connection:  # noqa: SLF001
            connection.execute(
                "UPDATE emergency_challenges SET expires_at = ? WHERE challenge_id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                 prepared["challenge_id"]),
            )
        with pytest.raises(EmergencyFlowError, match="expired"):
            coordinator.consume(
                prepared["challenge_id"], "owner", prepared["confirmation_phrase"], position
            )

    def test_changed_position_invalidates_the_confirmation(self, tmp_path) -> None:
        coordinator = self.coordinator(tmp_path)
        prepared = coordinator.prepare("owner", {"quantity": 1, "entry_price": 100.0})
        with pytest.raises(EmergencyFlowError, match="position changed"):
            coordinator.consume(
                prepared["challenge_id"],
                "owner",
                prepared["confirmation_phrase"],
                {"quantity": 2, "entry_price": 100.0},
            )

    def test_wrong_phrase_and_wrong_actor_are_refused(self, tmp_path) -> None:
        coordinator = self.coordinator(tmp_path)
        position = {"quantity": 1}
        prepared = coordinator.prepare("owner", position)
        with pytest.raises(EmergencyFlowError, match="phrase"):
            coordinator.consume(prepared["challenge_id"], "owner", "CONFIRM WHATEVER", position)
        with pytest.raises(EmergencyFlowError, match="actor"):
            coordinator.consume(
                prepared["challenge_id"], "someone-else", prepared["confirmation_phrase"], position
            )

    def test_challenge_survives_a_restart(self, tmp_path) -> None:
        position = {"quantity": 1}
        prepared = self.coordinator(tmp_path).prepare("owner", position)
        reopened = self.coordinator(tmp_path)
        consumed = reopened.consume(
            prepared["challenge_id"], "owner", prepared["confirmation_phrase"], position
        )
        assert consumed["challenge_id"] == prepared["challenge_id"]

    def test_engine_flatten_refuses_a_stale_confirmation(self, tmp_path) -> None:
        engine = TradingEngine(settings(tmp_path))
        engine.strategy = AlwaysOpen()
        try:
            for minute in range(6):
                engine.on_bar(bar(START + timedelta(minutes=minute), 20_000 + minute))
            assert engine.broker.position is not None
            prepared = engine.prepare_emergency_flat()
            # The position closes by other means before the operator confirms.
            engine.broker.close_long(20_010, START + timedelta(minutes=7), "manual")
            with pytest.raises(ValueError, match="invalid or expired"):
                engine.execute_emergency_flat(prepared["confirmation_token"])
        finally:
            engine.close()

    def test_engine_flatten_accepts_a_matching_confirmation_once(self, tmp_path) -> None:
        engine = TradingEngine(settings(tmp_path))
        engine.strategy = AlwaysOpen()
        try:
            for minute in range(6):
                engine.on_bar(bar(START + timedelta(minutes=minute), 20_000 + minute))
            prepared = engine.prepare_emergency_flat()
            assert engine.execute_emergency_flat(prepared["confirmation_token"]) == {
                "closed": True,
                "paused": True,
            }
            with pytest.raises(ValueError, match="invalid or expired"):
                engine.execute_emergency_flat(prepared["confirmation_token"])
        finally:
            engine.close()


# -- control security ------------------------------------------------------


class ControlAuthenticationTests:
    def token(self) -> str:
        return "t" * 40

    def test_short_token_disables_the_control_surface(self) -> None:
        authenticator = ControlAuthenticator("too-short")
        assert authenticator.configured is False
        with pytest.raises(AuthenticationError, match="disabled"):
            authenticator.authenticate(headers("too-short", 0, "n" * 20), "POST", "/x")

    def test_valid_request_is_accepted_exactly_once(self) -> None:
        authenticator = ControlAuthenticator(self.token(), clock=lambda: 1_000.0)
        request = headers(self.token(), 1_000, "nonce-abcdefghijklmno")
        result = authenticator.authenticate(request, "POST", "/api/control/pause")
        assert result.actor == "tester"
        with pytest.raises(AuthenticationError, match="replayed"):
            authenticator.authenticate(request, "POST", "/api/control/pause")

    def test_bad_token_stale_timestamp_and_short_nonce_are_refused(self) -> None:
        authenticator = ControlAuthenticator(self.token(), clock=lambda: 1_000.0)
        with pytest.raises(AuthenticationError, match="authentication failed"):
            authenticator.authenticate(headers("w" * 40, 1_000, "n" * 20), "POST", "/x")
        with pytest.raises(AuthenticationError, match="skew"):
            authenticator.authenticate(headers(self.token(), 1, "n" * 20), "POST", "/x")
        with pytest.raises(AuthenticationError, match="nonce"):
            authenticator.authenticate(headers(self.token(), 1_000, "short"), "POST", "/x")

    def test_nonce_reuse_is_refused_after_a_restart(self, tmp_path) -> None:
        path = tmp_path / "nonces.sqlite3"
        request = headers(self.token(), 1_000, "durable-nonce-abcdefg")
        ControlAuthenticator(
            self.token(), clock=lambda: 1_000.0, replay_db_path=path
        ).authenticate(request, "POST", "/x")
        restarted = ControlAuthenticator(
            self.token(), clock=lambda: 1_000.0, replay_db_path=path
        )
        with pytest.raises(AuthenticationError, match="replayed"):
            restarted.authenticate(request, "POST", "/x")

    def test_audit_payload_drops_credential_shaped_keys(self) -> None:
        authenticator = ControlAuthenticator(self.token(), clock=lambda: 1_000.0)
        auth = authenticator.authenticate(
            headers(self.token(), 1_000, "nonce-abcdefghijklmno"), "POST", "/x"
        )
        record = audit_payload(auth, "pause", {"reason": "drill", "control_token": "secret"})
        assert record["payload"] == {"reason": "drill"}
        assert "secret" not in str(record)

    def test_no_network_location_shortcut_is_shipped(self) -> None:
        import tickforge.control_security as module

        exported = dir(module)
        assert not [name for name in exported if "trust" in name.lower()]


# -- watchdog --------------------------------------------------------------


class IndependentWatchdogTests:
    def healthy(self, age: float = 5.0) -> dict:
        return {"status": "healthy", "last_bar_age_seconds": age, "unresolved_order_count": 0}

    def test_unreachable_surface_is_a_failure_not_an_absence(self) -> None:
        watchdog = HealthWatchdog(ScriptedProbe([RuntimeError("connection refused")]))
        observation = watchdog.observe(START)
        assert observation.reachable is False
        assert observation.healthy is False
        assert observation.actionable is True

    def test_staleness_budget_follows_the_session_calendar(self) -> None:
        policy = WatchdogPolicy(stale_open_seconds=120, stale_closed_seconds=900)
        payload = self.healthy(age=300)
        open_watch = HealthWatchdog(
            ScriptedProbe([payload]), calendar=AlwaysOpenCalendar(), policy=policy
        )
        closed_watch = HealthWatchdog(
            ScriptedProbe([payload]), calendar=ClosedCalendar(), policy=policy
        )
        assert open_watch.observe(START).healthy is False
        assert closed_watch.observe(START).healthy is True

    def test_unresolved_orders_are_reported_as_unhealthy(self) -> None:
        payload = {"status": "healthy", "last_bar_age_seconds": 1, "unresolved_order_count": 1}
        observation = HealthWatchdog(ScriptedProbe([payload])).observe(START)
        assert observation.healthy is False
        assert any("unresolved" in failure for failure in observation.failures)

    def test_alerting_requires_consecutive_failures_then_backs_off(self) -> None:
        notifier = RecordingNotifier()
        watchdog = HealthWatchdog(
            ScriptedProbe([{"status": "degraded"}]),
            notifier=notifier,
            policy=WatchdogPolicy(failures_before_alert=3, backoff_seconds=60),
        )
        for offset in range(2):
            watchdog.check(START + timedelta(seconds=offset))
        assert notifier.sent == []
        watchdog.check(START + timedelta(seconds=2))
        assert len(notifier.sent) == 1
        # Still inside the backoff window: observed, counted, not re-sent.
        watchdog.check(START + timedelta(seconds=3))
        assert len(notifier.sent) == 1
        watchdog.check(START + timedelta(seconds=120))
        assert len(notifier.sent) == 2

    def test_maintenance_failures_never_alert_and_never_recover(self) -> None:
        notifier = RecordingNotifier()
        watchdog = HealthWatchdog(
            ScriptedProbe([{"status": "degraded"}]),
            calendar=MaintenanceCalendar(),
            notifier=notifier,
            policy=WatchdogPolicy(failures_before_alert=1),
        )
        for offset in range(5):
            observation = watchdog.check(START + timedelta(seconds=offset))
            assert observation.actionable is False
        assert notifier.sent == []
        assert watchdog.state.alerted is False

    def test_recovery_is_announced_only_after_a_real_alert(self) -> None:
        notifier = RecordingNotifier()
        watchdog = HealthWatchdog(
            ScriptedProbe([{"status": "degraded"}, self.healthy()]),
            notifier=notifier,
            policy=WatchdogPolicy(failures_before_alert=1),
        )
        watchdog.check(START)
        assert [subject for subject, _ in notifier.sent] == ["health watchdog: degraded"]
        watchdog.check(START + timedelta(seconds=10))
        assert notifier.sent[-1][0] == "health watchdog: recovered"
        assert watchdog.state == EscalationState()

    def test_no_recovery_notice_when_nothing_was_ever_alerted(self) -> None:
        notifier = RecordingNotifier()
        watchdog = HealthWatchdog(ScriptedProbe([self.healthy()]), notifier=notifier)
        watchdog.check(START)
        assert notifier.sent == []

    def test_escalation_state_survives_a_restart(self, tmp_path) -> None:
        path = tmp_path / "watchdog-state.json"
        policy = WatchdogPolicy(failures_before_alert=2, backoff_seconds=600)
        first = RecordingNotifier()
        watchdog = HealthWatchdog(
            ScriptedProbe([{"status": "degraded"}]),
            notifier=first,
            policy=policy,
            state_path=path,
        )
        watchdog.check(START)
        watchdog.check(START + timedelta(seconds=1))
        assert len(first.sent) == 1

        second = RecordingNotifier()
        restarted = HealthWatchdog(
            ScriptedProbe([{"status": "degraded"}]),
            notifier=second,
            policy=policy,
            state_path=path,
        )
        assert restarted.state.alerted is True
        restarted.check(START + timedelta(seconds=2))
        assert second.sent == []

    def test_default_notifier_is_inert(self) -> None:
        watchdog = HealthWatchdog(ScriptedProbe([{"status": "degraded"}]))
        assert isinstance(watchdog.notifier, NullNotifier)
        assert watchdog.check(START).healthy is False

    def test_run_returns_a_nonzero_status_for_a_degraded_surface(self) -> None:
        assert HealthWatchdog(ScriptedProbe([self.healthy()])).run(once=True) == 0
        assert HealthWatchdog(ScriptedProbe([{"status": "degraded"}])).run(once=True) == 2
