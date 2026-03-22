"""
BLACK BOX LOGGER — Immutable Audit Trail
==========================================
Append-only log that captures every significant autonomous action.
The agent system can WRITE to this log but cannot READ, MODIFY,
or DELETE it. This is the operator's forensic record.

Format: JSONL (one JSON object per line), one file per day.
Location: logs/blackbox/
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

_BASE = Path(__file__).parent.parent
BLACKBOX_DIR = _BASE / "logs" / "blackbox"


def _ensure_dir():
    BLACKBOX_DIR.mkdir(parents=True, exist_ok=True)


def _get_today_file() -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    return BLACKBOX_DIR / f"blackbox_{today}.jsonl"


def log_event(
    event_type: str,
    data: Dict[str, Any] = None,
    source: str = "system",
):
    """
    Append an event to the black box log. Write-only.
    Never raises — logs silently on failure.

    Args:
        event_type: Category (e.g. "task_start", "kill_switch_engaged")
        data: Event-specific payload
        source: Which component logged this
    """
    _ensure_dir()

    now = time.time()
    entry = {
        "ts": now,
        "time": datetime.fromtimestamp(now).isoformat(),
        "event": event_type,
        "source": source,
        "data": data or {},
    }

    try:
        with open(_get_today_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass
