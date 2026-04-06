"""
VERIFICATION BUDGET — Phase 39: Hourly cap on bigbrain verification calls
==========================================================================
Tracks verifier (Claude Sonnet via Anthropic API) calls per rolling hour.
When the budget is exhausted, the verifier silently skips with reason
"verification_budget_exhausted" — the underlying task still completes
normally (verification is advisory, never blocks).

Cloned from shared/execution_budget.py — same shape, separate state file
so verification cost is tracked independently of task execution count.

State file: state/verification_budget.json
Thread-safe via file locking.
"""

import json
import time
import filelock
from pathlib import Path
from typing import Dict, List

import sys
_BASE = Path(__file__).parent.parent
sys.path.insert(0, str(_BASE))
from shared.logger import get_logger

logger = get_logger("verification_budget")

# === PATHS ===
STATE_DIR = _BASE / "state"
BUDGET_FILE = STATE_DIR / "verification_budget.json"
LOCK_FILE = STATE_DIR / "verification_budget.lock"

# === DEFAULTS ===
DEFAULT_CONFIG = {
    "enabled": True,
    "max_per_hour": 30,    # Default cap; configurable via config.yaml verifier.max_per_hour
    "verifications": [],   # List of timestamps (epoch) of recent verification calls
}

WINDOW_SECONDS = 3600  # 1 hour rolling


def _ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _load() -> Dict:
    """Load budget state from disk."""
    if not BUDGET_FILE.exists():
        return DEFAULT_CONFIG.copy()
    try:
        with open(BUDGET_FILE, "r") as f:
            data = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            if k not in data:
                data[k] = v
        return data
    except (json.JSONDecodeError, Exception):
        return DEFAULT_CONFIG.copy()


def _save(state: Dict):
    """Save budget state to disk under lock."""
    _ensure_state_dir()
    lock = filelock.FileLock(str(LOCK_FILE), timeout=5)
    with lock:
        with open(BUDGET_FILE, "w") as f:
            json.dump(state, f, indent=2)


def _prune_old(verifications: List[float]) -> List[float]:
    """Remove verification timestamps older than the rolling window."""
    cutoff = time.time() - WINDOW_SECONDS
    return [t for t in verifications if t > cutoff]


def check_budget() -> tuple:
    """
    Check if the verification budget allows another bigbrain call.

    Returns:
        (allowed: bool, info: dict)
        info contains: used, max, remaining, window_resets_in, enabled
    """
    state = _load()

    if not state.get("enabled", True):
        # Disabled = unlimited (still tracked in info for visibility)
        return True, {"used": 0, "max": 0, "remaining": 0, "enabled": False}

    max_per_hour = state.get("max_per_hour", 30)
    verifications = _prune_old(state.get("verifications", []))
    used = len(verifications)
    remaining = max(0, max_per_hour - used)

    if verifications and used >= max_per_hour:
        oldest = min(verifications)
        resets_in = max(0, (oldest + WINDOW_SECONDS) - time.time())
    else:
        resets_in = 0

    info = {
        "used": used,
        "max": max_per_hour,
        "remaining": remaining,
        "window_resets_in": round(resets_in),
        "enabled": True,
    }

    if used >= max_per_hour:
        return False, info
    return True, info


def record_verification():
    """
    Record that a verification call just happened.
    Call this after every successful bigbrain verifier invocation
    (skipped/uncertain calls due to errors do NOT count against budget).

    Returns the updated budget info.
    """
    state = _load()
    verifications = _prune_old(state.get("verifications", []))
    verifications.append(time.time())
    state["verifications"] = verifications
    _save(state)

    used = len(verifications)
    max_per_hour = state.get("max_per_hour", 30)

    logger.info(f"VERIFY BUDGET: {used}/{max_per_hour} verifications this hour")

    return {
        "used": used,
        "max": max_per_hour,
        "remaining": max(0, max_per_hour - used),
    }


def get_status() -> Dict:
    """Get full budget status for dashboard/API."""
    state = _load()
    verifications = _prune_old(state.get("verifications", []))
    max_per_hour = state.get("max_per_hour", 30)
    used = len(verifications)

    if verifications and used >= max_per_hour:
        oldest = min(verifications)
        resets_in = max(0, (oldest + WINDOW_SECONDS) - time.time())
    else:
        resets_in = 0

    return {
        "enabled": state.get("enabled", True),
        "max_per_hour": max_per_hour,
        "used_this_hour": used,
        "remaining": max(0, max_per_hour - used),
        "window_resets_in": round(resets_in),
        "recent_verifications": verifications[-10:],
    }


def update_config(max_per_hour: int = None, enabled: bool = None) -> Dict:
    """Update budget configuration. Only provided fields are changed."""
    state = _load()

    if max_per_hour is not None:
        state["max_per_hour"] = max(1, max_per_hour)
    if enabled is not None:
        state["enabled"] = enabled

    _save(state)
    logger.info(
        f"VERIFY BUDGET CONFIG: max={state['max_per_hour']}/hr, enabled={state['enabled']}"
    )
    return get_status()


def reset_counter() -> Dict:
    """Clear verification history (for testing)."""
    state = _load()
    state["verifications"] = []
    _save(state)
    logger.info("VERIFY BUDGET: Counter reset")
    return get_status()
