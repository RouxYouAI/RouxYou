"""Pending-draft queue — decouples proposal DRAFTING from the watchtower cron loop.

Why this exists (2026-05-30): roux_reflection, on a PROPOSE decision, used to call
POST /propose SYNCHRONOUSLY. /propose drafts via the authoring model — authoring-cpu
is CPU-pinned (~6.6 tok/s), so a 3000-token draft takes ~7 minutes. The watchtower
cron runs every job sequentially in ONE thread (services/watchtower/api.py::cron_loop),
so that ~7-minute draft froze EVERY cron (health_check, auto_restart, ...) — the
2026-05-30 "freeze". bigbrain was never the culprit (its call is ~19s); the slow
draft downstream of it was.

The fix is to decouple: reflection ENQUEUES a topic here and returns in ~20s; a
separate `proposal_drafter` cron claims one topic at a time and runs the slow draft
in a BACKGROUND daemon thread, off the cron critical path. The cron loop never waits
on drafting again, regardless of how slow the drafter model is.

Single-flight: at most one item is "drafting" at a time (one ~7-min CPU draft is
plenty; concurrent ones would saturate the CPU). Stale "drafting" claims (e.g. the
process restarted mid-draft, since the daemon thread isn't persisted) are reclaimed
after STALE_AFTER_S so the queue can't wedge.

Store: state/draft_queue.json — {"items": [ {id, topic, author, status, ...} ]}.
status ∈ pending | drafting | done | failed. Single watchtower process owns this
file (reflection cron enqueues, drafter cron claims), so a threading.Lock + atomic
rename is sufficient. Kept small and observable (the dashboard can read it later).
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

_BASE = Path(__file__).parent.parent
QUEUE_FILE = _BASE / "state" / "draft_queue.json"

# At most one item drafting at a time; reclaim a "drafting" claim older than this
# (covers a process restart that orphaned the background draft thread).
STALE_AFTER_S = int(os.getenv("ROUX_DRAFT_STALE_AFTER", "900"))  # 15 min
# Retry a failed draft up to this many times before parking it as "failed".
MAX_ATTEMPTS = int(os.getenv("ROUX_DRAFT_MAX_ATTEMPTS", "3"))
# Keep at most this many terminal (done/failed) items for observability, trim older.
MAX_TERMINAL_KEPT = 50

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_s(iso_ts: Optional[str]) -> float:
    if not iso_ts:
        return 1e9
    try:
        then = datetime.fromisoformat(iso_ts)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then).total_seconds()
    except Exception:
        return 1e9


def _load() -> Dict:
    if not QUEUE_FILE.exists():
        return {"items": []}
    try:
        with open(QUEUE_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "items" not in data:
            return {"items": []}
        return data
    except Exception:
        # Corrupt file: don't crash the cron — start clean (the queue is best-effort;
        # a dropped topic just gets re-proposed next reflection cycle).
        return {"items": []}


def _save(data: Dict) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, QUEUE_FILE)  # atomic


def _norm(topic: str) -> str:
    return " ".join((topic or "").split()).lower()[:400]


def enqueue_draft(topic: str, author: str) -> Optional[str]:
    """Append a pending draft. Dedups against any pending/drafting item with the same
    (normalized) topic so a re-proposing reflection cycle doesn't pile duplicates.
    Returns the item id, or None if it was a duplicate / empty topic."""
    topic = (topic or "").strip()
    if not topic:
        return None
    with _LOCK:
        data = _load()
        nt = _norm(topic)
        for it in data["items"]:
            if it.get("status") in ("pending", "drafting") and _norm(it.get("topic", "")) == nt:
                return None  # already queued / in flight
        item = {
            "id": uuid.uuid4().hex[:12],
            "topic": topic,
            "author": author,
            "status": "pending",
            "enqueued_at": _now(),
            "claimed_at": None,
            "attempts": 0,
            "last_error": None,
            "proposal_id": None,
            "completed_at": None,
        }
        data["items"].append(item)
        _save(data)
        return item["id"]


def claim_next() -> Optional[Dict]:
    """Single-flight claim. Returns the next item to draft (marked 'drafting') or None.

    None means: nothing to do right now — either the queue has no pending work, OR an
    item is already drafting and not yet stale (the background thread is still on it).
    A 'drafting' item older than STALE_AFTER_S is reclaimed (its thread was orphaned)."""
    with _LOCK:
        data = _load()
        # Reclaim a stale in-flight claim first.
        for it in data["items"]:
            if it.get("status") == "drafting" and _age_s(it.get("claimed_at")) > STALE_AFTER_S:
                it["status"] = "pending"
                it["last_error"] = "reclaimed stale drafting claim"
        # Single-flight: if anything is actively drafting, hold.
        if any(it.get("status") == "drafting" for it in data["items"]):
            _save(data)
            return None
        # Claim oldest pending.
        for it in data["items"]:
            if it.get("status") == "pending":
                it["status"] = "drafting"
                it["claimed_at"] = _now()
                it["attempts"] = it.get("attempts", 0) + 1
                _save(data)
                return dict(it)
        _save(data)
        return None


def complete_draft(item_id: str, proposal_id: Optional[str]) -> None:
    """Mark an item done (draft published)."""
    with _LOCK:
        data = _load()
        for it in data["items"]:
            if it.get("id") == item_id:
                it["status"] = "done"
                it["proposal_id"] = proposal_id
                it["completed_at"] = _now()
                it["last_error"] = None
                break
        _trim(data)
        _save(data)


def fail_draft(item_id: str, error: str) -> None:
    """Record a failed draft attempt. Re-queues for retry until MAX_ATTEMPTS, then
    parks the item as 'failed' so it stops consuming the single-flight slot."""
    with _LOCK:
        data = _load()
        for it in data["items"]:
            if it.get("id") == item_id:
                it["last_error"] = (error or "")[:300]
                if it.get("attempts", 0) >= MAX_ATTEMPTS:
                    it["status"] = "failed"
                    it["completed_at"] = _now()
                else:
                    it["status"] = "pending"  # retry on a later tick
                    it["claimed_at"] = None
                break
        _trim(data)
        _save(data)


def _trim(data: Dict) -> None:
    """Cap retained terminal items (done/failed) to keep the file small."""
    terminal = [it for it in data["items"] if it.get("status") in ("done", "failed")]
    if len(terminal) > MAX_TERMINAL_KEPT:
        keep_terminal = sorted(terminal, key=lambda it: it.get("completed_at") or "")[-MAX_TERMINAL_KEPT:]
        keep_ids = {id(it) for it in keep_terminal}
        data["items"] = [
            it for it in data["items"]
            if it.get("status") not in ("done", "failed") or id(it) in keep_ids
        ]


def list_drafts() -> List[Dict]:
    with _LOCK:
        return list(_load()["items"])


def queue_summary() -> Dict:
    """Counts by status — for the drafter cron's return value + observability."""
    items = list_drafts()
    out = {"pending": 0, "drafting": 0, "done": 0, "failed": 0, "total": len(items)}
    for it in items:
        out[it.get("status", "pending")] = out.get(it.get("status", "pending"), 0) + 1
    return out
