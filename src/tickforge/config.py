from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def _number(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw in (None, "") else float(raw)


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 5003
    control_token: str = ""
    initial_equity: float = 100_000
    point_value: float = 1
    fee_per_side: float = 0
    slippage_points: float = 1
    max_daily_loss: float = 1_000
    max_stop_points: float = 50
    db_path: Path = Path("data/tickforge-community.sqlite3")
    simulation_only: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            host=os.getenv("TICKFORGE_HOST", "127.0.0.1"),
            port=int(os.getenv("TICKFORGE_PORT", "5003")),
            control_token=os.getenv("TICKFORGE_CONTROL_TOKEN", ""),
            initial_equity=_number("TICKFORGE_INITIAL_EQUITY", 100_000),
            point_value=_number("TICKFORGE_POINT_VALUE", 1),
            fee_per_side=_number("TICKFORGE_FEE_PER_SIDE", 0),
            max_daily_loss=_number("TICKFORGE_MAX_DAILY_LOSS", 1_000),
            max_stop_points=_number("TICKFORGE_MAX_STOP_POINTS", 50),
            db_path=Path(os.getenv("TICKFORGE_DB_PATH", "data/tickforge-community.sqlite3")),
        )

    def validate(self) -> None:
        if self.host not in {"127.0.0.1", "localhost", "::1"} and len(self.control_token) < 32:
            raise ValueError(
                "non-loopback binding requires a control token of at least 32 characters"
            )
        if self.initial_equity <= 0 or self.point_value <= 0:
            raise ValueError("equity and point value must be positive")
        if self.max_daily_loss <= 0 or self.max_stop_points <= 0:
            raise ValueError("risk limits must be positive")

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["db_path"] = str(self.db_path)
        data["control_token_configured"] = len(self.control_token) >= 32
        data.pop("control_token")
        return data
