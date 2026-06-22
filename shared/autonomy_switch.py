"""Master autonomy switch — one graceful, durable pause for the whole autonomy pipe.

Why this exists (2026-05-31): pausing autonomy used to mean either (a) runtime
/watchtower/toggle calls that evaporate on reboot, or (b) per-job env flags set at
launch that a running process (or a future dashboard button) can't durably flip. And
three jobs (task_proposer, web_researcher, proposal_drafter) had no env gate at all,
so "paused" never really covered them. Every reboot re-enabled everything.

This is the single source of truth: a small persisted file (state/autonomy.json) that
the cron loop checks each tick. Flip it from anywhere — the launch wrapper, a future
dashboard flag (POST /watchtower/autonomy), or a one-liner — and the change is durable
across reboots. The cron loop skips jobs tagged autonomy=True while paused; infra jobs
(health_check, memory_decay) always run.

Default is UNPAUSED (autonomy ON) — DJ does not want autonomy disabled at launch. A
missing or unreadable state file means autonomy runs. ROUX_AUTONOMY_PAUSED seeds the
initial value only when no state file exists yet, so a fresh box can boot paused via
env without a file. Once the file exists (someone toggled), the file wins — that's how
the dashboard choice survives a reboot.

Layering: this is a GLOBAL gate on top of the existing per-job controls (job.enabled
toggle + each job's own ROUX_* env flag). A job runs only if: not globally paused AND
job.enabled AND its own env gate passes. The master switch never removes the finer
controls — it's the big red button.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

_BASE = Path(__file__).parent.parent
STATE_FILE = _BASE / "state" / "autonomy.json"

_LOCK = threading.Lock()
# Tiny mtime cache so cron_loop calling is_paused() every 30s doesn't re-read/parse
# needlessly. (text, mtime) — re-read only when the file changes.
_cache = {"mtime": None, "state": None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> Optional[Dict]:
    """Return the persisted state dict, or None if no (valid) file. Never throws."""
    try:
        if not STATE_FILE.exists():
            return None
        mtime = STATE_FILE.stat().st_mtime
        if _cache["mtime"] == mtime and _cache["state"] is not None:
            return _cache["state"]
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "paused" not in data:
            return None
        _cache["mtime"] = mtime
        _cache["state"] = data
        return data
    except Exception:
        return None  # corrupt/unreadable -> treat as no file (autonomy ON, default)


def is_paused() -> bool:
    """True if the autonomy pipe is globally paused. Default False (autonomy ON).
    Falls back to ROUX_AUTONOMY_PAUSED only when no state file exists. Never throws."""
    state = _read()
    if state is not None:
        return bool(state.get("paused", False))
    return os.environ.get("ROUX_AUTONOMY_PAUSED", "0").lower() in ("1", "true", "yes")


def set_paused(paused: bool, by: str = "unknown", reason: str = "") -> Dict:
    """Persist the global autonomy pause state atomically. Returns the new state."""
    state = {
        "paused": bool(paused),
        "by": by,
        "reason": reason,
        "changed_at": _now(),
    }
    with _LOCK:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
        _cache["mtime"] = None  # invalidate so next read picks it up
        _cache["state"] = None
    return state


def get_state() -> Dict:
    """Full state for display (dashboard / status). Includes where the value came from."""
    state = _read()
    if state is not None:
        return {**state, "source": "state_file", "path": str(STATE_FILE)}
    env = os.environ.get("ROUX_AUTONOMY_PAUSED", "0").lower() in ("1", "true", "yes")
    return {
        "paused": env,
        "by": "env_default" if env else "default",
        "reason": "no state file; ROUX_AUTONOMY_PAUSED env seed" if env else "no state file; default unpaused",
        "changed_at": None,
        "source": "env_or_default",
        "path": str(STATE_FILE),
    }
