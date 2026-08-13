"""TickForge Community: a simulation-first trading research framework."""

from .config import Settings
from .control_security import AuthenticationError, ControlAuthenticator
from .emergency import EmergencyCoordinator, EmergencyFlowError
from .engine import TradingEngine
from .gates import Gate, GateChain, GateContext, GateOutcome, GateResult
from .journal import OrderJournal, OrderStateError
from .local_review import LlamaCppRuntime, LocalModelReviewer
from .models import Bar, Decision
from .reconcile import Reconciler, ReconciliationReport, ReconciliationState
from .watchdog import HealthWatchdog, Notifier, SessionCalendar

__all__ = [
    "AuthenticationError",
    "Bar",
    "ControlAuthenticator",
    "Decision",
    "EmergencyCoordinator",
    "EmergencyFlowError",
    "Gate",
    "GateChain",
    "GateContext",
    "GateOutcome",
    "GateResult",
    "HealthWatchdog",
    "LlamaCppRuntime",
    "LocalModelReviewer",
    "Notifier",
    "OrderJournal",
    "OrderStateError",
    "Reconciler",
    "ReconciliationReport",
    "ReconciliationState",
    "SessionCalendar",
    "Settings",
    "TradingEngine",
]
__version__ = "0.1.0"
