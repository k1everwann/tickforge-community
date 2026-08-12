# Safety architecture

This document describes the order in which TickForge Community makes a decision,
and what each component is allowed to do. It is the part of the project worth
reading before the strategy code, because the strategy is an example and this is
not.

## The ordering rule

```
bookkeeping + protective exit
        |
        v
reconciliation  ------------------> external position view
        |
        v
pre-model gates   (no candidate exists yet, so no model has been called)
        |  reject -> HOLD, and the run ends here
        v
strategy  ->  candidate
        |
        v
reviewer          (may narrow the candidate to HOLD; may do nothing else)
        |
        v
post-model gates  (risk limits, pause state)
        |  reject -> HOLD
        v
durable order journal -> submission
```

Two properties hold by construction rather than by convention:

1. **Deterministic rules run before the model.** If a pre-model gate rejects,
   `reviewer.review()` is never called. There is a test asserting the reviewer's
   call count is zero on that path, because "the model is consulted first and the
   rules clean up afterwards" is the failure mode this ordering exists to
   prevent - it lets a non-deterministic component decide what the rules even get
   asked about.
2. **Nothing downstream can re-admit what an earlier stage rejected.**
   `GateChain` stops at the first rejection, and `GateOutcome.narrow()` can only
   turn `allowed` from `True` to `False`. `tests/test_fail_closed.py` asserts
   this over exhaustive and randomised gate orderings.

The reviewer sits in the middle on purpose. It is the only component that is not
deterministic, so it gets the least authority: it runs only on candidates the
rules already permitted, and its sole power is veto. `engine.narrow_only()`
enforces that structurally - a reviewer that returns a different action gets a
HOLD instead.

## Components

| Module | Responsibility |
| --- | --- |
| `gates.py` | `Gate` protocol, `GateResult`, `GateChain`, and the pre/post split |
| `reconcile.py` | `IN_SYNC / MISMATCH / ORPHAN_POSITION / ORPHAN_CLOSED / ERROR` state machine over an injectable external position source |
| `journal.py` | Durable order lifecycle, terminal-state immutability, transition audit table |
| `emergency.py` | Two-step, single-use, position-fingerprinted emergency authorisation |
| `control_security.py` | Bearer token + timestamp + single-use nonce, with optional durable replay store |
| `watchdog.py` | Out-of-process health watchdog with session-aware staleness and escalation |
| `review.py`, `local_review.py` | The veto-only reviewer extension point and an offline local-model implementation |

### Order lifecycle

```
INTENT_CREATED -> SUBMITTING -> SUBMITTED
                                 |
    +----------------+-----------+-----------+----------------+
    |                |           |           |                |
  FILLED         REJECTED    CANCELLED     UNKNOWN      MANUAL_REVIEW
 (terminal)     (terminal)  (terminal)   (unresolved)    (unresolved)
```

`UNKNOWN` and `MANUAL_REVIEW` are deliberately **not** terminal. "We do not know
whether that order reached the venue" has to halt the system, not be rounded down
to a failure and forgotten - rounding it down is how you get two positions where
you meant to have one. Clearing them requires a human. While any intent is
unresolved, a second intent is refused, and the pre-model gate stops the engine
before it evaluates anything.

Fields named `external_*` are opaque strings from whatever executes orders. This
project never interprets them and ships no real values in them.

### Reconciliation

Anything other than `IN_SYNC` blocks new exposure. In strict mode - the posture
intended for anything holding real exposure - an orphan position (one the venue
reports that this process did not create) is **never** auto-liquidated. It blocks
new exposure and waits for a human, because automatically closing a position you
do not understand turns a bookkeeping bug into a realised loss.

Note that the pre-model gate checks `state`, not `can_open`. "One position is
already held" is a reconciled, healthy state; whether a *new* position is allowed
is a risk question, answered after a candidate exists. That separation is what
lets an open position still be closed while entries are blocked.

### Emergency flatten

`prepare` returns a confirmation phrase and stores a challenge on disk. `consume`
requires the same challenge id, the same actor, the correct phrase, an unexpired
TTL, and an unchanged **position fingerprint** (SHA-256 of the position
snapshot). If the position changed between the two steps, the confirmation is
refused and the operator has to look again - confirming a flatten against a
position that no longer exists is how a flatten becomes a new position.

The engine's snapshot deliberately covers only quantity, entry price and entry
time, not marks that move with every tick, so an ordinary price move does not
invalidate a confirmation the operator is still typing.

### Control API authentication

`api.py` accepts a shared token by default. Send an `X-TickForge-Nonce` header
and the request is instead validated by `ControlAuthenticator`, which requires
`Authorization: Bearer`, a timestamp within a bounded skew, and a nonce that has
not been used before. Point `replay_db_path` at a file and replay protection
survives a restart - which is exactly when an in-memory implementation quietly
forgets everything it was protecting against.

This module deliberately ships **no** helper that trusts a request because of the
network address it came from. Network topology is not authentication.

### Watchdog

`watchdog.py` is designed to run as its own OS process. A wedged trading process
cannot be trusted to report that it is wedged, so the watchdog polls a health
surface it does not own and treats "cannot reach it" as a failure rather than as
an absence of evidence.

Both integration points are protocols with inert defaults, the same pattern as
`DecisionReviewer`:

- `SessionCalendar` - when is the watched system expected to be doing anything?
  `AlwaysOpenCalendar` is a trivial example. Replace it.
- `Notifier` - how alerts get delivered. `NullNotifier` sends nothing.
  **Implement this yourself**; this repository ships no outbound integration and
  no credentials.

Failures inside a declared maintenance window are recorded but never alerted on,
and never produce a "recovered" notice either. Alerting waits for consecutive
actionable failures and then backs off exponentially. Escalation state can be
persisted so a restarted watchdog neither re-alerts nor forgets.

## About the numbers

**This repository contains no trading parameters.**

Every threshold here is either a neutral placeholder or an explicitly fictional
example:

- The gates in `gates.py` whose behaviour depends on a number are named
  `Example*`, are **not enabled by default**, and their values (a 01:00-23:00
  session, an 11-minute cooldown, a 7-point spread cap) are invented to look
  invented.
- `WatchdogPolicy` defaults are demo-reasonable placeholders. Staleness budgets
  are a property of the system being watched - its bar interval, its decision
  cadence, its venue's maintenance schedule - so they are configuration, and this
  project has no opinion about yours.
- `Settings` risk limits are round example numbers for a simulated account.

The skeleton is the deliverable. Parameters are yours to choose, justify, and
defend.

## Redline scanner

`scripts/redline_check.py` greps a diff or the tree for strings that must not
appear in this repository: operator and host identifiers, execution-venue
identifiers, credential-shaped names, and third-party service endpoints.

```sh
python scripts/redline_check.py            # scan the tracked tree
python scripts/redline_check.py --diff     # scan the diff against origin/main
git diff | python scripts/redline_check.py -
```

It exits non-zero on any hit and is intentionally biased towards false positives:
a reviewer glancing at a short list is cheap, and a leak is not. It is not wired
into CI yet - the tree currently has known hits that need a decision first (see
the port default in `config.py`), and a check that is red on arrival gets ignored.
