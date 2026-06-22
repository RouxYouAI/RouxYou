"""Post-execution reflection — Pillar 1 stub (2026-05-20).

Targets the four-pillar Roux Vision gap: "Verifier is post-hoc, not introspective."

This module runs a brief v3-driven self-assessment AFTER each completed proposal,
asking: did the result match the intent? Output is appended to
`state/reflection_log.jsonl` (append-only, one JSON per line). The reflection is
read-only — does NOT generate proposals (that's `reflection_coach.py`'s job at
the system-meta level). This is per-completion quality judgment, fast and
local.

Cadence: hourly cron. Cheap — only judges proposals NOT already reflected on.
Env-gated by ROUX_POST_EXEC_REFLECTION (default on).

Offline-first: uses v3 (companion alias), NOT bigbrain. Per
`feedback_offline_first.md`. Bigbrain stays reserved for fabrication catches
and DEFER escalation.
"""
import json
import os
import time
from pathlib import Path
from typing import Dict, List

import sys
_BASE = Path(__file__).parent.parent
sys.path.insert(0, str(_BASE))

from shared.logger import get_logger

logger = get_logger("post_exec_reflection")

REFLECTION_LOG = _BASE / "state" / "reflection_log.jsonl"
REFLECTED_INDEX = _BASE / "state" / "reflection_index.json"  # which prop_ids done

ENABLED = os.environ.get("ROUX_POST_EXEC_REFLECTION", "1") != "0"

REFLECTION_PROMPT = """You are reflecting on a recently-completed proposal in RouxYou. The proposal was approved and executed; now judge whether the OUTCOME matched the INTENT.

Be brief, specific, and honest. This is internal — for Roux's own quality dataset, not for human review.

PROPOSAL:
title: {title}
category: {category}
proposed_action: {proposed_action}

RESULT:
state: {state}
result_payload: {result}
approver: {approver}

Output (exact format):

QUALITY: HIGH | MEDIUM | LOW
NOTES: <one paragraph: did the result satisfy the proposed_action? Anything noteworthy about how it landed? Flag anything that smells off even if the lifecycle says "completed".>
"""


def _load_index() -> dict:
    if not REFLECTED_INDEX.exists():
        return {"reflected_ids": []}
    try:
        return json.loads(REFLECTED_INDEX.read_text())
    except Exception:
        return {"reflected_ids": []}


def _save_index(idx: dict) -> None:
    REFLECTED_INDEX.write_text(json.dumps(idx, indent=2))


def _append_log(entry: dict) -> None:
    REFLECTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(REFLECTION_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def reflect_on_completed(max_per_run: int = 5) -> dict:
    """Run reflection on up to max_per_run recently-completed proposals
    that haven't been reflected on yet."""
    if not ENABLED:
        return {"skipped": True, "reason": "ROUX_POST_EXEC_REFLECTION env flag is 0"}
    from shared.proposal_bus import get_history
    from shared.llm import llm_generate
    from shared.companion import _strip_thinking

    idx = _load_index()
    reflected_set = set(idx.get("reflected_ids", []))
    history = get_history(50)
    candidates = [p for p in history if p.get("state") == "completed" and p["id"] not in reflected_set]
    if not candidates:
        return {"reflected_count": 0, "reason": "no new completed proposals"}

    candidates = candidates[:max_per_run]
    results = []
    for p in candidates:
        prompt = REFLECTION_PROMPT.format(
            title=p.get("title", "?")[:100],
            category=p.get("category", "?"),
            proposed_action=(p.get("proposed_action") or "")[:1000],
            state=p.get("state", "?"),
            result=json.dumps(p.get("result") or {})[:500],
            approver=p.get("approved_by", "?"),
        )
        try:
            r = await llm_generate(
                "companion",
                prompt=prompt,
                max_tokens=1500,  # was 300 — v5.3 reasoning ~256 left ~44 for the answer (blank-risk). Generous; async/non-latency-critical.
                temperature=0.3,
                timeout=120,
            )
            raw = _strip_thinking(r.text or "") if getattr(r, "success", False) else ""
        except Exception as e:
            logger.warning(f"reflection v3 call failed for {p['id']}: {e}")
            raw = ""
        entry = {
            "proposal_id": p["id"],
            "title": p.get("title", "")[:100],
            "reflected_at": time.time(),
            "v3_raw": raw,
        }
        # Quick parse — extract QUALITY/NOTES if present
        for line in (raw or "").splitlines():
            if line.lower().startswith("quality:"):
                entry["quality"] = line.split(":", 1)[1].strip().upper()
                break
        _append_log(entry)
        reflected_set.add(p["id"])
        results.append({"proposal_id": p["id"], "quality": entry.get("quality", "?")})
    idx["reflected_ids"] = list(reflected_set)
    _save_index(idx)
    logger.info(f"POST-EXEC REFLECTION: judged {len(results)} completed proposals")
    return {"reflected_count": len(results), "items": results}
