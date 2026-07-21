"""TickForge Community: a simulation-first trading research framework."""

from .config import Settings
from .engine import TradingEngine
from .models import Bar, Decision

__all__ = ["Bar", "Decision", "Settings", "TradingEngine"]
__version__ = "0.1.0"
