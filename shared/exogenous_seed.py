"""Exogenous (non-self) seed for reflection — put something other than Roux in the
room before asking her what to improve.

Why (2026-05-31): reflection_coach.gather_self_observation() feeds bigbrain ONLY
self-referential context — Roux's own propose history, her shadow verdicts, her authored
proposals, her cron jobs, and a memory query literally seeded with "self-improvement
bootstrap autonomous propose". With nothing external in the room, the only meta-pattern
to act on is herself → the self-propose attractor. The topic-diversity gate stops the
dups from reaching the pipe, but reflection still just throttles itself and produces
nothing. This module gives her EXTERNAL material so she can propose useful work instead.

Grounded in what actually has signal on THIS box (verified 2026-05-31, not assumed):
  * Code TODOs: RouxYou's own .py has ~0 real ones (the 39 found were all in
    unsloth_compiled_cache; a stdlib scan additionally false-matched the literal word in
    module strings). DROPPED — no signal + false-positive-prone. Re-add via a real AST
    comment scanner if DJ ever annotates the tree.
  * Git: not a repo here. Dropped.
  * Standalone conversation files: stale (msg_count 0). Dropped — but the memory archive
    already indexes the conversation EXPORTS, so that signal is absorbed there.
  * Worker/orch failure logs: mostly transient startup 503s = noise. Dropped for Phase 1.
  * Memory archive (claude_memory.query_memory): the GOLDMINE. With work/build/improve
    query terms it surfaces DJ's real projects, lineage, and unfinished ideas (a
    curated_memory hit scored 0.67; conversation hits surfaced "auto-ingest pipeline",
    "the lab he's building", ComfyUI). This is the workhorse seed source.
  * roux_notes_user.md: curated, non-self, who-DJ-is + what-he-values. Grounds proposals.

Design: async (memory query is async), every source independently capped and wrapped so
one failing source can't break reflection, returns plain dicts the prompt formatter can
render. Env-gated by the caller (ROUX_EXOGENOUS_SEED).
"""

import os
import re
from pathlib import Path
from typing import Dict, List

_BASE = Path(__file__).parent.parent
_USER_NOTES = _BASE / "calibration" / "roux_notes_user.md"

# Non-self query angles. Deliberately about DJ's projects / system improvements / the
# broader build — NOT about Roux's own loop. Each is one query_memory call.
_MEMORY_QUERY_ANGLES = [
    "unfinished projects and next steps DJ wants to build",
    "open problems, bugs, or improvements in the RouxYou system",
    "features DJ wants: RAG, voice input, integrations, home automation, security",
]
_MEMORY_K = int(os.getenv("ROUX_SEED_MEMORY_K", "4"))
_MEMORY_MAX_KEEP = int(os.getenv("ROUX_SEED_MEMORY_MAX", "6"))  # total unique snippets
# Reserve this many of the MAX_KEEP slots for FRESH external_knowledge drops (by recency),
# so curated drops always reach reflection even if broad queries rank them lower.
_EXTERNAL_RESERVE = int(os.getenv("ROUX_SEED_EXTERNAL_RESERVE", "3"))


def _clean(text: str, n: int = 240) -> str:
    return " ".join((text or "").split())[:n]


# ---------------------------------------------------------------- memory (primary)
def _parse_memory_blocks(raw: str) -> List[Dict]:
    """query_memory returns a formatted string of '[N] (score: X, era: Y...)\\n  content
    \\n  Source: {...}' blocks. Parse into [{score, era, text}] — best-effort, robust to
    format drift (a block with no parseable score just gets score 0.0)."""
    if not raw:
        return []
    out: List[Dict] = []
    # Split on the '[N]' entry markers (keep them).
    parts = re.split(r"\n(?=\[\d+\]\s*\()", raw.strip())
    for part in parts:
        m = re.match(r"\[\d+\]\s*\(score:\s*([0-9.]+),\s*era:\s*([^,)]+)", part)
        score = float(m.group(1)) if m else 0.0
        era = m.group(2).strip() if m else "?"
        # content = lines after the header, excluding the Source: line
        lines = part.splitlines()[1:]
        content = " ".join(
            ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("Source:")
        )
        content = _clean(content, 240)
        if content:
            out.append({"score": round(score, 4), "era": era, "text": content})
    return out


def _parse_recent_blocks(raw: str) -> List[Dict]:
    """Parse list_recent_memories output: '[N] Added: TS (era: X)\\n  content\\n  Source: ..'
    into [{era, text}]. Recency order is preserved (newest first, as the MCP returns)."""
    if not raw:
        return []
    out: List[Dict] = []
    parts = re.split(r"\n(?=\[\d+\]\s+Added:)", raw.strip())
    for part in parts:
        m = re.search(r"\(era:\s*([^)]+)\)", part)
        era = m.group(1).strip() if m else "?"
        lines = part.splitlines()[1:]
        content = " ".join(
            ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("Source:")
        )
        content = _clean(content, 240)
        if content:
            out.append({"era": era, "text": content, "score": 1.0})
    return out


def _external_knowledge_from_disk(limit: int) -> List[Dict]:
    """Contention-free fallback source for external_knowledge work-items.

    2026-06-03 fix: the archive path below (recent_memories) intermittently returns EMPTY
    in the busy cron-host — it pulls the 40 most-recent memories ACROSS ALL ERAS then
    filters, so (a) it times out / errors under concurrent load (→ silent []), and (b) other
    memory writes (reflection history, runtime, shadow) crowd the work-items out of the top-40
    window. Proven overnight: 21/21 reflection SKIPs had an EMPTY seed. The work-items are also
    durably stamped as .md files in knowledge_inbox/processed/ by the ingester — read them
    straight off disk (no MCP, no recency-window crowding, no lock contention). Filename is
    `<TS>Z__<original>.md`, so sorting by name descending = newest first."""
    proc = _BASE / "state" / "knowledge_inbox" / "processed"
    try:
        files = sorted(
            (p for p in proc.glob("*__workitem_*.md") if p.is_file()),
            key=lambda p: p.name, reverse=True,
        )
    except Exception:
        return []
    out: List[Dict] = []
    for p in files[: max(limit * 2, limit)]:  # read a few extra; dedup happens upstream
        try:
            orig = p.name.split("__", 1)[1] if "__" in p.name else p.name
            content = _clean(p.read_text(encoding="utf-8", errors="replace"), 240)
        except Exception:
            continue
        if not content:
            continue
        out.append({
            "era": "external_knowledge",
            "text": f"[external knowledge — source: {orig}] {content}",
            "score": 1.0,
            "source_kind": "recent_external_disk",
        })
        if len(out) >= limit:
            break
    return out


async def gather_recent_external_knowledge(limit: int) -> List[Dict]:
    """Fresh curated drops (era=external_knowledge) by RECENCY, not semantic rank — so
    a paper DJ dropped today reliably reaches reflection even if the broad seed queries
    rank entrenched archive memories higher.

    Primary source = the archive recency query; on EMPTY or error it falls back to reading
    the work-item files off disk (knowledge_inbox/processed/) so a busy cron-host can never
    starve reflection of standing work — see _external_knowledge_from_disk (2026-06-03)."""
    if limit <= 0:
        return []
    ext: List[Dict] = []
    try:
        from shared.claude_memory import recent_memories
        raw = await recent_memories(n=40)
        ext = [b for b in _parse_recent_blocks(raw) if b.get("era") == "external_knowledge"]
        for b in ext:
            b["source_kind"] = "recent_external"
    except Exception:
        ext = []
    # Fallback: archive returned nothing usable (timeout / crowded window / error) → disk.
    if not ext:
        ext = _external_knowledge_from_disk(limit)
    return ext[:limit]


async def gather_memory_seed() -> List[Dict]:
    """Build the memory seed: RESERVE the top slots for fresh external_knowledge drops
    (by recency), then fill the rest with semantic work-item queries over the archive."""
    seen = set()
    collected: List[Dict] = []

    # 1) Reserved: fresh curated external knowledge (always reaches reflection).
    try:
        for b in await gather_recent_external_knowledge(_EXTERNAL_RESERVE):
            key = b["text"][:80].lower()
            if key in seen:
                continue
            seen.add(key)
            collected.append(b)
    except Exception:
        pass

    # 2) Semantic work-item queries fill the remaining slots.
    try:
        from shared.claude_memory import query_memory
    except Exception:
        return collected[:_MEMORY_MAX_KEEP]
    semantic: List[Dict] = []
    for angle in _MEMORY_QUERY_ANGLES:
        try:
            raw = await query_memory(angle, k=_MEMORY_K)
        except Exception:
            continue
        for blk in _parse_memory_blocks(raw or ""):
            key = blk["text"][:80].lower()
            if key in seen:
                continue
            seen.add(key)
            blk["angle"] = angle
            semantic.append(blk)
    semantic.sort(key=lambda b: b.get("score", 0.0), reverse=True)

    # Reserved external-knowledge first (high value, fresh), then top semantic, capped.
    return (collected + semantic)[:_MEMORY_MAX_KEEP]


# ---------------------------------------------------------------- user context
def gather_user_context() -> str:
    """The curated who-DJ-is notes (non-self). Short; grounds proposals in DJ's values."""
    try:
        if _USER_NOTES.exists():
            return _clean(_USER_NOTES.read_text(), 1200)
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- orchestrate
async def gather_exogenous_signals() -> Dict:
    """Collect non-self signal sources. Never throws — a failing source returns empty.
    Returns {memory_seed: [...], user_context: str, any: bool}.

    MEMORY-CENTRIC (2026-05-31): this is a memory-centric system — the real "what's next"
    lives in DJ's archive, not in code/git/logs (verified: own-code TODOs ≈ 0 / all were
    false positives, not a git repo, conversation files stale, failure logs are transient
    noise). So the seed = the memory archive (work-item queries) grounded by who-DJ-is.
    code TODOs were dropped: false-positive-prone (matched the literal word in module
    strings) and ~0 real signal. Re-add via a proper AST comment scanner if DJ ever
    annotates the tree."""
    try:
        memory_seed = await gather_memory_seed()
    except Exception:
        memory_seed = []
    user_context = gather_user_context()
    return {
        "memory_seed": memory_seed,
        "user_context": user_context,
        "any": bool(memory_seed),  # user_context alone isn't "work to do"
    }


def format_exogenous_for_prompt(sig: Dict) -> str:
    """Render the signals as a prompt block. Empty-safe — returns a clear 'no external
    signal' marker so the steering instruction can tell reflection to SKIP."""
    if not sig or not sig.get("any"):
        ctx = (sig or {}).get("user_context", "")
        tail = f"\n\n(Who DJ is, for grounding:\n{ctx})" if ctx else ""
        return ("NO concrete external work-item surfaced from the memory archive this "
                "cycle." + tail)
    lines: List[str] = []
    ms = sig.get("memory_seed") or []
    if ms:
        lines.append("From DJ's memory archive (projects / unfinished work / ideas — propose about THESE):")
        for b in ms:
            lines.append(f"  - [{b.get('era','?')}] {b.get('text','')}")
    ctx = sig.get("user_context", "")
    if ctx:
        lines.append(f"\nWho DJ is (grounding):\n{ctx}")
    return "\n".join(lines)
