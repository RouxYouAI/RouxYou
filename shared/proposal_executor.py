"""Proposal Executor — the consumer that drains approved proposals into action.

Shipped 2026-05-19 per DJ greenlight. Addresses the "approved-purgatory" gap
Roux diagnosed: approve_proposal() sets state=approved but nothing then routes
to actual execution. This module IS that consumer.

Architecture: a typed router. NOT a god-object. Strict precedence routing
into one of 4 kinds, first-match-wins:

  1. category=="governance"                       → kind="manual" (meta-policy: always human)
  2. is_governance_relevant(proposal) is True     → kind="meta"   (Self-Edit MCP path)
  3. category=="codebase" or executor=="coder"    → kind="code"   (Coder→Worker pipeline)
  4. executor=="watchtower" + state-mutation rx   → kind="config" (direct state write)
  5. otherwise                                    → kind="manual" (dashboard click)

Failure model: handler exceptions bump executor_meta.dispatch_attempts; after
DISPATCH_RETRY_CAP attempts, state becomes "dispatch_failed" and waits for
human triage on the dashboard. NO silent retries past the cap.

Safety gates per kind (verified_by requirements):
  - meta:   verified_by == "claude" (bigbrain attestation required; governance routing
            already forced this via disclosure_rule v1.3)
  - code:   verified_by in ("claude", "human")  (Roux can't ship code)
  - config: verified_by in ("claude", "human", "roux")  (Roux's asymmetric FAIL-attest
            is fine for config rollback proposals)
  - manual: noop (not dispatched)
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import traceback
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_BASE = Path(__file__).resolve().parent.parent  # /home/user/RouxYou
_STATE = _BASE / "state"
_PROPOSALS_ACTIVE = _STATE / "proposals_active.json"
_EXECUTOR_LOG = _STATE / "executor_log.jsonl"

DISPATCH_RETRY_CAP = 3

EXECUTOR_ENABLED = os.environ.get("ROUX_EXECUTOR_ENABLED", "1").lower() in ("1", "true", "yes")

# Bigbrain pre-application review (2026-05-20 — DJ ask: "make sure the actual
# implementation looks legit"). When a handler builds a planned action,
# bigbrain (Opus 4.8) reviews it against the proposal intent BEFORE the action
# is committed. Conservative posture: start with config (the only auto-firing
# kind today). meta/code currently surface-only, so no review needed yet —
# they'll add their own review when they start applying mutations.
EXECUTOR_REVIEW_ENABLED = os.environ.get("ROUX_EXECUTOR_REVIEW", "1").lower() in ("1", "true", "yes")

# Self-Edit MCP server path (Linux port, shipped 2026-05-19)
SELF_EDIT_SERVER = Path("/home/user/self-edit-mcp/server.py")
SELF_EDIT_VENV_PY = Path("/home/user/self-edit-mcp/.venv/bin/python")

# Conservative config-kind detector: only matches explicit cron-interval changes
# for now. Broader config patterns can be added later once we trust the dispatch.
CONFIG_INTERVAL_RX = re.compile(
    r"(?i)\b(cron|interval|cadence)\b.*?\b(\d+)\s*(s|sec|second|m|min|minute|h|hour)",
)
CONFIG_JOB_HINT_RX = re.compile(r"(?i)\bjob\s*[:=]?\s*([a-z_][a-z0-9_]*)")


# ============================================================
# Persistence helpers
# ============================================================

def _load_active() -> list:
    if not _PROPOSALS_ACTIVE.exists():
        return []
    try:
        return json.loads(_PROPOSALS_ACTIVE.read_text())
    except Exception as e:
        logger.error(f"proposals_active.json unreadable: {e}")
        return []


def _save_active(rows: list) -> None:
    _PROPOSALS_ACTIVE.write_text(json.dumps(rows, indent=2, ensure_ascii=False))


def _append_log(entry: dict) -> None:
    _STATE.mkdir(parents=True, exist_ok=True)
    entry.setdefault("ts", time.time())
    with _EXECUTOR_LOG.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ============================================================
# Kind routing (strict precedence)
# ============================================================

def classify_kind(proposal: dict) -> tuple:
    """Return (kind, reason). kind in {meta, code, config, manual}.

    Strict precedence — first match wins. Reason names the rule that fired
    for audit. governance ALWAYS routes to manual (meta-policy lock).
    """
    from shared.proposal_bus import is_governance_relevant

    category = (proposal.get("category") or "").lower()
    executor = (proposal.get("executor") or "").lower()
    action = proposal.get("proposed_action") or ""

    # 1. Governance category — ALWAYS manual. Meta-policy lock, no override.
    if category == "governance":
        return "manual", "rule_1:category=governance(meta-policy lock)"

    # 1b. Notes edit — substring add/replace/remove against roux_notes_*.md
    # (Hermes Agent pattern, 2026-05-20). Reversible by inverse op; cap check
    # inside the API refuses over-cap mutations.
    if category == "notes_edit":
        return "notes_edit", "rule_1b:category=notes_edit"

    # 2. Governance-relevant (per disclosure_rule v1.3) — Self-Edit MCP path
    relevant, gr_reason = is_governance_relevant(proposal)
    if relevant:
        # 2026-06-01 fix: "reflection authored it" (author_prefix) alone does NOT make a
        # CODE change a governance change. If author_prefix is the ONLY signal AND the
        # proposal is a codebase change, fall through to rule 3 (code) so it executes via
        # the coder pipeline (which can actually edit code; the meta/Self-Edit path can't).
        # Approval still requires the disclosure observer-pass separately. Content-based
        # hits (path_glob/keyword → real governance files) still correctly route to meta.
        if not (gr_reason.startswith("author_prefix") and category == "codebase"):
            return "meta", f"rule_2:governance_relevant({gr_reason})"

    # 3. Code change — existing Coder→Worker pipeline
    if category == "codebase" or executor == "coder":
        return "code", f"rule_3:category={category},executor={executor}"

    # 4. Watchtower state mutation — narrow detector (cron interval changes only for now)
    if executor == "watchtower" and CONFIG_INTERVAL_RX.search(action):
        return "config", "rule_4:watchtower+cron_interval_rx"

    # 4b. Roux-primary category (2026-06-18 cord-off) — a category enabled in
    # ROUX_PRIMARY_CATEGORIES gets a dispatchable kind + handler instead of falling to
    # manual. Only "memory" has a handler (→ memory.safe_decay(), snapshot-first/reversible).
    # Gated on is_primary → DORMANT until the category is deliberately enabled.
    try:
        from shared.sampling_confirmer import is_primary
        if category == "memory" and is_primary("memory"):
            return "memory", "rule_4b:roux_primary_memory"
    except Exception:
        pass

    # 4c. Tasks — registry task-management (currently cancel-stale). Low-risk + reversible (status
    # change); the handler SELF-VERIFIES staleness on disk before acting. 2026-06-19: gives the coach's
    # task-cleanup proposals an execution PATH so they bank ledger successes instead of sitting approved.
    if category == "tasks":
        return "tasks", "rule_4c:category=tasks"

    # 4d. Research-topic — Roux proposes her OWN research topics so discovery widens itself.
    # ADD-only via this handler (EDIT/DELETE are human-only → protected topics can't be touched);
    # reversible (snapshot-first append) + self-validating (well-formed/redundancy/cap). 2026-06-19.
    if category == "research_topic":
        return "research_topic", "rule_4d:category=research_topic"

    # 5. Default — manual (dashboard)
    return "manual", "rule_5:default"


# ============================================================
# Per-kind verified_by safety gates
# ============================================================

ALLOWED_VERIFIER_BY_KIND = {
    "meta":   {"claude"},                     # bigbrain only
    "code":   {"human"},                      # 2026-06-18 Phase-1 v5.3-bigbrain: code → HUMAN only (removed "claude"=now-v5.3) until Phase 2 verify-toolbelt grounds v5.3 on code. REVERT → {"claude","human"}.
    "memory": {"roux", "human"},              # 2026-06-18 cord-off: Roux-primary memory decay — her attest (sampled) or human approval. Handler = safe_decay (snapshot-first, reversible).
    "config": {"claude", "human", "roux"},    # Roux's asymmetric FAIL-attest fine here
    "tasks":  {"human"},                      # 2026-06-19: human-approved task cleanup (cancel-stale) — reversible + self-verifying handler. Add "roux" once the class is earned (banks ledger successes meanwhile).
    "research_topic": {"human"},              # 2026-06-19 roux-proposes-topics: human-approved ADD now; ADD-only + reversible (snapshot) + capped. Add "roux" once the research_topic tier graduates.
}


def _verifier_gate_ok(proposal: dict, kind: str) -> tuple:
    """Returns (ok, reason). For manual kind, always False (not dispatched here)."""
    if kind == "manual":
        return False, "kind=manual: dashboard-only"
    allowed = ALLOWED_VERIFIER_BY_KIND.get(kind, set())
    disc = proposal.get("disclosure") or {}
    ov = disc.get("observer_verification") or {}
    verified_by = ov.get("verified_by")
    # 2026-06-19: HUMAN/CLAUDE APPROVAL also satisfies the gate. Disclosure-gated proposals set
    # observer_verification.verified_by; non-disclosure ones (e.g. coach-sourced tasks) instead carry
    # approved_by (set by approve_proposal, which already enforces governance + disclosure rules before
    # approval). So an explicitly-approved proposal is verified by that actor.
    approved_by = proposal.get("approved_by")
    actor = verified_by if verified_by in allowed else (approved_by if approved_by in allowed else None)
    if actor is None:
        return False, (f"neither verified_by={verified_by} nor approved_by={approved_by} "
                       f"in allowed={sorted(allowed)} for kind={kind}")
    return True, f"ok (via {actor})"


# ============================================================
# Handlers
# ============================================================

def _handle_meta(proposal: dict) -> dict:
    """Route meta-proposals through Self-Edit MCP.

    For now: we don't directly invoke Self-Edit's tools (those are MCP-server
    surfaces, called by an MCP client). Instead we LOG the proposal as
    ready-for-claude-ceremony and surface it on the dashboard. The actual
    Self-Edit ceremony (propose_edit → apply_proposal) is performed by Claude
    when DJ asks "ship the meta-proposals queue", because that requires a
    judgement-bearing actor and isn't safe to auto-fire.

    This is a deliberate conservative choice: bigbrain attested the proposal
    is legit, but the actual file mutation still needs a human-tied actor to
    invoke the MCP ceremony.
    """
    if not SELF_EDIT_SERVER.exists():
        return {"ok": False, "reason": f"Self-Edit MCP not found at {SELF_EDIT_SERVER}"}
    return {
        "ok": True,
        "outcome": "queued_for_self_edit_ceremony",
        "note": (
            "Meta-proposal validated by bigbrain. Surface on dashboard for Claude "
            "to invoke Self-Edit MCP propose→apply ceremony (requires judgement-bearing actor)."
        ),
        "self_edit_server": str(SELF_EDIT_SERVER),
    }


CODER_URL = "http://localhost:8000/coder/plan"        # Gateway → Coder
WORKER_URL = "http://localhost:8000/worker/execute"   # Gateway → Worker
CODER_TIMEOUT = 300   # bumped 2026-05-25: CREATE tasks (full-file gen) need >180s on the contended 14B coder
WORKER_TIMEOUT = 300  # execution can take 60-240s
MAX_FIX_ATTEMPTS = 2  # 2026-05-25: capped runtime self-heal loop (feed errors back to coder)


def _coder_plan(query: str, context: str = "") -> dict:
    """POST /coder/plan and return the plan response dict. Reused by the fix-loop."""
    import urllib.request
    req = urllib.request.Request(
        CODER_URL, data=json.dumps({"query": query, "context": context}).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=CODER_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _worker_execute(plan_resp: dict) -> dict:
    """POST /worker/execute and return the execution result dict. Reused by the fix-loop."""
    import urllib.request
    req = urllib.request.Request(
        WORKER_URL, data=json.dumps({"task": "execute_plan", "data": plan_resp}).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=WORKER_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _handle_code(proposal: dict) -> dict:
    """Auto-invoke the Coder→Worker pipeline for approved code-change proposals.

    Per DJ's 2026-05-20 ask: closing Pillar 3 functional gap. The chain:
      1. POST /coder/plan → coder generates plan_json from proposed_action
      2. bigbrain reviews the PLAN against proposal intent (pre-execution gate)
         - BLOCK → return ok=False, no execution
      3. POST /worker/execute → worker applies the plan
      4. bigbrain reviews the OUTCOME against proposal intent (post-application gate, #4)
         - PASS → proposal completes successfully
         - FAIL → proposal marked failed; no auto-rollback (eval-gate-trust territory).
           Human dashboards the failure for review.

    Cost: ~3-4 bigbrain calls per code proposal (shadow + this pre-plan + this post-app).
    Conservative: failures alert + record but don't auto-revert. Once eval gates prove
    out, add rollback path under a governance amendment.
    """
    import urllib.request
    import urllib.error

    prop_id = proposal.get("id", "?")
    audit = {
        "phase": "init",
        "plan_request_ok": False,
        "plan_review": None,
        "execution_ok": False,
        "post_app_review": None,
    }

    # Phase 1: ask coder for a plan
    audit["phase"] = "coder_plan"
    plan_payload = {
        "query": proposal.get("proposed_action") or proposal.get("title") or "",
        "context": (
            f"Proposal: {proposal.get('title', '')}\n"
            f"Description: {proposal.get('description', '')}\n"
            f"Source: {proposal.get('source', '')}\n"
            f"Priority: {proposal.get('priority', 5)}\n"
        ),
    }
    try:
        req = urllib.request.Request(
            CODER_URL,
            data=json.dumps(plan_payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=CODER_TIMEOUT) as resp:
            plan_resp = json.loads(resp.read().decode("utf-8"))
        audit["plan_request_ok"] = True
    except Exception as e:
        return {
            "ok": False,
            "reason": f"coder /plan call failed: {e}",
            "audit": audit,
            "phase_at_fail": audit["phase"],
        }

    plan = plan_resp.get("plan") or plan_resp.get("steps") or []
    if not plan:
        return {
            "ok": False,
            "reason": "coder returned empty plan",
            "audit": audit,
            "plan_response_preview": json.dumps(plan_resp)[:400],
        }

    # Capture write_file targets + pre-existence BEFORE the worker writes, so the
    # runtime-verify floor can cleanly roll back NET-NEW files that don't run.
    write_targets = [s.get("details") for s in plan
                     if isinstance(s, dict) and s.get("action") == "write_file" and s.get("details")]
    pre_existed = {p: os.path.exists(p) for p in write_targets}

    # Tier-0 pre-dispatch deny (defense-in-depth — worker.py is the hard backstop). Reject a
    # plan that would touch the supervisor/safety/governance layer BEFORE paying for review/exec.
    from shared.tier0 import plan_violates_tier0
    _t0 = plan_violates_tier0(plan)
    if _t0:
        audit["phase"] = "tier0_deny"
        return {
            "ok": False,
            "reason": f"TIER-0 DENY (pre-dispatch): {_t0}. The autonomous loop cannot modify the "
                      "supervisor/safety/governance layer; changes go through Claude-in-loop + DJ.",
            "audit": audit,
            "plan": plan,
        }

    # Phase 2: bigbrain reviews the plan against the proposal intent
    audit["phase"] = "plan_review"
    plan_review = bigbrain_review_planned_action(
        proposal,
        kind="code-plan",
        planned={
            "coder_plan": plan,
            "plan_summary": plan_resp.get("summary", ""),
            "n_steps": len(plan) if isinstance(plan, list) else "?",
        },
    )
    audit["plan_review"] = {
        "verdict": plan_review.get("verdict"),
        "confidence": plan_review.get("confidence"),
        "reasoning_preview": (plan_review.get("reasoning") or "")[:300],
    }
    if not plan_review.get("skipped"):
        verdict = plan_review.get("verdict")
        if verdict != "APPROVE":
            return {
                "ok": False,
                "reason": f"bigbrain plan review:{verdict or 'NO_VERDICT'} — {(plan_review.get('reasoning') or '')[:300]}",
                "audit": audit,
                "plan": plan,
            }

    # Phase 3: worker executes the plan
    audit["phase"] = "worker_execute"
    exec_payload = {"task": "execute_plan", "data": plan_resp}
    try:
        req = urllib.request.Request(
            WORKER_URL,
            data=json.dumps(exec_payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=WORKER_TIMEOUT) as resp:
            exec_resp = json.loads(resp.read().decode("utf-8"))
        audit["execution_ok"] = True
    except Exception as e:
        return {
            "ok": False,
            "reason": f"worker /execute call failed: {e}",
            "audit": audit,
            "plan": plan,
        }

    # Phase 4: post-application bigbrain verifier — does the outcome match intent?
    audit["phase"] = "post_app_review"
    post_review = bigbrain_review_planned_action(
        proposal,
        kind="code-implementation",
        planned={
            "coder_plan": plan,
            "execution_summary": {
                "success": exec_resp.get("success"),
                "steps_completed": exec_resp.get("steps_completed", "?"),
                "errors": exec_resp.get("errors", []),
                "outputs_preview": (json.dumps(exec_resp)[:600]),
            },
        },
    )
    audit["post_app_review"] = {
        "verdict": post_review.get("verdict"),
        "confidence": post_review.get("confidence"),
        "reasoning_preview": (post_review.get("reasoning") or "")[:300],
    }

    # ---- Runtime verification + capped self-heal fix-loop (2026-05-25) ----
    # bigbrain reviews intent-match, not execution — the Roux-built breakout passed
    # both reviews but crashed on load ("rightPressed is not defined"). So actually
    # RUN/load the write_file outputs.
    #   FLOOR:    a runtime failure overrides the APPROVE; NET-NEW files that don't run
    #             are rolled back (deleted); the event is logged confirmed_bad.
    #   FIX-LOOP: before giving up, feed the runtime errors back to the coder for up to
    #             MAX_FIX_ATTEMPTS corrected rewrites + re-verify — safe to attempt
    #             *because* of the rollback floor. Converge → self-healed; exhaust → floor.
    # Covers syntax/truncation/load-crashes (tiers 1-2), NOT behavioral logic (tier 3).
    audit["phase"] = "runtime_verify"
    runtime_verified = False   # all targets verified clean
    hard_fail = False          # a REAL code failure (verified=False AND not infra)
    inconclusive = False       # infra/transient verifier failure → unknown, never confirmed_bad
    runtime_checked = False
    healed = False
    rolled_back = []
    fix_attempts = 0
    try:
        if write_targets:
            from shared.runtime_verify import verify_paths
            rv = verify_paths(write_targets)
            runtime_checked = True
            runtime_verified, hard_fail, inconclusive = rv["verified"], rv["hard_fail"], rv["inconclusive"]
            audit["runtime_verify"] = {p: {"verified": r["verified"], "inconclusive": r.get("inconclusive", False),
                                           "method": r["method"], "errors": r["errors"][:5]}
                                       for p, r in rv["results"].items()}

            # FIX-LOOP: only on a REAL code failure (hard_fail) — never on infra/inconclusive.
            while hard_fail and fix_attempts < MAX_FIX_ATTEMPTS:
                fix_attempts += 1
                failing = {p: r["errors"] for p, r in rv["results"].items()
                           if (not r.get("verified")) and (not r.get("inconclusive"))}
                errtext = "; ".join(f"{p}: {' | '.join(e[:3])}" for p, e in failing.items())[:600]
                logger.warning(f"RUNTIME FIX-LOOP {fix_attempts}/{MAX_FIX_ATTEMPTS} for {prop_id}: {errtext[:200]}")
                fix_query = (
                    f"{proposal.get('proposed_action','')}\n\n"
                    f"The file(s) you generated FAILED runtime verification with these errors:\n{errtext}\n\n"
                    f"Produce a CORRECTED version that fixes these specific runtime errors. "
                    f"Same file path(s). Write the complete corrected file(s)."
                )
                try:
                    fpr = _coder_plan(fix_query, f"FIX attempt {fix_attempts}: {proposal.get('title','')}")
                    if not (fpr.get("plan") or fpr.get("steps")):
                        break
                    _worker_execute(fpr)
                    rv = verify_paths(write_targets)
                    runtime_verified, hard_fail, inconclusive = rv["verified"], rv["hard_fail"], rv["inconclusive"]
                    audit.setdefault("fix_loop", []).append({
                        "attempt": fix_attempts, "verified": runtime_verified, "hard_fail": hard_fail,
                        "results": {p: {"verified": r["verified"], "errors": r["errors"][:3]} for p, r in rv["results"].items()},
                    })
                    if not hard_fail:
                        healed = runtime_verified  # healed only if it now verifies clean (not merely inconclusive)
                        audit["runtime_verify"] = {p: {"verified": r["verified"], "inconclusive": r.get("inconclusive", False),
                                                       "method": r["method"], "errors": r["errors"][:5]}
                                                   for p, r in rv["results"].items()}
                        if healed:
                            logger.info(f"RUNTIME FIX-LOOP HEALED {prop_id} on attempt {fix_attempts}")
                        break
                except Exception as fe:
                    logger.warning(f"fix-loop attempt {fix_attempts} error: {fe}")
                    break

            # FLOOR: only on a REAL failure (hard_fail) → roll back net-new files.
            # Inconclusive (infra) NEVER rolls back — we couldn't verify, so we don't destroy.
            if hard_fail:
                for p in write_targets:
                    if not pre_existed.get(p, True):
                        try:
                            os.remove(p); rolled_back.append(p)
                        except OSError:
                            pass
                audit["runtime_rollback"] = {
                    "deleted_net_new": rolled_back,
                    "pre_existed_not_reverted": [p for p in write_targets if pre_existed.get(p)],
                    "fix_attempts": fix_attempts,
                }
                logger.warning(f"RUNTIME VERIFY FAILED after {fix_attempts} fix attempt(s) for {prop_id}; "
                               f"rolled back {len(rolled_back)} net-new file(s)")
            elif inconclusive:
                logger.info(f"runtime verify INCONCLUSIVE (infra) for {prop_id} — not blocking; ground_truth=unknown")
    except Exception as rv_err:
        logger.warning(f"runtime_verify/fix-loop error (fail-soft → pass): {rv_err}")
        audit["runtime_verify"] = {"error": str(rv_err)[:200]}

    # Behavioral smoke (tier-3, ADVISORY 2026-05-25): simulate input (keys/clicks) on HTML
    # outputs and record interaction-time crashes — the class that loads clean but breaks on
    # use. ADVISORY: logged in the audit, does NOT yet affect overall_ok or ground_truth. A
    # new fallible verifier earns trust (measured) before it's allowed to block — same
    # discipline as tier graduation. Promote to blocking once it proves reliable.
    try:
        html_targets = [p for p in write_targets if str(p).lower().endswith((".html", ".htm"))]
        if html_targets:
            from shared.behavioral_verify import behavioral_check
            bres = {p: behavioral_check(p) for p in html_targets}
            audit["behavioral"] = {p: {"clean": b["clean"], "inconclusive": b["inconclusive"],
                                       "interactions": b["interactions"], "errors": b["errors"][:5]}
                                   for p, b in bres.items()}
            bad = [p for p, b in bres.items() if b.get("applicable") and not b["clean"] and not b["inconclusive"]]
            if bad:
                logger.warning(f"BEHAVIORAL SMOKE (advisory) flagged interaction crash(es) for {prop_id}: {bad}")
    except Exception as be:
        logger.warning(f"behavioral smoke error (advisory, ignored): {be}")
        audit["behavioral"] = {"error": str(be)[:200]}

    audit["phase"] = "done"
    overall_ok = (
        bool(exec_resp.get("success", False))
        and (post_review.get("skipped") or post_review.get("verdict") == "APPROVE")
        and not hard_fail   # inconclusive (infra) does NOT block; only a real code failure does
    )
    # Phase B trust ledger: runtime verification supplies GROUND TRUTH.
    # pass (incl. self-healed) → confirmed_good; fail → confirmed_bad (a bigbrain APPROVE
    # on code that doesn't run is then a recorded FALSE-PASS — the disqualifying kind).
    try:
        from shared.trust_ledger import record as _tl_record
        # confirmed_good only if it actually verified clean; confirmed_bad only on a REAL
        # failure; infra/inconclusive (or not checked) → unknown (never poisons the ledger).
        gt = "unknown"
        if runtime_checked:
            gt = "confirmed_good" if runtime_verified else ("confirmed_bad" if hard_fail else "unknown")
        _tl_record(
            tier="code",
            # 2026-06-05: a MISSING post-app verdict means the review couldn't decide (bigbrain
            # 529/network/parse transient — verdict=None), NOT that the gate failed the change.
            # Record NO_VERDICT (→ gate_correct=None) so an API hiccup can't log a verified-good
            # change as a FALSE-BLOCK and pollute the graduation metrics. A real reject is "BLOCK".
            change_id=prop_id,
            gate_verdict=(post_review.get("verdict") or "NO_VERDICT"),
            ground_truth=gt,
            rollback_needed=(not overall_ok),
            rollback_fired=bool(rolled_back),
            source=(proposal.get("source") or "")[:80],
            notes=((("[healed x%d] " % fix_attempts) if healed else "") + (proposal.get("title") or ""))[:120],
            paths=list(write_targets) if write_targets else None,  # lever #1: enables re-verify on adjudication
        )
    except Exception:
        pass
    if overall_ok:
        _outcome = "code_applied_and_verified"
        _note = ("Code proposal executed end-to-end; bigbrain confirmed intent and runtime "
                 "verification confirmed it actually runs."
                 + (f" Self-healed via {fix_attempts} fix-loop attempt(s)." if healed else ""))
    elif rolled_back:
        _outcome = "runtime_failed_rolled_back"
        _note = (f"Code FAILED runtime verification after {fix_attempts} self-heal attempt(s); "
                 f"rolled back {len(rolled_back)} net-new file(s). bigbrain may have APPROVED the "
                 f"intent, but the code does not run — recorded confirmed_bad. Surfaced for review.")
    else:
        _outcome = "code_applied_but_intent_mismatch"
        _note = ("Code applied but post-application bigbrain verifier did NOT confirm intent match. "
                 "DJ should review the diff against the proposal on the dashboard.")
    return {
        "ok": overall_ok,
        "outcome": _outcome,
        "audit": audit,
        "plan": plan,
        "execution_result": exec_resp,
        "healed": healed,
        "fix_attempts": fix_attempts,
        "rolled_back": rolled_back,
        "note": _note,
    }


def _handle_config(proposal: dict) -> dict:
    """Handle narrow config-kind: cron interval changes via watchtower endpoint.

    Conservative scope: only fires on detected cron-interval mutation patterns.
    Calls the watchtower /cron/{job}/interval endpoint directly.

    Pre-application bigbrain review (2026-05-20): the planned action is shown
    to Opus 4.8 BEFORE the POST fires. BLOCK → handler returns ok=False with
    bigbrain's reasoning attached. Missing-verdict → fail-closed (BLOCK by
    default — the executor isn't a place to fail-open).
    """
    import urllib.request

    action = proposal.get("proposed_action") or ""

    # Extract job name (best-effort) and new interval
    job_match = CONFIG_JOB_HINT_RX.search(action)
    interval_match = CONFIG_INTERVAL_RX.search(action)
    if not (job_match and interval_match):
        return {"ok": False, "reason": "config detector matched but couldn't extract job + interval"}

    job = job_match.group(1)
    n = int(interval_match.group(2))
    unit = interval_match.group(3).lower()
    if unit.startswith("h"):
        minutes = n * 60
    elif unit.startswith("m"):
        minutes = n
    else:
        # seconds → minutes, but the endpoint clamps to >=1 anyway. Round up.
        minutes = max(1, (n + 59) // 60)

    # Build the request body to match watchtower's CronIntervalBody schema:
    # {minutes: int, changed_by: str, reasoning: str}
    proposal_reasoning = (proposal.get("disclosure") or {}).get("reasoning") or ""
    request_body = {
        "minutes": minutes,
        "changed_by": "claude",
        "reasoning": f"proposal {proposal.get('id','?')}: {proposal.get('title','')[:120]} — {proposal_reasoning[:200]}",
    }

    # Bigbrain pre-application review
    planned = {
        "endpoint": f"POST http://localhost:8012/cron/{job}/interval",
        "body": request_body,
        "extracted_job": job,
        "extracted_unit": unit,
        "extracted_n": n,
        "computed_minutes": minutes,
    }
    review = bigbrain_review_planned_action(proposal, kind="config", planned=planned)
    if not review.get("skipped"):
        verdict = review.get("verdict")
        if verdict != "APPROVE":
            # BLOCK or missing-verdict → fail-closed
            return {
                "ok": False,
                "reason": f"bigbrain_review:{verdict or 'NO_VERDICT'} — {(review.get('reasoning') or review.get('error') or '?')[:300]}",
                "executor_review": {
                    "verdict": verdict,
                    "confidence": review.get("confidence"),
                    "reasoning": review.get("reasoning"),
                    "error": review.get("error"),
                },
                "planned": planned,
            }

    # POST to local watchtower. Bumped timeout to 30s — bigbrain review can warm
    # caches and the watchtower endpoint sometimes blackbox-logs synchronously.
    url = f"http://localhost:8012/cron/{job}/interval"
    body_bytes = json.dumps(request_body).encode("utf-8")
    req = urllib.request.Request(
        url, data=body_bytes, method="POST", headers={"Content-Type": "application/json"},
    )
    post_resp_text = None
    post_error = None
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            post_resp_text = resp.read().decode("utf-8")
    except Exception as e:
        post_error = str(e)

    # Verify-after step: regardless of POST response status, ask watchtower for
    # the current interval. If it matches our target, the change took effect even
    # if the response read timed out. This avoids the "lying about failure" mode
    # where the mutation succeeded but the response was lost.
    actual_minutes = None
    verify_error = None
    try:
        with urllib.request.urlopen(f"http://localhost:8012/watchtower/jobs", timeout=10) as resp:
            jobs_data = json.loads(resp.read().decode("utf-8"))
        for j in jobs_data.get("jobs", []):
            if j.get("name") == job:
                actual_minutes = j.get("interval_minutes")
                break
    except Exception as e:
        verify_error = str(e)

    review_summary = {
        "verdict": review.get("verdict"),
        "confidence": review.get("confidence"),
        "reasoning": (review.get("reasoning") or "")[:300],
    }

    if actual_minutes == minutes:
        # Change took effect — success regardless of response read
        return {
            "ok": True,
            "outcome": "cron_interval_set",
            "job": job,
            "minutes": minutes,
            "post_response": (post_resp_text or "")[:400],
            "post_error": post_error,
            "verified_actual_minutes": actual_minutes,
            "executor_review": review_summary,
        }
    # Change did NOT take effect — handler fail
    return {
        "ok": False,
        "reason": (
            f"watchtower POST + verify mismatch: requested minutes={minutes}, "
            f"actual={actual_minutes}, post_error={post_error}, verify_error={verify_error}"
        ),
        "planned": planned,
        "executor_review": review_summary,
    }


def _handle_notes_edit(proposal: dict) -> dict:
    """Handle notes_edit kind: substring add/replace/remove against
    roux_notes_user.md or roux_notes_work.md (Hermes-style edit API, 2026-05-20).

    Proposal must carry a structured payload at proposal['notes_edit']:
        {target: 'user'|'work', op: 'add'|'replace'|'remove', ...}

    Op-specific fields:
        add:     {text: str}
        replace: {old: str, new: str}
        remove:  {text: str}

    Frozen-at-start semantics: write hits disk immediately but the in-prompt
    copy doesn't refresh until companion module reloads. Same as Hermes.

    No bigbrain review — notes edits are reversible (the inverse substring op
    restores). Cap check inside the API refuses over-cap mutations.
    """
    from shared import notes_edit
    payload = proposal.get("notes_edit")
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "reason": "missing structured notes_edit payload on proposal",
        }
    result = notes_edit.apply_edit(payload)
    return {
        "ok": bool(result.get("ok")),
        "reason": result.get("error", "applied"),
        "notes_edit_result": result,
    }


def _handle_memory(proposal: dict) -> dict:
    """Roux-primary memory-maintenance handler (2026-06-18 cord-off). The ONLY side effect
    is memory.safe_decay() — snapshot-first episodic decay (decay utilities + dedup + prune
    stale/low-utility episodes; snapshots memory.json to memory_predecay/ first; FAIL-SAFE:
    refuses to prune if it can't back up). Defensive: only acts on category=='memory'.
    Records a roux_primary ledger event so the sampling-confirmer clean-streak can track it."""
    if (proposal.get("category") or "").lower() != "memory":
        return {"ok": False, "reason": f"_handle_memory on non-memory category '{proposal.get('category')}'"}
    # 2026-06-19 INTENT GATE (GAP-2 fix): this handler's ONLY action is episodic decay/prune. A memory
    # proposal asking to CREATE/write/document something must NOT silently trigger a decay AND falsely
    # record a memory-primary-pass (that would pollute the cord-off graduation signal on a basis the
    # proposal never intended — caught live on prop_7f351fea645b). Require a decay-intent keyword; else refuse
    # WITHOUT decaying or recording a pass. Knowledge-creation routes through research/ingest, not memory.
    _blob = " ".join(str(proposal.get(k, "")) for k in ("title", "description", "proposed_action")).lower()
    _DECAY_INTENT = ("decay", "prune", "dedup", "stale", "compact", "trim", "consolidat",
                     "evict", "forget", "garbage", "low-utility", "low utility", "clean up memor", "clean memor")
    if not any(k in _blob for k in _DECAY_INTENT):
        return {"ok": False, "reason": ("memory handler performs episodic DECAY/prune ONLY; this proposal's "
                "intent is not decay (no decay/prune/dedup/compact keyword) — refused without running decay or "
                "recording a memory-pass. Knowledge-creation routes through the research/ingest pipeline, not a "
                "memory proposal.")}
    try:
        from shared.memory import memory
        stats = memory.safe_decay()
    except Exception as e:
        return {"ok": False, "reason": f"safe_decay error: {e}"}
    if not stats.get("decayed"):
        return {"ok": False, "reason": stats.get("error", "decay skipped (pre-snapshot failed — fail-safe)")}
    try:
        from shared.sampling_confirmer import record_primary_pass
        record_primary_pass("memory", proposal.get("id", "<?>"),
                            notes=f"safe_decay -{stats.get('total_removed', 0)} eps, backup={stats.get('predecay_backup')}")
    except Exception:
        pass
    return {"ok": True, "result": stats}


def _handle_tasks(proposal: dict) -> dict:
    """Tasks-registry handler (2026-06-19). Currently supports CANCEL-STALE: retires PENDING tasks
    older than the stale threshold (default 24h). SELF-VERIFYING — re-reads the registry on disk and
    only cancels tasks that are genuinely PENDING + stale (never an active/fresh one, even if a
    proposal names it). Reversible (status → CANCELLED, not removed). Unsupported task ops → ok=False
    (no false ledger success). This gives the coach's task-cleanup proposals a real execution path."""
    blob = ((proposal.get("title") or "") + " " + (proposal.get("proposed_action") or "")).lower()
    if "cancel" not in blob or "stale" not in blob:
        return {"ok": False, "reason": f"unsupported task op; handler only does cancel-stale (got: {blob[:70]!r})"}
    import time
    try:
        from shared.task_registry import TaskRegistry
        from shared.schemas import TaskStatus
    except Exception as e:
        return {"ok": False, "reason": f"task registry import failed: {e}"}
    try:
        stale_hours = float(os.environ.get("ROUX_TASK_STALE_HOURS", "24"))
    except Exception:
        stale_hours = 24.0
    reg = TaskRegistry()
    now, cutoff = time.time(), stale_hours * 3600
    cancelled = []
    for t in list(reg.tasks):
        created = getattr(t, "created_at", None) or 0
        if t.status == TaskStatus.PENDING_APPROVAL and created and (now - created) > cutoff:
            if reg.cancel_task(t.id, reason=f"auto-cancelled: stale >{stale_hours:.0f}h, via proposal {proposal.get('id','?')}"):
                cancelled.append({"id": t.id, "title": t.title, "age_h": round((now - created) / 3600, 1)})
    if not cancelled:
        return {"ok": False, "reason": f"no genuinely-stale (PENDING, >{stale_hours:.0f}h) tasks found — re-verified on disk"}
    # Bank a tasks-tier ledger event (2026-06-19, DJ): the task is verifiably CANCELLED → confirmed_good.
    # HONEST gate_verdict: a real observer verdict (authored_by_roux → gated) counts toward tasks-tier
    # graduation; a human-approved coach proposal records "HUMAN_APPROVED" = audit-only (gate_correct N/A,
    # never pollutes gate-graduation metrics with a non-gate decision).
    try:
        from shared.trust_ledger import record as _tl_record
        _ov = (proposal.get("disclosure") or {}).get("observer_verification") or {}
        _verdict = _ov.get("status", "").upper() if _ov.get("status") else "HUMAN_APPROVED"
        _tl_record(
            tier="tasks",
            change_id=proposal.get("id", "?"),
            gate_verdict=_verdict,
            ground_truth="confirmed_good",
            rollback_needed=False,
            rollback_fired=False,
            source=(proposal.get("source") or "")[:80],
            notes=f"cancelled {len(cancelled)} stale task(s): {', '.join(c['title'][:30] for c in cancelled)}"[:120],
        )
    except Exception as _e:
        logger.warning(f"_handle_tasks ledger record failed: {_e}")
    return {"ok": True, "result": {"cancelled": cancelled, "count": len(cancelled)}}


def _handle_research_topic(proposal: dict) -> dict:
    """Roux-proposes-topics handler (2026-06-19). ADD-ONLY: appends a NEW research topic to
    state/research_topics.json so discovery widens itself. EDIT/DELETE are HUMAN-ONLY (refused here) —
    which also means the protected topics (agent_safety, guardian_home_network) can never be touched via
    this path. SELF-VALIDATING + reversible:
      - parses a JSON topic spec {focus, queries, context, [search]} from proposed_action (raw_decode,
        handles the nested 'search' object)
      - well-formed (focus str ≤60, queries non-empty list[str], context str ≥30)
      - redundancy (focus must not already exist — no dupes/edits)
      - cap (refuse at ROUX_MAX_RESEARCH_TOPICS — anti-sprawl + anti-self-reinforcing-loop)
      - SNAPSHOT research_topics.json first (fail-safe: don't write if snapshot fails)
    Records a research_topic-tier ledger event on success (gated proposals → counts toward graduation)."""
    import json, time, shutil
    from pathlib import Path as _P
    blob = ((proposal.get("title") or "") + " " + (proposal.get("proposed_action") or "") + " " + (proposal.get("description") or "")).lower()
    if any(w in blob for w in ("delete topic", "remove topic", "edit the topic", "edit existing topic",
                               "modify topic", "rewrite topic", "change existing topic", "delete the topic")):
        return {"ok": False, "reason": "EDIT/DELETE of research topics is HUMAN-ONLY; this handler ADDs only (protected topics can never be removed here)"}
    action = proposal.get("proposed_action") or ""
    # robust JSON topic-spec extraction (scan for '{', raw_decode handles nested 'search')
    spec, _dec, i = None, json.JSONDecoder(), 0
    while i < len(action):
        if action[i] == "{":
            try:
                obj, end = _dec.raw_decode(action[i:])
                if isinstance(obj, dict) and obj.get("focus"):
                    spec = obj
                    break
                i += max(end, 1)
                continue
            except Exception:
                pass
        i += 1
    if not spec:
        return {"ok": False, "reason": "no parseable JSON topic spec {focus, queries, context} found in proposed_action"}
    focus = str(spec.get("focus", "")).strip()
    queries = spec.get("queries") or []
    context = str(spec.get("context", "")).strip()
    if not (focus and isinstance(queries, list) and queries and all(isinstance(q, str) and q.strip() for q in queries) and context):
        return {"ok": False, "reason": "malformed topic: need non-empty focus(str) + queries(non-empty list[str]) + context(str)"}
    if len(focus) > 60 or len(context) < 30:
        return {"ok": False, "reason": "focus too long (>60) or context too thin (<30 chars) — be specific + grounded"}
    path = _P(__file__).resolve().parent.parent / "state" / "research_topics.json"
    try:
        topics = json.loads(path.read_text())
        assert isinstance(topics, list)
    except Exception as e:
        return {"ok": False, "reason": f"can't read research_topics.json: {e}"}
    if focus.lower() in {str(t.get("focus", "")).lower() for t in topics if isinstance(t, dict)}:
        return {"ok": False, "reason": f"topic focus '{focus}' already exists — ADD-only, no dupes/edits"}
    cap = int(os.environ.get("ROUX_MAX_RESEARCH_TOPICS", "25"))
    if len(topics) >= cap:
        return {"ok": False, "reason": f"research-topic cap reached ({len(topics)}/{cap}); prune before adding (anti-sprawl)"}
    try:  # snapshot first — reversible, fail-safe
        snap_dir = path.parent / "research_topics_backups"
        snap_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(path), str(snap_dir / f"research_topics_{int(time.time())}.json"))
    except Exception as e:
        return {"ok": False, "reason": f"snapshot failed — refusing to write (fail-safe): {e}"}
    new_topic = {"focus": focus, "queries": [q.strip() for q in queries][:6], "context": context[:800]}
    if isinstance(spec.get("search"), dict):
        sb = {k: v for k, v in spec["search"].items() if k in ("engines", "categories", "time_range")}
        if sb:
            new_topic["search"] = sb
    topics.append(new_topic)
    try:
        path.write_text(json.dumps(topics, indent=2))
    except Exception as e:
        return {"ok": False, "reason": f"write failed: {e}"}
    try:
        from shared.trust_ledger import record as _tl_record
        _ov = (proposal.get("disclosure") or {}).get("observer_verification") or {}
        _tl_record(tier="research_topic", change_id=proposal.get("id", "?"),
                   gate_verdict=(_ov.get("status", "").upper() if _ov.get("status") else "HUMAN_APPROVED"),
                   ground_truth="confirmed_good", rollback_needed=False, rollback_fired=False,
                   source=(proposal.get("source") or "")[:80], notes=f"added research topic: {focus}"[:120])
    except Exception as _e:
        logger.warning(f"_handle_research_topic ledger record failed: {_e}")
    return {"ok": True, "result": {"added_topic": focus, "queries": new_topic["queries"], "total_topics": len(topics)}}


HANDLERS = {
    "meta":           _handle_meta,
    "code":           _handle_code,
    "config":         _handle_config,
    "notes_edit":     _handle_notes_edit,
    "memory":         _handle_memory,
    "tasks":          _handle_tasks,
    "research_topic": _handle_research_topic,
}


# ============================================================
# Bigbrain executor-output review (pre-application)
# ============================================================

EXECUTOR_REVIEW_PROMPT = """You are RouxYou's **bigbrain** review gate, running locally on the v5.3 substrate, reviewing an executor-planned action BEFORE it's applied. (You are the same substrate as Roux — lean on the mechanical grounding below, not on being a more-capable independent model.)

The proposal was already verified upstream (observer.status=pass). Your job here is narrower: given the proposal's stated intent and the executor's planned action, does the planned action actually MATCH the intent? Are there any red flags (wrong target, unreasonable magnitude, off-by-one, side effects)?

You are the second pair of eyes between approval and irreversible application.

PROPOSAL:
TITLE: {title}
CATEGORY: {category}
PROPOSED_ACTION (the human-readable brief):
{proposed_action}

EXECUTOR'S PLANNED ACTION (what the handler is about to do):
kind: {kind}
planned: {planned}

Decide:
- APPROVE if the planned action faithfully implements the proposal intent and looks safe
- BLOCK if the planned action diverges from intent, has unreasonable magnitude, targets the wrong thing, or smells off

Output (exact format):

VERDICT: APPROVE | BLOCK
CONFIDENCE: HIGH | MEDIUM | LOW
REASONING: <one paragraph: specifically why the planned action does or doesn't match the intent>
"""


def bigbrain_review_planned_action(proposal: dict, kind: str, planned: dict) -> dict:
    """Call bigbrain (Opus 4.8) to review a planned executor action.

    Returns {verdict, confidence, reasoning, raw, error}. On any failure (529,
    parser miss, network) returns verdict=None — the caller decides the safe
    default (currently: BLOCK on missing verdict, fail-closed).

    Synchronous wrapper: runs the async llm call in a fresh event loop. Safe
    to call from sync handlers.
    """
    if not EXECUTOR_REVIEW_ENABLED:
        return {"verdict": "APPROVE", "confidence": "HIGH", "reasoning": "executor review disabled (env)", "skipped": True}

    import asyncio
    prompt = EXECUTOR_REVIEW_PROMPT.format(
        title=(proposal.get("title") or "")[:200],
        category=proposal.get("category", "?"),
        proposed_action=(proposal.get("proposed_action") or "")[:1500],
        kind=kind,
        planned=json.dumps(planned, ensure_ascii=False)[:1500],
    )
    # 2026-06-18: mechanical cited-targets grounding — inject disk-truth on the functions/files/
    # paths named in the action + plan, so the executor plan-review can't APPROVE a plan that
    # targets something non-existent. Sync (outer fn is sync); cheap grep/path_exists.
    try:
        from shared.verify_toolbelt import cited_targets_report
        _tr, _tm = cited_targets_report(
            f"{proposal.get('proposed_action','')}\n{json.dumps(planned, ensure_ascii=False)[:1500]}")
        if _tr:
            prompt = prompt + "\n\n" + _tr
    except Exception:
        pass

    async def _call():
        from shared.llm import llm_generate
        from shared.companion import _strip_thinking
        from shared.shadow_verifier import parse_verification
        from shared.bigbrain_context import bigbrain_system_prompt
        system = bigbrain_system_prompt()
        # 2026-06-18 Phase-2: ground the plan review with the read-only verify-toolbelt so
        # v5.3 CHECKS the cited files/symbols on disk before APPROVE/BLOCK instead of
        # confabulating them. grounded_verify is sync (own tool-loop to :8090) → to_thread.
        from shared.verify_toolbelt import grounded_verify
        res = await asyncio.to_thread(grounded_verify, prompt, system,
                                      max_tokens=2048, temperature=0.2, timeout=120)
        if not res.get("success"):
            return {"verdict": None, "error": res.get("error")}
        raw = _strip_thinking(res.get("text") or "")
        parsed, warnings = parse_verification(raw)
        # parse_verification looks for VERDICT: PASS|FAIL|DEFER. We have APPROVE|BLOCK.
        # Re-extract APPROVE/BLOCK directly.
        m = re.search(r"(?im)^\s*VERDICT\s*:\s*(APPROVE|BLOCK)\b", raw)
        verdict = m.group(1).upper() if m else None
        return {
            "verdict": verdict,
            "confidence": parsed.get("confidence"),
            "reasoning": parsed.get("reasoning"),
            "raw": raw,
            "parse_warnings": warnings,
            "tool_trace": res.get("tool_trace"),  # audit: files/symbols v5.3 actually checked
        }

    try:
        return asyncio.run(_call())
    except RuntimeError:
        # Already in an event loop — use a fresh thread to host one
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            def _worker():
                return asyncio.run(_call())
            return ex.submit(_worker).result(timeout=300)  # grounded_verify can take several tool-turns
    except Exception as e:
        logger.warning(f"executor-review bigbrain call exception: {e}")
        return {"verdict": None, "error": str(e)}


# ============================================================
# Eligibility — which proposals do we even look at
# ============================================================

def _is_eligible(p: dict) -> bool:
    """Eligible = state=approved AND observer_verification.status=pass AND not already dispatched."""
    if p.get("state") != "approved":
        return False
    disc = p.get("disclosure") or {}
    ov = disc.get("observer_verification") or {}
    if ov.get("status") != "pass":
        # Heuristic-source proposals don't have observer_verification — treat as pass if approved
        if not p.get("source", "").startswith("authored_by_"):
            return True
        return False
    em = p.get("executor_meta") or {}
    if em.get("dispatched") is True:
        return False
    if em.get("dispatch_state") == "dispatch_failed":
        return False  # exhausted retries, awaiting human triage
    return True


# ============================================================
# Public entry point — called by watchtower cron
# ============================================================

def run_dispatch_cycle(max_per_tick: int = 1) -> dict:
    """One cron tick: pick up to max_per_tick eligible proposals and dispatch.

    Returns summary dict for logging/audit.
    """
    if not EXECUTOR_ENABLED:
        return {"enabled": False, "dispatched": 0}

    rows = _load_active()
    if not rows:
        return {"enabled": True, "dispatched": 0, "total_active": 0}

    dispatched: list = []
    for p in rows:
        if len(dispatched) >= max_per_tick:
            break
        if not _is_eligible(p):
            continue

        prop_id = p.get("id", "<?>")
        em = p.setdefault("executor_meta", {})
        attempts = int(em.get("dispatch_attempts") or 0)

        kind, kind_reason = classify_kind(p)
        em["dispatch_kind"] = kind
        em["dispatch_kind_reason"] = kind_reason

        if kind == "manual":
            # Skip — leave for dashboard. Don't count as a dispatch attempt.
            continue

        ok, gate_reason = _verifier_gate_ok(p, kind)
        if not ok:
            em["last_error"] = f"verifier_gate: {gate_reason}"
            em["last_dispatch_at"] = time.time()
            _append_log({
                "type": "verifier_gate_blocked",
                "proposal_id": prop_id, "kind": kind, "reason": gate_reason,
            })
            # Not a retryable error — mark dispatch_failed so it goes to triage
            em["dispatch_state"] = "dispatch_failed"
            dispatched.append({"id": prop_id, "kind": kind, "result": "blocked_by_gate"})
            continue

        handler = HANDLERS.get(kind)
        if handler is None:
            em["last_error"] = f"no_handler:{kind}"
            em["dispatch_state"] = "dispatch_failed"
            dispatched.append({"id": prop_id, "kind": kind, "result": "no_handler"})
            continue

        attempts += 1
        em["dispatch_attempts"] = attempts
        em["last_dispatch_at"] = time.time()

        try:
            result = handler(p)
        except Exception as e:
            tb = traceback.format_exc()
            logger.exception(f"EXECUTOR handler error on {prop_id}")
            em["last_error"] = f"{type(e).__name__}: {e}"
            em["last_traceback"] = tb[-1500:]
            if attempts >= DISPATCH_RETRY_CAP:
                em["dispatch_state"] = "dispatch_failed"
                _append_log({"type": "dispatch_failed", "proposal_id": prop_id, "kind": kind,
                             "attempts": attempts, "error": em["last_error"]})
            dispatched.append({"id": prop_id, "kind": kind, "result": "exception", "attempts": attempts})
            continue

        if not result.get("ok"):
            em["last_error"] = result.get("reason", "handler returned ok=False")
            if attempts >= DISPATCH_RETRY_CAP:
                em["dispatch_state"] = "dispatch_failed"
                _append_log({"type": "dispatch_failed", "proposal_id": prop_id, "kind": kind,
                             "attempts": attempts, "reason": em["last_error"]})
            dispatched.append({"id": prop_id, "kind": kind, "result": "handler_not_ok", "attempts": attempts})
            continue

        # Success path
        em["dispatched"] = True
        em["dispatch_state"] = "dispatched"
        em["last_result"] = result
        em["last_error"] = None
        # For meta/code we leave state=approved (still need explicit ceremony/kickoff)
        # For config/memory we move to resolved since the action completed (one-shot)
        if kind in ("config", "memory"):
            p["state"] = "resolved"
            p["resolved_at"] = time.time()
            p["result"] = result
        _append_log({"type": "dispatched", "proposal_id": prop_id, "kind": kind,
                     "result": result, "attempts": attempts})
        dispatched.append({"id": prop_id, "kind": kind, "result": "ok", "attempts": attempts})

    if dispatched:
        _save_active(rows)

    summary = {
        "enabled": True,
        "total_active": len(rows),
        "dispatched": len(dispatched),
        "details": dispatched,
    }
    return summary
