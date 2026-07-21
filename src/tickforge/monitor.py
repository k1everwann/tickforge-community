from __future__ import annotations

import json
import time
import urllib.request
from typing import Any


def fetch_health(url: str, timeout: float = 5) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return json.load(response)


def monitor(url: str, interval: float = 10, once: bool = False) -> int:
    """Run independently from the API process and return nonzero on degraded health."""
    while True:
        try:
            health = fetch_health(url)
            healthy = health.get("status") == "healthy"
            print(json.dumps(health, ensure_ascii=False))
        except Exception as exc:
            healthy = False
            print(json.dumps({"status": "unreachable", "error": str(exc)}, ensure_ascii=False))
        if once or not healthy:
            return 0 if healthy else 2
        time.sleep(max(1, interval))
