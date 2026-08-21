#!/usr/bin/env python3
"""Docker healthcheck: verify the bot heartbeat is recent and valid."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

DATA_DIR = Path(os.getenv("MENXUN_DATA_DIR", "/data"))
HEALTH_FILE = DATA_DIR / "health.json"
MAX_AGE_SECONDS = int(os.getenv("MENXUN_HEALTH_MAX_AGE", "120"))


def main() -> int:
    try:
        age = time.time() - HEALTH_FILE.stat().st_mtime
        payload = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        if payload.get("status") not in {"starting", "running"}:
            return 1
        return 0 if age <= MAX_AGE_SECONDS else 1
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
