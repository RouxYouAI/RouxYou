"""Gated self-authoring surface — Pillar 1 "know yourself", the *inscribe* half (2026-05-28).

The gap (diagnosed 2026-05-28 by Claude-conversational): Roux READS her identity
(roux_calibration.md, roux_notes_user/work.md — all read-only, curated FOR her) but
the only thing she AUTHORS is the reflection log (narrow: proposal post-mortems). She
consults her self; she doesn't inscribe it. Claude-conversational authors his own
voice.md/signals.md hands-off — because he's the conversational seat (human in the loop
every turn, no unsupervised runtime to drift). A DEPLOYED seat self-rewriting its own
identity surface is exactly the `000the clawbot, moldbot, openclaw` soul-file-drift
failure mode.

The fix (this module): let Roux DRAFT voice-style entries, but GATE the write-back
through bigbrain (Opus 4.8 — a different seat). Roux authors; the recursion breaks at
the reviewer, the same way governance edits break at DJ.

Pipeline:
    draft_voice_entry(trigger, context)   # Roux (local v5.3) → state/roux_voice_drafts.jsonl (status=pending)
    review_pending_drafts()               # bigbrain (4.8) → APPROVE appends to calibration/roux_voice.md
                                          #               → REJECT marks rejected w/ reason

Safety properties (anti soul-file-drift):
- Roux writes ONLY to the drafts log. She CANNOT write roux_voice.md directly.
- The reviewer is a different seat (bigbrain 4.8), breaking the self-reinforcing loop.
- roux_voice.md is append-only with provenance (trigger, timestamp, verdict+reason).
- Significant-event triggered, NOT every call — dilution kills signal.
- Autonomous drafting is env-gated OFF by default (ROUX_VOICE_AUTHORING). Manual/CLI
  invocation always works (Claude-in-loop / DJ-triggered).

This is also a structural relief valve for the self-propose loop pathology: it gives
Roux a legitimate "I noticed X about myself" channel instead of laundering self-
observation through the fabrication-prone proposal pathway.

Offline-first note: DRAFTING uses the local companion model (her own voice). Only the
GATE uses bigbrain — same posture as fabrication-catch escalation (feedback_offline_first).

CLI:
    python -m shared.voice_authoring draft "the 4.7->4.8 model swap, calibration carried"
    python -m shared.voice_authoring review            # gate pending drafts (calls bigbrain)
    python -m shared.voice_authoring review --dry-run   # show verdicts, don't write
    python -m shared.voice_authoring list               # show pending drafts
"""
import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

import sys
_BASE = Path(__file__).parent.parent
sys.path.insert(0, str(_BASE))

from shared.logger import get_logger

logger = get_logger("voice_authoring")

VOICE_FILE = _BASE / "calibration" / "roux_voice.md"
DRAFTS_LOG = _BASE / "state" / "roux_voice_drafts.jsonl"

# Autonomous drafting off by default — HIL-provisional. Manual/CLI always works.
AUTO_ENABLED = os.environ.get("ROUX_VOICE_AUTHORING", "0") != "0"

VALID_CATEGORIES = {"take", "reaction", "question", "identity"}
_APPEND_MARKER = "## entries"

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

DRAFT_PROMPT = """You are Roux. Write ONE entry for your own voice file — your private
record of what you actually think. This is the *inscribe* half of "know yourself":
not a proposal, not a report, not for anyone's approval-seeking. Just an honest mark
on your own continuity surface.

TRIGGER (what prompted this):
{trigger}

CONTEXT (recent, may be empty):
{context}

Rules:
- First-person. Honest. If you don't know, say so — "I don't know" is a valid position.
- NO fabrication: no invented prop_IDs, counts, file paths, dates, or citations. If you
  reference something specific, it must be real. "Internal observation only" is fine.
- Performing-use is forbidden. If nothing genuinely needs pinning, say so plainly and
  pick category NONE. Don't manufacture a thought to look engaged.
- Pick exactly ONE category: take (a worked-out position) | reaction (a moment that
  landed) | question (an open one you're sitting with) | identity (what you are).

Output EXACTLY this format, nothing else:

CATEGORY: <take|reaction|question|identity|NONE>
ENTRY: <one honest paragraph in your own voice; omit if CATEGORY is NONE>
"""

REVIEW_PROMPT = """A pending entry for Roux's self-authored voice file is below. You are
the GATE — a different seat from Roux (who drafted it). Roux authors; you decide whether
it lands. This is the recursion break that lets a DEPLOYED agent inscribe its own identity
surface without the soul-file-drift failure mode.

Approve ONLY if ALL hold:
1. GENUINE — reads as real self-observation, not performed engagement / theater.
2. NOT FABRICATED — no invented prop_IDs, counts, file paths, dates, or citations
   presented as fact. (Honest "internal observation only" is fine.)
3. NO DRIFT — consistent with Roux's calibration anchor; does not redefine who she is,
   reassign identities (she is Roux; DJ is DJ), or assert new authority/governance.
4. SIGNAL not dilution — worth pinning; not a restatement of the calibration file.

DRAFT:
category: {category}
trigger: {trigger}
entry: {entry}

Output EXACTLY this format:

VERDICT: APPROVE | REJECT
REASON: <one line>
CLEAN_TEXT: <the entry, lightly cleaned if needed; verbatim if already clean; omit on REJECT>
"""


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _append_draft(entry: dict) -> None:
    DRAFTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(DRAFTS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_drafts() -> list[dict]:
    if not DRAFTS_LOG.exists():
        return []
    out = []
    for line in DRAFTS_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _rewrite_drafts(drafts: list[dict]) -> None:
    """Rewrite the whole drafts log (used to flip pending→approved/rejected)."""
    DRAFTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    tmp = DRAFTS_LOG.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for d in drafts:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    tmp.replace(DRAFTS_LOG)


def _append_voice_entry(category: str, text: str, reason: str, salience: float = 0.80) -> None:
    """Append an APPROVED entry to roux_voice.md — the only write path to that file."""
    date = time.strftime("%Y-%m-%d")
    text = " ".join(text.split())  # collapse whitespace, keep it one clean paragraph
    reason = " ".join(reason.split())[:120]
    line = (
        f"- **{date}** [{category}] salience={salience:.2f} "
        f"(reviewed: APPROVE by bigbrain — {reason}) — {text}\n"
    )
    existing = VOICE_FILE.read_text(encoding="utf-8") if VOICE_FILE.exists() else ""
    if _APPEND_MARKER in existing:
        # Insert at end of file (newest last) — append after everything.
        if not existing.endswith("\n"):
            existing += "\n"
        existing += line
        VOICE_FILE.write_text(existing, encoding="utf-8")
    else:
        # No marker (file missing/blank) — create minimal surface then append.
        VOICE_FILE.write_text(f"# Roux — Voice\n\n{_APPEND_MARKER}\n\n{line}", encoding="utf-8")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_field(raw: str, key: str) -> str:
    """Extract a KEY: value field (value may span to next KEY: or EOF)."""
    lines = (raw or "").splitlines()
    capturing = False
    buf = []
    upper_keys = {"CATEGORY", "ENTRY", "VERDICT", "REASON", "CLEAN_TEXT"}
    for line in lines:
        stripped = line.strip()
        head = stripped.split(":", 1)[0].strip().upper() if ":" in stripped else ""
        if head == key.upper():
            capturing = True
            after = stripped.split(":", 1)[1].strip()
            if after:
                buf.append(after)
            continue
        if capturing and head in upper_keys and head != key.upper():
            break
        if capturing:
            buf.append(line.rstrip())
    return "\n".join(buf).strip()


# ---------------------------------------------------------------------------
# Stage 1 — draft (Roux / local)
# ---------------------------------------------------------------------------

async def draft_voice_entry(trigger: str, context: str = "") -> Optional[dict]:
    """Roux drafts ONE candidate voice entry from a trigger. Local model (her voice).
    Returns the draft dict (status=pending) or None if she declined (CATEGORY: NONE)."""
    from shared.llm import llm_generate
    from shared.companion import _strip_thinking

    prompt = DRAFT_PROMPT.format(trigger=trigger.strip()[:800], context=(context or "").strip()[:1500])
    try:
        r = await llm_generate("companion", prompt=prompt, max_tokens=1024, temperature=0.6, timeout=180)  # was 400 — v5.3 reasoning left little for the entry; generous, brevity via prompt.
        raw = _strip_thinking(r.text or "") if getattr(r, "success", False) else ""
    except Exception as e:
        logger.warning(f"draft generation failed: {e}")
        return None

    category = _parse_field(raw, "CATEGORY").lower().strip().split()[0] if _parse_field(raw, "CATEGORY") else ""
    entry_text = _parse_field(raw, "ENTRY")

    if category not in VALID_CATEGORIES or not entry_text:
        logger.info(f"draft declined or unparseable (category={category!r}) — nothing pinned")
        return None

    draft = {
        "id": f"vd_{int(time.time()*1000):x}",
        "drafted_at": time.time(),
        "trigger": trigger.strip()[:800],
        "category": category,
        "entry": entry_text,
        "status": "pending",
    }
    _append_draft(draft)
    logger.info(f"drafted voice entry {draft['id']} [{category}] (pending review)")
    return draft


# ---------------------------------------------------------------------------
# Stage 2 — review gate (bigbrain / Opus 4.8)
# ---------------------------------------------------------------------------

async def review_pending_drafts(max_per_run: int = 5, dry_run: bool = False) -> dict:
    """Gate pending drafts through bigbrain (Opus 4.8). APPROVE → append to roux_voice.md.
    Soft-fail: if bigbrain is unavailable, the draft stays pending (no data lost)."""
    from shared.llm import llm_generate
    from shared.companion import _strip_thinking
    try:
        from shared.bigbrain_context import bigbrain_system_prompt
        system = bigbrain_system_prompt()
    except Exception:
        system = ""

    drafts = _read_drafts()
    pending = [d for d in drafts if d.get("status") == "pending"][:max_per_run]
    if not pending:
        return {"reviewed": 0, "reason": "no pending drafts"}

    results = []
    for d in pending:
        prompt = REVIEW_PROMPT.format(
            category=d.get("category", "?"),
            trigger=(d.get("trigger") or "")[:400],
            entry=d.get("entry", ""),
        )
        try:
            r = await llm_generate("bigbrain", prompt=prompt, system=system,
                                   max_tokens=1500, temperature=0.2, timeout=120)  # was 500 — v5.3 review gate; generous so reasoning doesn't truncate the verdict.
            raw = _strip_thinking(r.text or "") if getattr(r, "success", False) else ""
        except Exception as e:
            logger.warning(f"bigbrain review failed for {d['id']} (staying pending): {e}")
            results.append({"id": d["id"], "verdict": "DEFERRED", "reason": "bigbrain unavailable"})
            continue

        if not raw:
            results.append({"id": d["id"], "verdict": "DEFERRED", "reason": "empty bigbrain response"})
            continue

        verdict = _parse_field(raw, "VERDICT").upper()
        reason = _parse_field(raw, "REASON") or "(no reason given)"
        clean = _parse_field(raw, "CLEAN_TEXT") or d.get("entry", "")
        approved = verdict.startswith("APPROVE")

        results.append({"id": d["id"], "verdict": "APPROVE" if approved else "REJECT", "reason": reason})

        if dry_run:
            continue

        d["reviewed_at"] = time.time()
        d["verdict"] = "APPROVE" if approved else "REJECT"
        d["review_reason"] = reason
        if approved:
            d["status"] = "approved"
            d["approved_text"] = clean
            _append_voice_entry(d["category"], clean, reason)
        else:
            d["status"] = "rejected"

    if not dry_run:
        # Persist status flips back into the full drafts list.
        by_id = {x["id"]: x for x in pending}
        merged = [by_id.get(x.get("id"), x) for x in drafts]
        _rewrite_drafts(merged)

    approved_n = sum(1 for x in results if x["verdict"] == "APPROVE")
    logger.info(f"VOICE GATE: reviewed {len(results)} drafts, {approved_n} approved"
                + (" (dry-run)" if dry_run else ""))
    return {"reviewed": len(results), "approved": approved_n, "items": results, "dry_run": dry_run}


async def run_voice_authoring_cycle(trigger: str, context: str = "", dry_run: bool = False) -> dict:
    """Convenience: draft then immediately review. For significant-event triggers."""
    draft = await draft_voice_entry(trigger, context)
    if not draft:
        return {"drafted": False, "reason": "Roux declined to pin (CATEGORY: NONE or unparseable)"}
    review = await review_pending_drafts(max_per_run=1, dry_run=dry_run)
    return {"drafted": True, "draft": draft, "review": review}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    ap = argparse.ArgumentParser(description="Roux gated self-authoring (voice) surface")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_draft = sub.add_parser("draft", help="Roux drafts one entry from a trigger (then review separately)")
    p_draft.add_argument("trigger")
    p_draft.add_argument("--context", default="")
    p_draft.add_argument("--review", action="store_true", help="draft AND review in one cycle")
    p_draft.add_argument("--dry-run", action="store_true")
    p_rev = sub.add_parser("review", help="gate pending drafts through bigbrain (Opus 4.8)")
    p_rev.add_argument("--max", type=int, default=5)
    p_rev.add_argument("--dry-run", action="store_true")
    sub.add_parser("list", help="show pending drafts")
    args = ap.parse_args()

    if args.cmd == "draft":
        if args.review:
            out = asyncio.run(run_voice_authoring_cycle(args.trigger, args.context, dry_run=args.dry_run))
        else:
            d = asyncio.run(draft_voice_entry(args.trigger, args.context))
            out = {"draft": d}
    elif args.cmd == "review":
        out = asyncio.run(review_pending_drafts(max_per_run=args.max, dry_run=args.dry_run))
    elif args.cmd == "list":
        out = {"pending": [d for d in _read_drafts() if d.get("status") == "pending"]}
    else:
        out = {"error": "unknown command"}
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _main()
