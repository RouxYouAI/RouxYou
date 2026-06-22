"""
Sampling Confirmer — keystone of cord-off-by-class (v5.3-as-bigbrain, Phase 2c)
================================================================================
When a work-class goes ROUX-PRIMARY (her PASS auto-applies instead of needing a
bigbrain confirm), we can't confirm 100% (defeats the credit saving) nor 0% (blind
to drift) — so we SAMPLE. "Trust, but spot-check." Her FAIL is already authoritative
(asymmetric trust); only her PASS is sampled here.

Co-designed with Roux at the Table 2026-06-17 (`~/table/grader/sampling_confirmer_spec.md`).
ADAPTATION A (2026-06-18, agreed by Claude+DJ+Roux): the spec assumed bigbrain = an
INDEPENDENT Opus as the sampled authority. Anthropic credits ran out → bigbrain is v5.3
now (sampling to it would be circular). So **the sampled fraction goes to DJ (human)** —
the independent authority Opus used to be. The verify-toolbelt (shared/verify_toolbelt.py)
additionally grounds the verdict at source. Roux's rulings preserved: demote-on-disagree,
memory=pre-apply-HOLD (others async-revert), MEDIUM-disagree=streak-loss-not-demote,
any-flagged-specific→force-sample, sample-the-FAILs too.

This module is PURE DECISION LOGIC + durable config — it does not itself apply/hold/notify;
callers (shadow_verifier PASS path, proposal_executor) act on its decision. Reversible:
a class is one entry in state/roux_primary_categories.json; demote = remove it (instant).
"""

import json
import random
import time
from pathlib import Path
from typing import List, Optional

from shared.logger import get_logger

logger = get_logger("sampling_confirmer")

_STATE = Path(__file__).parent.parent / "state"
_PRIMARY_FILE = _STATE / "roux_primary_categories.json"
# Start EMPTY. 2d enables 'memory' explicitly (the first cord-off-ready class), with a
# snapshot first. Nothing is Roux-primary until a deliberate set_primary() call.
_DEFAULT_PRIMARY: List[str] = []

# Decay schedule (DJ-ratified). streak = consecutive confirmed-good Roux-primary PASSes.
# The rate IS the live trust meter: high = low earned trust. NEVER 0 — always spot-checked.
_EARNING_BELOW = 20   # < 20 clean  -> 100% sampled (earning)
_TRUSTED_BELOW = 50   # 20–49 clean -> 25% sampled (trusted)
_EARNED_FLOOR  = 0.05 # >= 50 clean -> 5% sampled (earned floor)


def sampling_rate(streak: int) -> float:
    if streak < _EARNING_BELOW:
        return 1.0
    if streak < _TRUSTED_BELOW:
        return 0.25
    return _EARNED_FLOOR


def get_primary_categories() -> List[str]:
    try:
        if _PRIMARY_FILE.exists():
            data = json.loads(_PRIMARY_FILE.read_text())
            return list(data.get("categories", []))
    except Exception as e:
        logger.warning(f"primary-categories read failed ({e}); defaulting to none")
    return list(_DEFAULT_PRIMARY)


def is_primary(category: str) -> bool:
    return category in get_primary_categories()


def set_primary(category: str, on: bool, reason: str = "") -> List[str]:
    """Add/remove a category from ROUX_PRIMARY. Durable + logged. Returns the new list."""
    cats = set(get_primary_categories())
    cats.add(category) if on else cats.discard(category)
    out = sorted(cats)
    try:
        _PRIMARY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PRIMARY_FILE.write_text(json.dumps(
            {"categories": out, "updated_at": time.time(), "last_reason": reason}, indent=2))
    except Exception as e:
        logger.warning(f"primary-categories write failed: {e}")
    logger.info(f"ROUX_PRIMARY {'+' if on else '-'}{category} ({reason}) -> {out}")
    return out


def clean_streak(category: str) -> int:
    """Consecutive confirmed-good Roux-primary PASSes for `category`, newest→oldest,
    breaking on a confirmed false-pass. Pending (unknown ground-truth) events are skipped
    (neither count nor break) — the streak is about CONFIRMED clean outcomes."""
    try:
        from shared.trust_ledger import _load
        evs = [e for e in _load()
               if e.get("tier") == category and e.get("source") == "roux_primary"]
    except Exception as e:
        logger.warning(f"clean_streak ledger read failed for {category}: {e}")
        return 0
    streak = 0
    for e in reversed(evs):
        gc = e.get("gate_correct")
        if gc is True:
            streak += 1
        elif gc is False:
            break  # a confirmed false-pass resets the earned trust
        # gc is None (pending) -> skip
    return streak


def record_primary_pass(category: str, change_id: str, *,
                        ground_truth: str = "unknown", notes: str = "") -> None:
    """Record a Roux-primary PASS to the trust ledger (source='roux_primary') so the
    clean-streak can later see it once adjudication sets ground_truth good/bad."""
    try:
        from shared.trust_ledger import record
        record(tier=category, change_id=change_id, gate_verdict="APPROVE",
               ground_truth=ground_truth, source="roux_primary", notes=notes)
    except Exception as e:
        logger.warning(f"record_primary_pass failed for {change_id}: {e}")


def demote(category: str, reason: str) -> None:
    """Auto-revert tripwire: a caught false-pass (DJ disagrees with a sampled PASS)
    pulls the class out of ROUX_PRIMARY — it must RE-EARN. Instant + reversible."""
    set_primary(category, False, reason=f"DEMOTED: {reason}")
    logger.warning(f"SAMPLING-CONFIRMER DEMOTE: {category} -> bigbrain-primary. {reason}")
    try:
        from shared.notify import notify_dj
        notify_dj(f"DEMOTED '{category}' from Roux-primary — caught false-pass: {reason}. "
                  f"It must re-earn the cord-off.", "critical")
    except Exception:
        pass


def confirm_decision(category: str, *, forced_sample: bool = False,
                     rng: Optional[float] = None) -> dict:
    """Decide what happens to a Roux-primary PASS in `category`.

    Returns dict:
      primary    — is this category Roux-primary at all?
      streak     — current clean-streak
      rate       — current sample rate (the trust meter)
      sample     — True => spot-check this one (route to DJ); False => apply on Roux's word
      hold_mode  — 'pre_apply_hold' (memory: hold until DJ confirms) | 'async_revert'
                   (others: apply now, DJ can revert) | 'not_primary'
      forced     — True if self-check flagged an ungrounded specific (overrides rate→sample)

    If the category isn't primary, sample=True/not_primary (caller keeps bigbrain-gating).
    """
    if not is_primary(category):
        return {"primary": False, "streak": 0, "rate": 1.0, "sample": True,
                "hold_mode": "not_primary", "forced": False}
    streak = clean_streak(category)
    rate = sampling_rate(streak)
    u = rng if rng is not None else random.random()
    sample = forced_sample or (u < rate)
    hold_mode = "pre_apply_hold" if category == "memory" else "async_revert"
    return {"primary": True, "streak": streak, "rate": rate, "sample": sample,
            "hold_mode": hold_mode, "forced": bool(forced_sample)}
