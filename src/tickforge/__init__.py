"""TickForge Community: a simulation-first trading research framework."""

from .config import Settings
from .engine import TradingEngine
from .local_review import LlamaCppRuntime, LocalModelReviewer
from .models import Bar, Decision

__all__ = [
    "Bar",
    "Decision",
    "LlamaCppRuntime",
    "LocalModelReviewer",
    "Settings",
    "TradingEngine",
]
__version__ = "0.1.0"
