"""
KILL SWITCH — Emergency Stop for Autonomous Execution
=======================================================
Single source of truth for whether autonomous execution is allowed.
File-backed (survives restarts), checked by task queue, proposal bus,
and deploy pipeline.

When ENGAGED:
  - Task queue pauses (current task finishes, no new ones start)
  - Auto-approve disabled
  - No proposals dispatched
  - No deploys initiated
  - Manual chat still works

State file: state/kill_switch.json
"""

import json
import time
import filelock
from pathlib import Path
from typing import Dict

import sys
_BASE = Path(__file__).parent.parent
sys.path.insert(0, str(_BASE))
from shared.logger import get_logger
from shared.blackbox import log_event as _bb_log

logger = get_logger("kill_switch")

STATE_DIR = _BASE / "state"
SWITCH_FILE = STATE_DIR / "kill_switch.json"
LOCK_FILE = STATE_DIR / "kill_switch.lock"

DEFAULT_STATE = {
    "engaged": False,
    "engaged_at": None,
    "engaged_by": None,
    "reason": None,
    "disengaged_at": None,
    "history": [],
}


def _ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _load_state() -> Dict:
    if not SWITCH_FILE.exists():
        return DEFAULT_STATE.copy()
    try:
        with open(SWITCH_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception):
        return DEFAULT_STATE.copy()


def _save_state(state: Dict):
    _ensure_state_dir()
    lock = filelock.FileLock(str(LOCK_FILE), timeout=5)
    with lock:
        with open(SWITCH_FILE, "w") as f:
            json.dump(state, f, indent=2)


def is_engaged() -> bool:
    """Check if kill switch is engaged. Hot path — reads from disk each call."""
    return _load_state().get("engaged", False)


def engage(reason: str = "Manual kill switch", engaged_by: str = "human") -> Dict:
    """Engage the kill switch — stop all autonomous execution."""
    state = _load_state()

    if state.get("engaged"):
        logger.warning(f"Kill switch already engaged (by {state.get('engaged_by')})")
        return state

    now = time.time()
    state["engaged"] = True
    state["engaged_at"] = now
    state["engaged_by"] = engaged_by
    state["reason"] = reason
    state["disengaged_at"] = None

    history = state.get("history", [])
    history.append({"action": "engaged", "at": now, "by": engaged_by, "reason": reason})
    state["history"] = history[-20:]

    _save_state(state)
    logger.warning(f"KILL SWITCH ENGAGED by {engaged_by}: {reason}")
    _bb_log("kill_switch_engaged", {"reason": reason, "engaged_by": engaged_by}, source="kill_switch")
    return state


def disengage(disengaged_by: str = "human") -> Dict:
    """Disengage the kill switch — resume autonomous execution."""
    state = _load_state()

    if not state.get("engaged"):
        logger.info("Kill switch already disengaged")
        return state

    now = time.time()
    engaged_duration = now - state.get("engaged_at", now)
    state["engaged"] = False
    state["disengaged_at"] = now

    history = state.get("history", [])
    history.append({
        "action": "disengaged", "at": now, "by": disengaged_by,
        "was_engaged_for": round(engaged_duration, 1),
    })
    state["history"] = history[-20:]

    _save_state(state)
    logger.info(f"KILL SWITCH DISENGAGED by {disengaged_by} (was engaged for {engaged_duration:.0f}s)")
    _bb_log("kill_switch_disengaged", {"disengaged_by": disengaged_by, "was_engaged_for": round(engaged_duration)}, source="kill_switch")
    return state


def get_status() -> Dict:
    return _load_state()
