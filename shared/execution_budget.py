"""
EXECUTION BUDGET — Hourly Execution Cap
=========================================
Tracks autonomous task executions per rolling hour window.
When the budget is exhausted, the task queue pauses until
the window rolls forward.

State file: state/execution_budget.json
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
from shared.blackbox import log_event as _bb_log

logger = get_logger("execution_budget")

STATE_DIR = _BASE / "state"
BUDGET_FILE = STATE_DIR / "execution_budget.json"
LOCK_FILE = STATE_DIR / "execution_budget.lock"

DEFAULT_CONFIG = {
    "enabled": True,
    "max_per_hour": 20,
    "auto_kill_switch": False,
    "executions": [],
}

WINDOW_SECONDS = 3600


def _ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _load() -> Dict:
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
    _ensure_state_dir()
    lock = filelock.FileLock(str(LOCK_FILE), timeout=5)
    with lock:
        with open(BUDGET_FILE, "w") as f:
            json.dump(state, f, indent=2)


def _prune_old(executions: List[float]) -> List[float]:
    cutoff = time.time() - WINDOW_SECONDS
    return [t for t in executions if t > cutoff]


def check_budget() -> tuple:
    """
    Check if execution budget allows another task.
    Returns: (allowed: bool, info: dict)
    """
    state = _load()

    if not state.get("enabled", True):
        return True, {"used": 0, "max": 0, "remaining": 0, "enabled": False}

    max_per_hour = state.get("max_per_hour", 20)
    executions = _prune_old(state.get("executions", []))
    used = len(executions)
    remaining = max(0, max_per_hour - used)

    if executions and used >= max_per_hour:
        oldest = min(executions)
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

    return (used < max_per_hour), info


def record_execution():
    """Record that a task was just executed. Call after task completion."""
    state = _load()
    executions = _prune_old(state.get("executions", []))
    executions.append(time.time())
    state["executions"] = executions
    _save(state)

    used = len(executions)
    max_per_hour = state.get("max_per_hour", 20)
    logger.info(f"BUDGET: {used}/{max_per_hour} executions this hour")
    _bb_log("budget_recorded", {"used": used, "max": max_per_hour}, source="execution_budget")

    if state.get("auto_kill_switch", False) and used >= max_per_hour:
        try:
            from shared.kill_switch import engage
            engage(
                reason=f"Execution budget exceeded ({used}/{max_per_hour} per hour)",
                engaged_by="budget_exceeded"
            )
            logger.warning(f"BUDGET: Auto-engaged kill switch ({used}/{max_per_hour})")
        except Exception as e:
            logger.error(f"Failed to auto-engage kill switch: {e}")

    return {"used": used, "max": max_per_hour, "remaining": max(0, max_per_hour - used)}


def get_status() -> Dict:
    state = _load()
    executions = _prune_old(state.get("executions", []))
    max_per_hour = state.get("max_per_hour", 20)
    used = len(executions)

    if executions and used >= max_per_hour:
        oldest = min(executions)
        resets_in = max(0, (oldest + WINDOW_SECONDS) - time.time())
    else:
        resets_in = 0

    return {
        "enabled": state.get("enabled", True),
        "max_per_hour": max_per_hour,
        "used_this_hour": used,
        "remaining": max(0, max_per_hour - used),
        "window_resets_in": round(resets_in),
        "auto_kill_switch": state.get("auto_kill_switch", False),
    }


def update_config(max_per_hour: int = None, enabled: bool = None,
                  auto_kill_switch: bool = None) -> Dict:
    state = _load()
    if max_per_hour is not None:
        state["max_per_hour"] = max(1, max_per_hour)
    if enabled is not None:
        state["enabled"] = enabled
    if auto_kill_switch is not None:
        state["auto_kill_switch"] = auto_kill_switch
    _save(state)
    return get_status()


def reset_counter() -> Dict:
    state = _load()
    state["executions"] = []
    _save(state)
    logger.info("BUDGET: Counter reset")
    return get_status()
