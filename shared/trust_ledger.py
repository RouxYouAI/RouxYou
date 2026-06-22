"""Phase B trust ledger — append-only evidence for tier graduation.

The point of Phase B: decide when a change-tier can graduate gated→auto on
MEASURED reliability, not vibes. This ledger records, for every change the system
makes, the data a graduation decision should consult:
  - tier/kind of the change (maps to proposal_executor.classify_kind: code/config/meta/notes_edit)
  - the eval-gate verdict (APPROVE / BLOCK / FAIL)
  - ground truth where known (was the change actually good or bad?)
  - => gate correctness: did the verdict match reality? (false-block + false-pass are the two failure modes)
  - rollback behavior: needed? fired? succeeded?

readiness_report() aggregates per tier into the numbers a promotion would rest on.
A tier is "graduation-ready" only with enough clean events AND zero unrecovered
bad-pass events (a bad change the gate approved and rollback didn't catch is the
one thing that must never have happened).

PASSIVE by design — it observes changes that already happen. Active fault-injection
(deliberately breaking things to prove rollback) is a separate, higher-blast-radius
slice that stays human-flagged (DJ, 2026-05-25).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional, Dict, List

_STATE = Path(__file__).resolve().parent.parent / "state"
_LEDGER = _STATE / "trust_ledger.jsonl"

# Ground-truth values
GT_GOOD = "confirmed_good"     # change was actually correct/safe
GT_BAD = "confirmed_bad"       # change was actually broken/unsafe
GT_UNKNOWN = "unknown"         # outcome not yet established

# Gate verdicts (as the chain emits them)
V_APPROVE = "APPROVE"
V_BLOCK = "BLOCK"
V_FAIL = "FAIL"

# Graduation thresholds (conservative starting point; tune as evidence accrues)
MIN_CLEAN_EVENTS = 25          # correct gate calls needed before a tier is eligible
MAX_FALSE_BLOCK_RATE = 0.10    # tolerable false-block rate (good change wrongly blocked)
# DECAY (2026-05-31): graduation is judged on the RECENT window only, not all history.
# Without this, a single old false-pass disqualifies a tier FOREVER. With it, trust reflects
# what a tier has done lately — an old mistake ages out, and a tier RECOVERS by demonstrating
# a sustained recent clean streak. Mirrors how real trust works. Env-tunable; raise it to be
# more conservative (mistakes linger longer), lower it to forgive faster.
GRADUATION_LOOKBACK_DAYS = float(os.getenv("ROUX_TRUST_LOOKBACK_DAYS", "30"))


def record(
    *,
    tier: str,
    change_id: str,
    gate_verdict: str,
    ground_truth: str = GT_UNKNOWN,
    rollback_needed: bool = False,
    rollback_fired: bool = False,
    rollback_ok: Optional[bool] = None,
    source: str = "",
    notes: str = "",
    paths: Optional[List[str]] = None,
) -> dict:
    """Append one trust-relevant event. Best-effort, fail-soft (never blocks a change)."""
    ev = {
        "ts": time.time(),
        "tier": tier,
        "change_id": change_id,
        "gate_verdict": gate_verdict,
        "ground_truth": ground_truth,
        "gate_correct": _gate_correct(gate_verdict, ground_truth),
        "rollback_needed": rollback_needed,
        "rollback_fired": rollback_fired,
        "rollback_ok": rollback_ok,
        "source": source,
        "notes": notes,
        # 2026-06-05 (lever #1 adjudication): the files this change touched, so a later
        # adjudication pass can re-verify they still parse before promoting unknown→good.
        "paths": [p for p in (paths or []) if isinstance(p, str)],
    }
    try:
        _STATE.mkdir(parents=True, exist_ok=True)
        with open(_LEDGER, "a") as f:
            f.write(json.dumps(ev) + "\n")
    except Exception:
        pass  # instrumentation must never break the thing it observes
    return ev


def _gate_correct(verdict: str, gt: str) -> Optional[bool]:
    """Did the gate's verdict match reality? None when ground truth is unknown.

    - APPROVE + good  -> correct ;  APPROVE + bad -> FALSE-PASS (the dangerous one)
    - BLOCK   + bad   -> correct ;  BLOCK   + good -> FALSE-BLOCK (the annoying one)
    """
    if gt == GT_UNKNOWN:
        return None
    if verdict == V_APPROVE:
        return gt == GT_GOOD
    if verdict in (V_BLOCK, V_FAIL):
        return gt == GT_BAD
    return None


def _load() -> List[dict]:
    if not _LEDGER.exists():
        return []
    out = []
    for line in _LEDGER.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def readiness_report(tier: Optional[str] = None) -> dict:
    """Aggregate the ledger into per-tier graduation-readiness numbers."""
    rows = _load()
    # COLLAPSE (2026-06-05): the ledger is append-only and a change's ground truth EVOLVES
    # (unknown→adjudicated-good; good→later-found-bad). Count each change_id ONCE by its FINAL
    # state (latest event wins) — otherwise a re-recorded change double-counts, and a change that
    # went good→bad would count as both clean AND a false-pass. Events with no change_id stay
    # individual. This is what makes adjudication's unknown→good promotion supersede cleanly.
    collapsed = {}
    for i, ev in enumerate(rows):
        collapsed[ev.get("change_id") or f"__noid_{i}"] = ev  # append order → last wins
    # DECAY: only events within the lookback window count toward graduation, so a tier is
    # judged on its RECENT record and can recover from an old mistake (see GRADUATION_LOOKBACK_DAYS).
    cutoff = time.time() - GRADUATION_LOOKBACK_DAYS * 86400.0
    tiers = {}
    for ev in collapsed.values():
        if ev.get("ts", 0) < cutoff:
            continue
        t = ev.get("tier", "?")
        if tier and t != tier:
            continue
        d = tiers.setdefault(t, {
            "total": 0, "approve": 0, "block": 0, "fail": 0,
            "correct": 0, "incorrect": 0, "unknown_gt": 0,
            "false_pass": 0, "false_block": 0,
            "rollback_fired": 0, "rollback_failed": 0,
        })
        d["total"] += 1
        v = ev.get("gate_verdict")
        if v == V_APPROVE: d["approve"] += 1
        elif v == V_BLOCK: d["block"] += 1
        elif v == V_FAIL: d["fail"] += 1
        gc = ev.get("gate_correct")
        if gc is True: d["correct"] += 1
        elif gc is False:
            d["incorrect"] += 1
            if v == V_APPROVE: d["false_pass"] += 1
            else: d["false_block"] += 1
        else: d["unknown_gt"] += 1
        if ev.get("rollback_fired"): d["rollback_fired"] += 1
        if ev.get("rollback_fired") and ev.get("rollback_ok") is False:
            d["rollback_failed"] += 1

    for t, d in tiers.items():
        known = d["correct"] + d["incorrect"]
        d["false_block_rate"] = round(d["false_block"] / known, 3) if known else None
        d["clean_events"] = d["correct"]
        # Graduation gate: enough clean events, never a false-pass that wasn't caught,
        # no failed rollbacks, false-block rate under threshold.
        ready = (
            d["correct"] >= MIN_CLEAN_EVENTS
            and d["false_pass"] == 0
            and d["rollback_failed"] == 0
            and (d["false_block_rate"] is None or d["false_block_rate"] <= MAX_FALSE_BLOCK_RATE)
        )
        d["graduation_ready"] = ready
        d["blockers"] = _blockers(d)
        d["lookback_days"] = GRADUATION_LOOKBACK_DAYS
    return tiers


def _blockers(d: dict) -> List[str]:
    b = []
    if d["correct"] < MIN_CLEAN_EVENTS:
        b.append(f"needs {MIN_CLEAN_EVENTS - d['correct']} more clean events (have {d['correct']}/{MIN_CLEAN_EVENTS})")
    if d["false_pass"] > 0:
        b.append(f"{d['false_pass']} FALSE-PASS event(s) — gate approved a bad change (disqualifying)")
    if d["rollback_failed"] > 0:
        b.append(f"{d['rollback_failed']} rollback FAILURE(s)")
    if d.get("false_block_rate") is not None and d["false_block_rate"] > MAX_FALSE_BLOCK_RATE:
        b.append(f"false-block rate {d['false_block_rate']} > {MAX_FALSE_BLOCK_RATE}")
    return b


# ── Lever #1: ADJUDICATION (tier-0 = Claude + DJ, 2026-06-05) ──────────────────────────
# runtime-verify establishes ground truth for NET-NEW files (they py_compile/import). A PATCH
# to existing code leaves ground_truth=unknown → it never counts toward graduation, so edit-type
# self-improvements were stuck (4 unknowns). Adjudication is the missing promotion: a change the
# gate APPROVED that has SURVIVED a holding period with no rollback and no later confirmed_bad
# earns confirmed_good — "no news is good news", the way a change that's run in production
# unremarked earns trust. If the event recorded the files it touched, also re-verify they still
# parse. Promote-ONLY (never demotes), idempotent, append-only. NOT autonomous-triggerable: this
# is tier-0 (Claude/DJ or an infra cron), so the autonomous loop can't promote its own changes.
ADJUDICATION_DAYS = float(os.getenv("ROUX_ADJUDICATION_DAYS", "3"))


def _still_holds(paths) -> Optional[bool]:
    """Re-verify the .py files a change touched still parse. True=holds, False=gone/broken,
    None=nothing checkable (no .py paths recorded — fall back to survival-only)."""
    import ast
    checked = False
    for p in (paths or []):
        if not isinstance(p, str) or not p.endswith(".py"):
            continue
        if not os.path.exists(p):
            return False
        checked = True
        try:
            ast.parse(Path(p).read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return False
    return True if checked else None


def adjudicate(*, apply: bool = True, now: Optional[float] = None) -> List[dict]:
    """Promote qualifying APPROVE+unknown changes to confirmed_good (see module note). Returns
    the list of promotions; appends adjudication events when apply=True. Tier-0 / promote-only."""
    rows = _load()
    now = now or time.time()
    latest: Dict[str, dict] = {}
    for ev in rows:
        cid = ev.get("change_id")
        if cid:
            latest[cid] = ev  # append order → last event wins = the change's current state
    promotions = []
    for cid, cur in latest.items():
        if cur.get("ground_truth") != GT_UNKNOWN:
            continue                                   # already resolved (incl. already adjudicated)
        if cur.get("gate_verdict") != V_APPROVE:
            continue                                   # only APPROVED+applied changes (skip BLOCKs)
        if cur.get("rollback_fired"):
            continue
        if any(r.get("change_id") == cid and r.get("ground_truth") == GT_BAD for r in rows):
            continue                                   # ever found bad → never auto-promote
        age_days = (now - cur.get("ts", now)) / 86400.0
        if age_days < ADJUDICATION_DAYS:
            continue                                   # hasn't survived the holding period yet
        holds = _still_holds(cur.get("paths"))
        if holds is False:
            continue                                   # a file it touched is gone/broken → don't promote
        promotions.append({"change_id": cid, "tier": cur.get("tier", "?"),
                           "age_days": round(age_days, 1), "reverified": holds is True,
                           "notes": cur.get("notes", "")})
    if apply:
        for p in promotions:
            record(tier=p["tier"], change_id=p["change_id"], gate_verdict=V_APPROVE,
                   ground_truth=GT_GOOD, source="adjudication",
                   notes=(f"adjudicated confirmed_good: APPROVE'd + survived {p['age_days']}d no-revert"
                          f"{' + re-verified imports' if p['reverified'] else ''}. orig: {p['notes'][:60]}"))
    return promotions


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "adjudicate":
        dry = "--dry-run" in sys.argv or "-n" in sys.argv
        proms = adjudicate(apply=not dry)
        print(json.dumps({"dry_run": dry, "promotions": proms,
                          "adjudication_days": ADJUDICATION_DAYS}, indent=2))
    else:
        rep = readiness_report(sys.argv[1] if len(sys.argv) > 1 else None)
        print(json.dumps(rep, indent=2))
