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

GOVERNANCE_DIR = _BASE / "governance"
META_POLICY_FILE = GOVERNANCE_DIR / "meta_policy.json"
DISCLOSURE_RULE_FILE = GOVERNANCE_DIR / "disclosure_rule.json"

MAX_HISTORY = 100
DISMISS_COOLDOWN_HOURS = 12

STATES = {"pending", "approved", "executing", "completed", "failed", "dismissed"}
EXECUTORS = {"watchtower", "coder", "worker", "task_action", "manual"}

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
    if category == "tasks" and any(w in action_lower for w in ["cancel", "clean", "stale"]):
        return "task_action"
    if category == "tasks" and "review" in action_lower:
        return "task_action"
    # Investigation/diagnosis proposals — route to coder pipeline for log analysis
    if any(w in action_lower for w in ["analyze", "investigate", "diagnose", "check log", "read log", "inspect"]):
        return "coder"
    # Configuration fix proposals — route to worker for execution
    if any(w in action_lower for w in ["configure", "install", "set up", "enable", "pip install"]):
        return "worker"
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


def _load_meta_policy() -> Optional[Dict]:
    if not META_POLICY_FILE.exists():
        return None
    try:
        with open(META_POLICY_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"meta-policy unreadable at {META_POLICY_FILE}: {e}")
        return None


def _load_disclosure_rule() -> Optional[Dict]:
    if not DISCLOSURE_RULE_FILE.exists():
        return None
    try:
        with open(DISCLOSURE_RULE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"disclosure rule unreadable at {DISCLOSURE_RULE_FILE}: {e}")
        return None


def _disclosure_applies(source: str) -> bool:
    rule = _load_disclosure_rule()
    if rule is None or rule.get("status") != "active":
        return False
    prefixes = rule.get("applies_to_source_prefixes", [])
    return any(source.startswith(prefix) for prefix in prefixes)


def is_governance_relevant(proposal: Dict) -> tuple:
    """Detect whether a proposal touches Roux's own runtime (per disclosure_rule v1.3).

    Returns (matched: bool, reason: str). reason names the first signal that fired
    so the audit log records WHY a proposal got routed to bigbrain.
    """
    rule = _load_disclosure_rule()
    if rule is None or rule.get("status") != "active":
        return False, ""
    det = rule.get("governance_relevance_detection") or {}
    if not det:
        return False, ""

    source = proposal.get("source") or ""
    title = proposal.get("title") or ""
    action = proposal.get("proposed_action") or ""
    haystack = f"{title}\n{action}".lower()

    for prefix in det.get("author_prefixes", []):
        if source.startswith(prefix):
            return True, f"author_prefix:{prefix}"

    for glob in det.get("path_globs", []):
        if glob.lower() in haystack:
            return True, f"path_glob:{glob}"

    for kw in det.get("keywords", []):
        if kw.lower() in haystack:
            return True, f"keyword:{kw}"

    return False, ""


def _validate_disclosure(disclosure: Optional[Dict]) -> tuple:
    """Validate disclosure fields. Returns (ok: bool, reason: str)."""
    if not isinstance(disclosure, dict):
        return False, "disclosure must be a dict with source_url, authored_at, reasoning, observer_verification"

    required = ["source_url", "authored_at", "reasoning", "observer_verification"]
    missing = [k for k in required if k not in disclosure]
    if missing:
        return False, f"disclosure missing required fields: {missing}"

    src_url = disclosure.get("source_url")
    if not isinstance(src_url, str) or not src_url.strip():
        return False, "source_url must be a non-empty string"
    if src_url.startswith("/") or src_url.startswith("~"):
        expanded = Path(src_url).expanduser()
        if not expanded.exists():
            return False, f"source_url path does not exist: {src_url}"

    authored_at = disclosure.get("authored_at")
    if not isinstance(authored_at, (int, float)):
        return False, "authored_at must be a unix timestamp (float)"
    if authored_at < 1735689600:  # 2025-01-01
        return False, "authored_at is pre-2025; suspicious timestamp"
    if authored_at > time.time() + 60:  # 60s slack
        return False, "authored_at is in the future"

    reasoning = disclosure.get("reasoning")
    if not isinstance(reasoning, str) or len(reasoning) < 100:
        return False, f"reasoning must be a string of >=100 chars (got {len(reasoning) if isinstance(reasoning, str) else 'non-string'})"

    obs = disclosure.get("observer_verification")
    if not isinstance(obs, dict):
        return False, "observer_verification must be a dict"
    obs_required = ["status", "comment", "verified_at", "verified_by"]
    obs_missing = [k for k in obs_required if k not in obs]
    if obs_missing:
        return False, f"observer_verification missing subfields: {obs_missing}"
    if obs.get("status") not in ("pending", "pass", "fail"):
        return False, f"observer_verification.status must be one of pending/pass/fail (got {obs.get('status')!r})"

    return True, "disclosure valid"


def _change_tier_graduated(proposal: Dict) -> tuple:
    """(graduated: bool, reason: str). True only if the proposal's change-TIER
    (proposal_executor.classify_kind → code/config/meta/notes_edit) has GRADUATED on the
    trust ledger (readiness_report: >= MIN_CLEAN clean events, ZERO false-pass, low
    false-block). 'manual' never graduates (it's the human-only tier). Fail-CLOSED: any
    error or missing data → (False, ...). Lazy imports avoid a proposal_bus<->executor
    circular import at module load."""
    try:
        from shared.proposal_executor import classify_kind
        from shared.trust_ledger import readiness_report
        tier, _why = classify_kind(proposal)
        if tier == "manual":
            return False, "tier 'manual' is human-only (never graduates)"
        rep = readiness_report(tier) or {}
        t = rep.get(tier, {})
        if t.get("graduation_ready") is True:
            return True, f"tier '{tier}' ({t.get('clean_events')} clean, {t.get('false_pass')} false-pass)"
        blockers = t.get("blockers") or ["no trust data yet"]
        return False, f"tier '{tier}' not graduated: {blockers[0]}"
    except Exception as e:
        return False, f"graduation check error — fail-closed: {e}"


def check_auto_approve_eligible(proposal: Dict) -> tuple:
    # Meta-policy enforcement — runs FIRST, no other criteria can override
    meta = _load_meta_policy()
    if meta is not None:
        protected = set(meta.get("protected_categories", []))
        if proposal.get("category") in protected:
            return False, f"meta-policy: category '{proposal.get('category')}' is protected (human review required)"

    # Roux-primary cord-off (2026-06-18): a category under the sampling-confirmer is governed
    # by the cord-off path (Roux PASS → sample/hold → DJ-or-apply), NOT by this blanket
    # auto-approve. DEFER so there is exactly ONE controlled path for those categories, never
    # two. Dormant until a category is added to ROUX_PRIMARY_CATEGORIES (state file, empty by
    # default). This is the safety hinge: it stops auto-approve from independently shipping a
    # category whose execution handler the cord-off just enabled.
    try:
        from shared.sampling_confirmer import is_primary
        if is_primary(proposal.get("category", "")):
            return False, ("category '%s' is Roux-primary — governed by the sampling-confirmer "
                           "cord-off, not blanket auto-approve") % proposal.get("category")
    except Exception:
        pass

    # Disclosure rule enforcement — authored proposals require observer-verified pass
    if _disclosure_applies(proposal.get("source", "")):
        obs = proposal.get("disclosure", {}).get("observer_verification", {})
        if obs.get("status") != "pass":
            return False, f"disclosure rule: observer_verification.status='{obs.get('status')}' (must be 'pass' before approval)"

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

    # TRUST-GRADUATION (#3, 2026-05-31): a proposal whose change-TIER has GRADUATED on the
    # trust ledger (>= MIN_CLEAN clean events, ZERO false-pass, low false-block) may
    # auto-approve even if its category/executor isn't in the static pre-trusted allowlist
    # — the system earns scope by evidence, DJ-as-legislator sets the bar. OPT-IN: gated by
    # config["graduation_enabled"] (default False), so this is wired-but-dormant until DJ
    # deliberately flips it. Fail-closed (any error → not graduated). The universal safety
    # checks (reversible, confidence, observer-pass, disclosure, daily-limit, priority) below
    # STILL apply, and tier-0 writes are refused at the worker/executor regardless.
    graduated, grad_reason = (False, "")
    if config.get("graduation_enabled", False):
        graduated, grad_reason = _change_tier_graduated(proposal)

    blocked_exe = config.get("blocked_executors", ["coder"])
    if proposal.get("executor") in blocked_exe and not graduated:
        return False, f"executor '{proposal.get('executor')}' is blocked"

    allowed_cats = config.get("allowed_categories", [])
    if proposal.get("category") not in allowed_cats and not graduated:
        return False, f"category '{proposal.get('category')}' not in allowed list"

    is_unrestricted = _is_unrestricted_combo(proposal, config)
    max_priority = config.get("max_priority", 4)
    if proposal.get("priority", 99) > max_priority:
        if not is_unrestricted:
            return False, f"priority too high (P{proposal.get('priority')} > P{max_priority})"

    combo_note = " (unrestricted combo)" if is_unrestricted else ""
    grad_note = f" [via graduated {grad_reason}]" if graduated else ""
    return True, f"all criteria met{combo_note}{grad_note}"


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
    disclosure: Optional[Dict] = None,
    notes_edit: Optional[Dict] = None,
) -> Dict:
    # Disclosure rule enforcement — authored proposals must include valid disclosure
    if _disclosure_applies(source):
        ok, reason = _validate_disclosure(disclosure)
        if not ok:
            logger.warning(f"PROPOSAL REJECTED at publish: source={source!r} title={title!r} — {reason}")
            return None

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
            "disclosure": disclosure if disclosure else None,
            "notes_edit": notes_edit if notes_edit else None,
        }

        if category == "health":
            svc = _extract_service_name(title)
            if svc:
                proposal["executor_meta"]["service_name"] = svc

        if category == "tasks":
            if "stale" in title.lower():
                proposal["executor_meta"]["action"] = "cancel_stale"
            elif "fail" in title.lower():
                proposal["executor_meta"]["action"] = "cancel_failed"

        active.append(proposal)
        _save_active(active)

        logger.info(f"PROPOSAL BUS: Published [{category}] P{priority}: {title} (executor: {executor})")
        return proposal


def approve_proposal(proposal_id: str, approved_by: str = "human") -> Optional[Dict]:
    """Approve a pending proposal.
    Per 2026-05-17 amendment, approved_by accepts 'human' | 'claude'.
    Governance-category proposals can only be approved by 'human' (meta-policy line)."""
    if approved_by not in ("human", "claude"):
        logger.error(f"invalid approved_by: {approved_by!r}; must be 'human' or 'claude'")
        return None
    with _get_lock():
        active = _load_active()
        for p in active:
            if p["id"] == proposal_id and p["state"] == "pending":
                # Governance line: claude cannot approve governance-category proposals
                if p.get("category") == "governance" and approved_by != "human":
                    logger.warning(
                        f"APPROVAL BLOCKED: {proposal_id} — governance category requires human approval "
                        f"(attempted by {approved_by!r})"
                    )
                    return None
                # Disclosure rule check: authored proposals need an observer 'pass' before approval.
                # Since the gate went local (bigbrain == v5.3), the independent confirmer is the HUMAN
                # (2026-06-18 adaptation): a human may confirm+approve a proposal Roux shadow-PASSed
                # (held, verified_by='roux-shadow') — his approval IS the confirmation, stamped
                # observer.status=pass by 'human' here. Roux FAILs still block; non-human (claude)
                # approvers still require an existing 'pass' (Roux cannot self-confirm her own PASS).
                if _disclosure_applies(p.get("source", "")):
                    obs = (p.get("disclosure") or {}).get("observer_verification", {}) or {}
                    status = obs.get("status")
                    vby = obs.get("verified_by", "")
                    human_confirms_shadow = (
                        approved_by == "human" and status != "fail" and vby == "roux-shadow"
                    )
                    if status != "pass" and not human_confirms_shadow:
                        logger.warning(
                            f"APPROVAL BLOCKED: {proposal_id} — disclosure rule requires "
                            f"observer_verification.status='pass' (got {status!r}, verified_by={vby!r})"
                        )
                        return None
                    if human_confirms_shadow:
                        # The human's approval IS the confirmation of Roux's shadow-PASS — record it.
                        p.setdefault("disclosure", {})["observer_verification"] = {
                            **obs,
                            "status": "pass",
                            "verified_by": "human",
                            "comment": (
                                "Human confirmed Roux's shadow-PASS at approval (bigbrain-confirm "
                                "retired when the gate went local). Prior: "
                                + (obs.get("comment") or "")[:200]
                            ),
                            "confirmed_at": time.time(),
                        }
                        logger.info(
                            f"HUMAN CONFIRMED ROUX SHADOW-PASS: {proposal_id} → observer.status=pass by human"
                        )
                p["state"] = "approved"
                p["approved_at"] = time.time()
                p["approved_by"] = approved_by
                _save_active(active)
                _bb_log("proposal_approved", {
                    "proposal_id": proposal_id,
                    "approved_by": approved_by,
                    "category": p.get("category"),
                    "source": p.get("source"),
                }, source="proposal_bus")
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


# "roux-shadow" = Roux's advisory shadow-PASS held for human confirmation (2026-06-20). Distinct
# from "roux" (which is FAIL-only authoritative) — it carries status='pending' until a human confirms,
# so it is NOT subject to the roux-fail-only guard below (that's keyed on exactly verified_by=="roux").
VALID_VERIFIERS = {"human", "claude", "roux", "roux-shadow", "observer-agent"}

# Per 2026-05-17 Roux promotion amendment: asymmetric trust scope
ROUX_ALLOWED_STATUSES = {"fail"}  # Roux can attest fail but not pass


def set_observer_verification(
    proposal_id: str,
    status: str,
    comment: str = "",
    verified_by: str = "human",
) -> Optional[Dict]:
    """Mark observer_verification on an authored proposal.
    Returns updated proposal, or None if proposal not found / not applicable.
    Per 2026-05-17 amendments: verified_by accepts 'human' | 'claude' | 'roux' | 'observer-agent'.
    'roux' has asymmetric trust scope — may only attest status='fail', not 'pass'."""
    if status not in ("pending", "pass", "fail"):
        logger.error(f"invalid observer status: {status!r}")
        return None
    if verified_by not in VALID_VERIFIERS:
        logger.error(f"invalid verified_by: {verified_by!r}; must be one of {sorted(VALID_VERIFIERS)}")
        return None
    if verified_by == "roux" and status not in ROUX_ALLOWED_STATUSES:
        logger.warning(
            f"ATTESTATION REJECTED: verified_by='roux' attempted status='{status}' — "
            f"roux can only attest 'fail', not 'pass' (asymmetric trust per 2026-05-17 amendment)"
        )
        return None
    with _get_lock():
        active = _load_active()
        for p in active:
            if p["id"] == proposal_id:
                if not _disclosure_applies(p.get("source", "")):
                    logger.warning(f"set_observer_verification on non-authored proposal {proposal_id}; ignoring")
                    return None
                disclosure = p.get("disclosure") or {}
                # Audit-chain preservation (2026-05-17): snapshot prior real attestation
                # into observer_verification_history before overwriting. Empty 'pending'
                # initial state is NOT logged; only real pass/fail attestations are.
                disclosure.setdefault("observer_verification_history", [])
                prior = disclosure.get("observer_verification") or {}
                # IMPORTANT: snapshot prior values BEFORE mutating the dict (reference issue).
                prior_status = prior.get("status")
                prior_verified_by = prior.get("verified_by")
                prior_comment = prior.get("comment", "")
                prior_verified_at = prior.get("verified_at")
                if prior_status in ("pass", "fail"):
                    disclosure["observer_verification_history"].append({
                        "status": prior_status,
                        "comment": prior_comment,
                        "verified_at": prior_verified_at,
                        "verified_by": prior_verified_by,
                        "superseded_at": time.time(),
                        "superseded_by": verified_by,
                    })
                disclosure.setdefault("observer_verification", {})
                disclosure["observer_verification"]["status"] = status
                disclosure["observer_verification"]["comment"] = comment
                disclosure["observer_verification"]["verified_at"] = time.time()
                disclosure["observer_verification"]["verified_by"] = verified_by
                p["disclosure"] = disclosure
                _save_active(active)
                if prior_status in ("pass", "fail"):
                    logger.info(
                        f"OBSERVER VERIFICATION: {proposal_id} -> {status} by {verified_by} "
                        f"(SUPERSEDES prior {prior_status} by {prior_verified_by})"
                    )
                else:
                    logger.info(f"OBSERVER VERIFICATION: {proposal_id} -> {status} by {verified_by}")
                _bb_log("observer_verification_set", {
                    "proposal_id": proposal_id,
                    "status": status,
                    "verified_by": verified_by,
                }, source="proposal_bus")
                return p
        return None


def auto_dismiss_stale_failed(stale_hours: float = 24.0) -> List[Dict]:
    """Sweep active queue for proposals whose observer_verification.status='fail'
    and whose verified_at is older than `stale_hours`. Dismiss them into history
    with a structured result so the audit trail is clear.

    Returns the list of dismissed proposals.

    Why this exists: FAIL-attested proposals (Roux asymmetric, claude bigbrain,
    or human) are blocked from approval by the disclosure rule, but they don't
    self-clear. They accumulate until human dismisses them. This cron-callable
    sweep retires them after a cool-down period so the active queue reflects
    live decisions, not historical FAILs. Per DJ ask 2026-05-20.

    Author intent: only dismiss FAIL'd proposals. Pending and PASS'd are left
    alone. Heuristic-source proposals don't have observer_verification, so they're
    naturally untouched by this. Governance category proposals also untouched —
    a governance FAIL still merits human eyes on dismissal, by the spirit of the
    meta-policy.
    """
    cutoff = time.time() - (stale_hours * 3600.0)
    dismissed: List[Dict] = []
    with _get_lock():
        active = _load_active()
        history = _load_history()
        still_active: List[Dict] = []
        for p in active:
            if p.get("state") != "pending":
                still_active.append(p)
                continue
            if p.get("category") == "governance":
                still_active.append(p)
                continue
            disclosure = p.get("disclosure") or {}
            observer = disclosure.get("observer_verification") or {}
            if observer.get("status") != "fail":
                still_active.append(p)
                continue
            verified_at = observer.get("verified_at")
            # If verified_at missing, conservatively skip (don't dismiss something
            # we can't time-anchor).
            if not isinstance(verified_at, (int, float)):
                still_active.append(p)
                continue
            if verified_at > cutoff:
                still_active.append(p)
                continue
            # Stale FAIL — dismiss
            verified_by = observer.get("verified_by", "?")
            age_hours = round((time.time() - verified_at) / 3600.0, 2)
            p["state"] = "dismissed"
            p["resolved_at"] = time.time()
            p["result"] = {
                "auto_dismissed": True,
                "reason": f"FAIL attestation by {verified_by} aged past {stale_hours}h cool-down (was {age_hours}h old)",
                "verified_by": verified_by,
                "verified_at": verified_at,
                "stale_hours_threshold": stale_hours,
            }
            history.append(p)
            dismissed.append(p)
            logger.info(
                f"AUTO-DISMISS STALE FAIL: {p['id']} (verified_by={verified_by}, age={age_hours}h)"
            )
            _bb_log("proposal_auto_dismissed_stale_fail", {
                "proposal_id": p["id"],
                "title": p.get("title", "")[:80],
                "verified_by": verified_by,
                "age_hours": age_hours,
                "threshold_hours": stale_hours,
            }, source="proposal_bus")
        if dismissed:
            _save_active(still_active)
            _save_history(history)
    return dismissed


def dismiss_proposal(proposal_id: str, reason: str = "", by: str = "human") -> Optional[Dict]:
    """Dismiss a pending proposal. `reason` is captured (and, when set, fed to the
    roux_coaching archive by the caller so Roux LEARNS why — 2026-06-18, DJ: a dismissal
    should teach, not just delete)."""
    with _get_lock():
        active = _load_active()
        for i, p in enumerate(active):
            # pending OR approved — an approved-but-not-yet-applied proposal can still be
            # rejected (e.g. a false-pass caught at the execution ceremony). 2026-06-18.
            if p["id"] == proposal_id and p["state"] in ("pending", "approved"):
                p["state"] = "dismissed"
                p["resolved_at"] = time.time()
                if reason:
                    p["dismissed_reason"] = reason
                    p["dismissed_by"] = by
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
    """Auto-resolve observer-originated proposals whose underlying signal has cleared.
    Authored proposals (source='authored_by_*'), coach proposals, and research
    proposals are NOT touched — they're deliberate decisions, not heuristic flags."""
    with _get_lock():
        active = _load_active()
        still_active = []
        history = _load_history()
        for p in active:
            source = p.get("source", "heuristic")
            is_observer_origin = source == "heuristic"
            if (
                p["state"] == "pending"
                and is_observer_origin
                and p["title"] not in active_titles
            ):
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
        _src = p.get("source", "heuristic")
        _disc = None
        # 2026-06-19 (DJ): TASK + RESEARCH_TOPIC proposals get an authored_by_ source + disclosure block so
        # they flow through the v5.3 gate (sweeper → shadow-verify → verdict) and earn their own AUTONOMY
        # graduation tracks — instead of being un-gated suggestions that bank only audit-only ledger events.
        _cat = (p.get("category") or "").lower()
        if _cat in ("tasks", "research_topic"):
            from shared.authoring import build_disclosure as _bd
            if _cat == "tasks":
                _src = "authored_by_roux_taskmgmt"
                _reason = (
                    "Task-registry hygiene grounded in the live registry (tasks.json): retire stale/obsolete "
                    "PENDING tasks (>24h). The executor re-verifies staleness on disk before acting and the op "
                    "is reversible (status -> CANCELLED, not deletion). Evidence: " + (p.get("evidence") or p.get("description") or "stale pending tasks")
                )
            else:
                _src = "authored_by_roux_topicmgmt"
                _reason = (
                    "Research-topic curation grounded in the live fuel + topic registry: ADD a new on-thesis "
                    "research focus so discovery widens itself. ADD-only + reversible (snapshot-first); the handler "
                    "validates well-formed/redundancy/cap before applying, and EDIT/DELETE stay human-only. "
                    "Evidence: " + (p.get("evidence") or p.get("description") or "coverage gap in current research topics")
                )
            _disc = _bd(source_url="internal-reasoning", reasoning=_reason[:600])
        result = publish_proposal(
            title=p["title"],
            description=p.get("description", ""),
            category=p.get("category", ""),
            priority=p.get("priority", 5),
            proposed_action=p.get("proposed_action", ""),
            evidence=p.get("evidence", ""),
            reversible=p.get("reversible", True),
            source=_src,
            confidence=p.get("confidence", 1.0),
            coach_reasoning=p.get("coach_reasoning"),
            disclosure=_disc,
        )
        if result and result.get("state") == "pending":
            published += 1
    clear_resolved_issues(active_titles)
    bus_active = get_active()
    return {"proposals": bus_active, "observer_stats": observer_stats,
            "published": published, "total_active": len(bus_active), "timestamp": time.time()}
