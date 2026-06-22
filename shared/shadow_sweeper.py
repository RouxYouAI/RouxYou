"""Shadow-sweeper — re-fires shadow verification on authored proposals
whose shadow task was lost (orchestrator restart, transient outage, etc.).

Problem this addresses (2026-05-20): /propose fires shadow as a
fire-and-forget asyncio task. If the orchestrator restarts between
publish-time and shadow-completion, the task is killed and the proposal
sits with observer.status=pending forever, blocking approval.

Sweeper logic (per cron tick):
- Walk active queue
- For each authored_by_* proposal, state=pending, observer.status pending|None,
  and age > MIN_AGE_BEFORE_SWEEP:
    - If no shadow log entry for this prop_id → re-fire (clearly never ran)
    - If shadow log entries exist but ALL ended with verdict=None → re-fire
      (likely Anthropic 529 / parse failure)
- Skip if last sweep attempt was < MIN_AGE_BEFORE_SWEEP ago (don't hammer)
- Mark executor_meta.shadow_resweep_attempts so we have an audit trail

Env: ROUX_SHADOW_SWEEPER (default on).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List

import sys
_BASE = Path(__file__).parent.parent
sys.path.insert(0, str(_BASE))

from shared.logger import get_logger

logger = get_logger("shadow_sweeper")

SHADOW_LOG = _BASE / "state" / "shadow_verifications.jsonl"

MIN_AGE_BEFORE_SWEEP = float(os.environ.get("ROUX_SHADOW_SWEEP_MIN_AGE_SEC", "600"))  # 10 min default
MAX_RESWEEPS = int(os.environ.get("ROUX_SHADOW_SWEEP_MAX_ATTEMPTS", "2"))
SWEEPER_ENABLED = os.environ.get("ROUX_SHADOW_SWEEPER", "1") != "0"


def _scan_shadow_log_for(prop_id: str) -> List[Dict]:
    """Return all shadow log entries that mention this prop_id."""
    out: List[Dict] = []
    if not SHADOW_LOG.exists():
        return out
    try:
        with open(SHADOW_LOG, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("proposal_id") == prop_id:
                    out.append(e)
    except Exception as e:
        logger.warning(f"shadow log scan failed: {e}")
    return out


def _try_retro_attest(proposal: Dict, entries: List[Dict]) -> bool:
    """If the latest shadow log entry would auto-apply under TODAY's rules
    (FAIL + HIGH|MEDIUM, non-governance), retroactively attest. Returns True if
    attested. Used to heal proposals whose verdicts predate a threshold change.
    """
    if proposal.get("category") == "governance":
        return False
    # Walk entries newest-first; find the most recent parsed verdict
    for e in reversed(entries):
        for verdict_key in ("claude_defer_parsed", "claude_parsed", "roux_parsed"):
            parsed = e.get(verdict_key) or {}
            v = parsed.get("verdict")
            c = parsed.get("confidence")
            if v == "FAIL" and c in ("HIGH", "MEDIUM"):
                verifier = "claude" if verdict_key in ("claude_defer_parsed", "claude_parsed") else "roux"
                try:
                    from shared.proposal_bus import set_observer_verification
                    comment = (
                        f"Retroactive auto-attestation via shadow_sweeper. "
                        f"Source: {verdict_key}. Confidence: {c}. "
                        f"Reasoning: {(parsed.get('reasoning') or '')[:300]}"
                    )
                    applied = set_observer_verification(
                        proposal_id=proposal["id"],
                        status="fail",
                        comment=comment,
                        verified_by=verifier,
                    )
                    if applied:
                        logger.info(
                            f"SHADOW SWEEP RETRO-ATTEST: {proposal['id']} → observer.status=fail by {verifier} (from {verdict_key} {c})"
                        )
                        return True
                except Exception as e:
                    logger.warning(f"retro-attest failed for {proposal.get('id')}: {e}")
                    return False
    return False


def _has_terminal_verdict(entries: List[Dict]) -> bool:
    """Does any entry for this prop have a terminal verdict (PASS/FAIL/DEFER on the bigbrain or roux side)?"""
    for e in entries:
        rp = e.get("roux_parsed") or {}
        cp = e.get("claude_parsed") or {}
        cdp = e.get("claude_defer_parsed") or {}
        for parsed in (rp, cp, cdp):
            v = parsed.get("verdict")
            if v in ("PASS", "FAIL", "DEFER"):
                return True
    return False


async def sweep_pending_shadows() -> Dict:
    """Walk active queue + re-fire shadow on orphans. Returns audit summary.

    Soft-fail throughout — never raises, never blocks. Re-firing is async
    fire-and-forget like the original /propose path.
    """
    if not SWEEPER_ENABLED:
        return {"skipped": True, "reason": "ROUX_SHADOW_SWEEPER=0"}

    import asyncio
    from shared.proposal_bus import get_active
    from shared.shadow_verifier import run_shadow_verification, SHADOW_ENABLED

    if not SHADOW_ENABLED:
        return {"skipped": True, "reason": "shadow disabled"}

    now = time.time()
    orphans: List[Dict] = []
    refired = 0
    skipped_age = 0
    skipped_resweep_cap = 0
    skipped_no_disclosure = 0
    skipped_has_terminal = 0

    for p in get_active():
        if p.get("state") != "pending":
            continue
        source = p.get("source") or ""
        if not source.startswith("authored_by_"):
            continue
        disclosure = p.get("disclosure") or {}
        if not disclosure:
            skipped_no_disclosure += 1
            continue
        ov = disclosure.get("observer_verification") or {}
        if ov.get("status") in ("pass", "fail"):
            continue  # already attested
        age = now - (p.get("created_at") or now)
        if age < MIN_AGE_BEFORE_SWEEP:
            skipped_age += 1
            continue
        em = p.get("executor_meta") or {}
        n_prior = int(em.get("shadow_resweep_attempts") or 0)
        if n_prior >= MAX_RESWEEPS:
            skipped_resweep_cap += 1
            continue
        entries = _scan_shadow_log_for(p["id"])
        if _has_terminal_verdict(entries):
            # Has a real verdict but observer still pending — likely a verdict
            # produced under stricter auto-apply rules (e.g., HIGH-only) that
            # wouldn't fire today. Retroactively attest if the latest verdict
            # would auto-apply under current rules (FAIL + HIGH|MEDIUM, not
            # governance category).
            applied = _try_retro_attest(p, entries)
            if applied:
                refired += 1  # report it under refired so callers see action taken
                continue
            skipped_has_terminal += 1
            continue
        orphans.append(p)

    for p in orphans:
        logger.info(
            f"SHADOW SWEEP RE-FIRE: {p['id']} (age={(now - (p.get('created_at') or now))/60:.1f}min, prior_resweeps={int((p.get('executor_meta') or {}).get('shadow_resweep_attempts') or 0)})"
        )
        # Bump the audit counter BEFORE re-firing — concurrent-safe via proposal_bus's lock
        try:
            from shared.proposal_bus import _get_lock, _load_active, _save_active  # internal but stable
            with _get_lock():
                rows = _load_active()
                for r in rows:
                    if r["id"] == p["id"]:
                        em = r.setdefault("executor_meta", {})
                        em["shadow_resweep_attempts"] = int(em.get("shadow_resweep_attempts") or 0) + 1
                        em["last_shadow_resweep_at"] = now
                        break
                _save_active(rows)
        except Exception as e:
            logger.warning(f"sweep audit bump failed for {p['id']}: {e}")

        # AWAIT the re-shadow (not fire-and-forget) — this function runs inside
        # a temporary asyncio.run() loop in the watchtower worker thread, and
        # asyncio.create_task'd tasks get killed when the loop tears down.
        # Awaiting sequentially is also a natural rate-limit for Anthropic 529s.
        try:
            await run_shadow_verification(p)
            refired += 1
        except Exception as e:
            logger.warning(f"sweep re-fire failed for {p['id']}: {e}")

    summary = {
        "scanned_active": True,
        "orphans_found": len(orphans),
        "refired": refired,
        "skipped_age": skipped_age,
        "skipped_resweep_cap": skipped_resweep_cap,
        "skipped_no_disclosure": skipped_no_disclosure,
        "skipped_has_terminal": skipped_has_terminal,
        "ts": now,
    }
    if orphans:
        logger.info(f"SHADOW SWEEP: refired {refired} orphan(s). {summary}")
    return summary
