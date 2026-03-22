"""
PROPOSAL BUS — Auto-Approve + Persistent Lifecycle
====================================================
Central store for all proposals. Manages state transitions:

  pending → approved → executing → completed/failed
  pending → dismissed (with cooldown)
  pending → auto-approved → executing → completed/failed

Any part of the system can:
  - publish_proposal()         — observers, coach, research
  - approve_proposal()         — dashboard (routes to Orchestrator)
  - auto_approve_eligible()    — cron (low-risk auto-dispatch)
  - update_state()             — Orchestrator (executing/completed/failed)
  - dismiss_proposal()         — dashboard (cooldown prevents re-proposal)
  - get_active()               — dashboard (display current proposals)
  - get_history()              — dashboard/memory (audit trail)

Auto-approve:
  - Config-driven: state/auto_approve_config.json
  - Criteria: reversible, high confidence, safe category, executor not blocked
  - Unrestricted combos: executor+category pairs that bypass the priority cap
  - Coder executor always blocked (no auto code changes)
  - Daily limit prevents runaway
  - Full audit trail: approved_by = "auto" vs "human"

Backed by: state/proposals_active.json + state/proposals_history.json
Thread-safe via file locking.
"""

import json
import time
import uuid
import filelock
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import date

import sys
_BASE = Path(__file__).parent.parent
sys.path.insert(0, str(_BASE))

from shared.logger import get_logger
from shared.kill_switch import is_engaged as _kill_switch_engaged
from shared.blackbox import log_event as _bb_log

logger = get_logger("proposal_bus")

STATE_DIR = _BASE / "state"
ACTIVE_FILE = STATE_DIR / "proposals_active.json"
HISTORY_FILE = STATE_DIR / "proposals_history.json"
LOCK_FILE = STATE_DIR / "proposals.lock"
AUTO_APPROVE_CONFIG = STATE_DIR / "auto_approve_config.json"

MAX_HISTORY = 100
DISMISS_COOLDOWN_HOURS = 12

STATES = {"pending", "approved", "executing", "completed", "failed", "dismissed"}
EXECUTORS = {"watchtower", "coder", "worker", "manual"}

DEFAULT_AUTO_APPROVE_CONFIG = {
    "enabled": True,
    "allowed_categories": ["health", "memory", "resources"],
    "max_priority": 4,
    "min_confidence": 0.8,
    "require_reversible": True,
    "blocked_executors": ["coder"],
    "daily_limit": 10,
    "today_count": 0,
    "today_date": "",
    "unrestricted_combos": [
        {"executor": "watchtower", "category": "health"},
        {"executor": "worker", "category": "memory"},
    ],
}


def _ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _load_active() -> List[Dict]:
    if not ACTIVE_FILE.exists():
        return []
    try:
        with open(ACTIVE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception):
        return []


def _save_active(proposals: List[Dict]):
    _ensure_state_dir()
    with open(ACTIVE_FILE, "w") as f:
        json.dump(proposals, f, indent=2)


def _load_history() -> List[Dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception):
        return []


def _save_history(history: List[Dict]):
    _ensure_state_dir()
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def _get_lock() -> filelock.FileLock:
    _ensure_state_dir()
    return filelock.FileLock(str(LOCK_FILE), timeout=10)


def _infer_executor(category: str, title: str, proposed_action: str) -> str:
    title_lower = title.lower()
    action_lower = proposed_action.lower()
    if category == "health" and any(w in title_lower for w in ["offline", "crashed", "unhealthy"]):
        return "watchtower"
    if "restart" in action_lower:
        return "watchtower"
    if category == "codebase" and any(w in action_lower for w in ["rebuild", "refactor", "fix", "update"]):
        return "coder"
    if category in ("memory", "resources") and any(w in action_lower for w in ["decay", "clean", "prune"]):
        return "worker"
    if category == "skills":
        return "manual"
    if category == "tasks" and "review" in action_lower:
        return "manual"
    return "manual"


def _extract_service_name(title: str) -> Optional[str]:
    import re
    match = re.search(r"(?:offline|unhealthy|crashed|slow):\s*(\w+)", title, re.IGNORECASE)
    return match.group(1) if match else None


def load_auto_approve_config() -> Dict:
    _ensure_state_dir()
    if not AUTO_APPROVE_CONFIG.exists():
        save_auto_approve_config(DEFAULT_AUTO_APPROVE_CONFIG)
        return DEFAULT_AUTO_APPROVE_CONFIG.copy()
    try:
        with open(AUTO_APPROVE_CONFIG, "r") as f:
            config = json.load(f)
        for k, v in DEFAULT_AUTO_APPROVE_CONFIG.items():
            if k not in config:
                config[k] = v
        return config
    except (json.JSONDecodeError, Exception):
        return DEFAULT_AUTO_APPROVE_CONFIG.copy()


def save_auto_approve_config(config: Dict):
    _ensure_state_dir()
    with open(AUTO_APPROVE_CONFIG, "w") as f:
        json.dump(config, f, indent=2)


def _reset_daily_counter(config: Dict) -> Dict:
    today = date.today().isoformat()
    if config.get("today_date") != today:
        config["today_count"] = 0
        config["today_date"] = today
    return config


def _is_unrestricted_combo(proposal: Dict, config: Dict) -> bool:
    combos = config.get("unrestricted_combos", [])
    for combo in combos:
        if (combo.get("executor") == proposal.get("executor") and
            combo.get("category") == proposal.get("category")):
            return True
    return False


def check_auto_approve_eligible(proposal: Dict) -> tuple:
    config = load_auto_approve_config()
    config = _reset_daily_counter(config)

    if not config.get("enabled", False):
        return False, "auto-approve disabled"
    if config.get("today_count", 0) >= config.get("daily_limit", 10):
        return False, f"daily limit reached ({config['daily_limit']})"
    if proposal.get("state") != "pending":
        return False, f"not pending (state: {proposal.get('state')})"
    if config.get("require_reversible", True) and not proposal.get("reversible", False):
        return False, "not reversible"

    min_conf = config.get("min_confidence", 0.8)
    if proposal.get("confidence", 0) < min_conf:
        return False, f"confidence too low ({proposal.get('confidence')} < {min_conf})"

    blocked_exe = config.get("blocked_executors", ["coder"])
    if proposal.get("executor") in blocked_exe:
        return False, f"executor '{proposal.get('executor')}' is blocked"

    allowed_cats = config.get("allowed_categories", [])
    if proposal.get("category") not in allowed_cats:
        return False, f"category '{proposal.get('category')}' not in allowed list"

    is_unrestricted = _is_unrestricted_combo(proposal, config)
    max_priority = config.get("max_priority", 4)
    if proposal.get("priority", 99) > max_priority:
        if not is_unrestricted:
            return False, f"priority too high (P{proposal.get('priority')} > P{max_priority})"

    combo_note = " (unrestricted combo)" if is_unrestricted else ""
    return True, f"all criteria met{combo_note}"


def auto_approve_if_eligible(proposal_id: str) -> Optional[Dict]:
    with _get_lock():
        active = _load_active()
        proposal = next((p for p in active if p["id"] == proposal_id), None)
        if not proposal:
            return None

        eligible, reason = check_auto_approve_eligible(proposal)
        if not eligible:
            return None

        proposal["state"] = "approved"
        proposal["approved_at"] = time.time()
        proposal["approved_by"] = "auto"
        proposal["auto_approve_reason"] = reason
        _save_active(active)

        config = load_auto_approve_config()
        config = _reset_daily_counter(config)
        config["today_count"] = config.get("today_count", 0) + 1
        save_auto_approve_config(config)

        logger.info(
            f"AUTO-APPROVED: [{proposal['category']}] P{proposal['priority']}: "
            f"{proposal['title']} (reason: {reason}, daily: {config['today_count']}/{config.get('daily_limit', 10)})"
        )
        _bb_log("proposal_auto_approved", {
            "id": proposal["id"], "title": proposal["title"],
            "category": proposal["category"], "priority": proposal["priority"],
            "executor": proposal["executor"], "confidence": proposal.get("confidence"),
            "reason": reason,
        }, source="proposal_bus")

        return proposal


def auto_approve_eligible_batch() -> List[Dict]:
    if _kill_switch_engaged():
        logger.warning("Kill switch engaged — auto-approve blocked")
        return []

    config = load_auto_approve_config()
    if not config.get("enabled", False):
        return []

    with _get_lock():
        active = _load_active()
        pending = [p for p in active if p["state"] == "pending"]

    auto_approved = []
    for p in pending:
        result = auto_approve_if_eligible(p["id"])
        if result:
            auto_approved.append(result)

    return auto_approved


def publish_proposal(
    title: str,
    description: str,
    category: str,
    priority: int,
    proposed_action: str,
    evidence: str,
    reversible: bool = True,
    source: str = "heuristic",
    confidence: float = 1.0,
    executor: Optional[str] = None,
    coach_reasoning: Optional[str] = None,
) -> Dict:
    with _get_lock():
        active = _load_active()

        for existing in active:
            if existing["title"] == title and existing["state"] in ("pending", "approved", "executing"):
                return existing

        history = _load_history()
        for past in reversed(history):
            if past["title"] == title and past["state"] == "dismissed":
                hours_ago = (time.time() - past.get("resolved_at", 0)) / 3600
                if hours_ago < DISMISS_COOLDOWN_HOURS:
                    return None

        if executor is None:
            executor = _infer_executor(category, title, proposed_action)

        recurrence = sum(1 for p in history if p["title"] == title)
        if recurrence > 0:
            priority = min(10, priority + recurrence)

        proposal = {
            "id": f"prop_{uuid.uuid4().hex[:12]}",
            "title": title,
            "description": description,
            "category": category,
            "priority": priority,
            "proposed_action": proposed_action,
            "evidence": evidence,
            "reversible": reversible,
            "source": source,
            "confidence": confidence,
            "executor": executor,
            "state": "pending",
            "created_at": time.time(),
            "approved_at": None,
            "approved_by": None,
            "executing_at": None,
            "resolved_at": None,
            "result": None,
            "executor_meta": {},
            "coach_reasoning": coach_reasoning,
        }

        if category == "health":
            svc = _extract_service_name(title)
            if svc:
                proposal["executor_meta"]["service_name"] = svc

        active.append(proposal)
        _save_active(active)

        logger.info(f"PROPOSAL BUS: Published [{category}] P{priority}: {title} (executor: {executor})")
        return proposal


def approve_proposal(proposal_id: str) -> Optional[Dict]:
    with _get_lock():
        active = _load_active()
        for p in active:
            if p["id"] == proposal_id and p["state"] == "pending":
                p["state"] = "approved"
                p["approved_at"] = time.time()
                p["approved_by"] = "human"
                _save_active(active)
                return p
        return None


def update_state(proposal_id: str, new_state: str, result: Any = None) -> Optional[Dict]:
    if new_state not in STATES:
        logger.error(f"Invalid state: {new_state}")
        return None

    with _get_lock():
        active = _load_active()
        for i, p in enumerate(active):
            if p["id"] == proposal_id:
                p["state"] = new_state
                if new_state == "executing":
                    p["executing_at"] = time.time()
                elif new_state in ("completed", "failed"):
                    p["resolved_at"] = time.time()
                    p["result"] = result
                    history = _load_history()
                    history.append(p)
                    _save_history(history)
                    active.pop(i)
                _save_active(active)
                return p
        return None


def dismiss_proposal(proposal_id: str) -> Optional[Dict]:
    with _get_lock():
        active = _load_active()
        for i, p in enumerate(active):
            if p["id"] == proposal_id and p["state"] == "pending":
                p["state"] = "dismissed"
                p["resolved_at"] = time.time()
                history = _load_history()
                history.append(p)
                _save_history(history)
                active.pop(i)
                _save_active(active)
                return p
        return None


def get_active() -> List[Dict]:
    with _get_lock():
        return _load_active()


def get_history(limit: int = 50) -> List[Dict]:
    with _get_lock():
        history = _load_history()
        return list(reversed(history[-limit:]))


def get_proposal(proposal_id: str) -> Optional[Dict]:
    with _get_lock():
        for p in _load_active():
            if p["id"] == proposal_id:
                return p
        for p in _load_history():
            if p["id"] == proposal_id:
                return p
    return None


def get_recurrence_count(title: str) -> int:
    with _get_lock():
        return sum(1 for p in _load_history() if p["title"] == title)


def get_proposal_stats() -> Dict:
    with _get_lock():
        history = _load_history()

    if not history:
        return {"total": 0, "by_state": {}, "by_category": {}, "by_executor": {},
                "recurrences": [], "failure_rate": 0.0, "auto_approved_count": 0}

    by_state: Dict[str, int] = {}
    by_category: Dict[str, int] = {}
    by_executor: Dict[str, int] = {}
    title_counts: Dict[str, Dict] = {}
    completed = failed = auto_approved = 0

    for p in history:
        state = p.get("state", "unknown")
        cat = p.get("category", "unknown")
        exe = p.get("executor", "unknown")
        title = p.get("title", "")

        by_state[state] = by_state.get(state, 0) + 1
        by_category[cat] = by_category.get(cat, 0) + 1
        by_executor[exe] = by_executor.get(exe, 0) + 1

        if state == "completed": completed += 1
        elif state == "failed": failed += 1
        if p.get("approved_by") == "auto": auto_approved += 1

        if title:
            if title not in title_counts:
                title_counts[title] = {"count": 0, "last_state": state,
                                       "last_time": p.get("resolved_at", 0),
                                       "category": cat, "executor": exe}
            title_counts[title]["count"] += 1

    recurrences = [{"title": t, **info}
                   for t, info in sorted(title_counts.items(), key=lambda x: x[1]["count"], reverse=True)
                   if info["count"] > 1]

    total_resolved = completed + failed
    return {
        "total": len(history),
        "by_state": by_state,
        "by_category": by_category,
        "by_executor": by_executor,
        "recurrences": recurrences,
        "failure_rate": round(failed / max(total_resolved, 1), 3),
        "success_rate": round(completed / max(total_resolved, 1), 3),
        "auto_approved_count": auto_approved,
    }


def clear_resolved_issues(active_titles: List[str]):
    with _get_lock():
        active = _load_active()
        still_active = []
        history = _load_history()
        for p in active:
            if p["state"] == "pending" and p["title"] not in active_titles:
                p["state"] = "completed"
                p["resolved_at"] = time.time()
                p["result"] = {"auto_resolved": True, "message": "Issue no longer detected by observers"}
                history.append(p)
            else:
                still_active.append(p)
        _save_active(still_active)
        _save_history(history)


def sync_from_proposer(all_active_proposals: List[Dict], observer_stats: Dict) -> Dict:
    active_titles = [p["title"] for p in all_active_proposals]
    published = 0
    for p in all_active_proposals:
        result = publish_proposal(
            title=p["title"],
            description=p.get("description", ""),
            category=p.get("category", ""),
            priority=p.get("priority", 5),
            proposed_action=p.get("proposed_action", ""),
            evidence=p.get("evidence", ""),
            reversible=p.get("reversible", True),
            source=p.get("source", "heuristic"),
            confidence=p.get("confidence", 1.0),
            coach_reasoning=p.get("coach_reasoning"),
        )
        if result and result.get("state") == "pending":
            published += 1
    clear_resolved_issues(active_titles)
    bus_active = get_active()
    return {"proposals": bus_active, "observer_stats": observer_stats,
            "published": published, "total_active": len(bus_active), "timestamp": time.time()}
