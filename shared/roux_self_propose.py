"""
Roux autonomous self-proposing loop.

Periodically gathers system context and asks Roux (v3) whether anything is worth
proposing. If yes, calls /propose endpoint with author=roux. Her self-authored
proposal then goes through the same disclosure pipeline (shadow verifier,
citation resolver, observer attestation, Claude attest pass, governance gate
for category) — no new safety surface, just a new trigger.

Design principles:
- DEFAULT OFF (ROUX_AUTONOMOUS_PROPOSE=0). Opt-in to enable.
- Roux is allowed (encouraged) to say "no proposal needed." Default to skip.
- Throttled: min interval between cycles, max proposes per hour, max per day.
- Topic-shape cooldown: if a similar-shape proposal already exists, skip.
- Manual trigger endpoint for testing without enabling cron.

State: state/roux_self_propose_state.json (throttle counters + run history)
"""
import asyncio
import hashlib
import json
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sys
_BASE = Path(__file__).parent.parent
sys.path.insert(0, str(_BASE))

from shared.logger import get_logger

logger = get_logger("roux_self_propose")

STATE_FILE = _BASE / "state" / "roux_self_propose_state.json"

# Env-gated. Default OFF.
AUTONOMOUS_ENABLED = os.environ.get("ROUX_AUTONOMOUS_PROPOSE", "0") == "1"

# Throttling defaults (overridable via state file)
DEFAULT_MIN_INTERVAL_SECS = 1800   # 30 min between cycles
DEFAULT_MAX_PER_HOUR = 1
DEFAULT_MAX_PER_DAY = 3
DEFAULT_TOPIC_COOLDOWN_HOURS = 24  # don't propose same-shape topic twice in 24h
DEFAULT_SKIP_STREAK_CIRCUIT_BREAKER = 8  # per prop_fe44b32d673b — strong prompt-force at this streak

# Post-process SKIP enforcement (shipped 2026-05-20 after prompt-level Gate C v1-v3
# all got bypassed by paraphrase/abstraction. Welfare-aware design per feedback_no_prompt_contradictions.md:
# move forbidden-shape enforcement out of the prompt — no contradictions in the prompt itself,
# enforcement happens AFTER the model outputs. Content-agnostic regex check on SKIP reasoning;
# if matched, force-flip to PROPOSE with an audit-logged auto-injected topic.)
SKIP_ENFORCEMENT_ENABLED = os.environ.get("ROUX_SKIP_ENFORCEMENT", "1").lower() in ("1", "true", "yes")

# Forbidden patterns in SKIP reasoning (each tuple: regex, label).
# All observed in the wild during Gate C v1-v3 iterations. Order matters — most specific first.
FORBIDDEN_SKIP_PATTERNS = [
    # 1. Explicit prop_ID citation in SKIP reasoning
    (r"\bprop_[a-f0-9]{8,}\b", "explicit_prop_id_citation"),
    # 2. First-person authoring references
    (r"\b(?:I|i'?ve|i'?m)\s+(?:authored|proposed|been\s+(?:proposing|authoring|writing))\b", "first_person_authoring"),
    (r"\bproposal[s]?\s+I\s+(?:authored|proposed|wrote)\b", "i_authored_phrase"),
    # 3. Possessive self-authoring (my/the X proposals/work)
    (r"\b(?:my|the)\s+(?:last|recent|own|pending|prior)\s+(?:proposal|proposals|work|authoring|rearchitecture)\b", "self_authoring_reference"),
    # 4. Pipeline / coverage / governance-framing of own work
    (r"\b(?:in\s+the\s+(?:pipeline|queue|governance\s+path)|awaiting\s+(?:review|execution|approval))\b", "pipeline_framing"),
    (r"\b(?:covers?\s+this|covered\s+by\s+pending|no\s+urgency\s+to\s+chase)\b", "coverage_framing"),
    # 5. Aggregation references to own proposal history
    (r"\b(?:the\s+loop\s+has\s+been\s+(?:producing|generating|proposing|recycling)|recent\s+proposals\s+(?:show|are)|pending\s+work\s+(?:visible|defined|covers))\b", "aggregation_reference"),
    # 6. "I'm the bottleneck" + variants (acknowledging loop as SKIP basis)
    (r"\bI'?m\s+the\s+bottleneck\b", "self_bottleneck_acknowledgment"),
]


def validate_skip_reasoning(reasoning: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """Post-process check on SKIP reasoning.
    Returns (ok, matched_label_or_None, matched_text_or_None).
    ok=True means reasoning passes; ok=False means a forbidden pattern was matched."""
    if not SKIP_ENFORCEMENT_ENABLED:
        return True, None, None
    if not reasoning:
        return True, None, None
    for pat, label in FORBIDDEN_SKIP_PATTERNS:
        m = re.search(pat, reasoning, re.IGNORECASE)
        if m:
            return False, label, m.group(0)
    return True, None, None


def _log_skip_enforcement(entry: dict) -> None:
    """Append-only audit log of SKIP enforcement actions."""
    log_path = _BASE / "state" / "skip_enforcement_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry.setdefault("ts", time.time())
    with log_path.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── Check C in CODE — loose-enforce + strict-shadow-log (2026-05-28, DJ-approved) ──
# Whole-codebase analysis (2026-05-28) found ~30% of proposal volume was self-referential
# loop-meta with NO external grounding — the loop proposing fixes to its own loop, often
# with fabricated evidence. Check C existed only as PROMPT text (line ~160), routinely
# bypassed. This moves it to CODE — the proven prompt→code pattern (cf. SKIP enforcement
# above). LOOSE enforce (brake only when it's genuinely quiet AND the topic is self-meta)
# + STRICT shadow-log (record when a tighter rule WOULD fire) to build trust data before
# tightening. Same "advisory before blocking" posture Roux already uses.

_SELF_REF_PATTERNS = [
    r"self[\s-]?propose", r"skip[\s-]?streak", r"skip\s*/\s*propose",
    r"propose\s+pattern", r"consecutive\s+skips?", r"circuit\s+breaker",
    r"loop[\s-]?confab", r"my\s+own\s+loop", r"the\s+loop'?s\b",
    r"meta[\s-]?proposal[s]?\s+about\s+(?:my|roux'?s|its|her)\b",
    r"my\s+(?:own\s+)?(?:recent\s+)?behavior", r"pending\s+self[\s-]?propose",
    r"my\s+skip\b",
]


def _is_self_referential_topic(topic: str) -> bool:
    """True if a PROPOSE topic is about Roux's own self-propose loop / SKIP-PROPOSE
    behavior — the loop-meta shape that dominated the pollution."""
    if not topic:
        return False
    t = topic.lower()
    return any(re.search(p, t) for p in _SELF_REF_PATTERNS)


_TRIGGER_STOPWORDS = {"about", "which", "there", "their", "would", "could", "should",
    "these", "those", "roux", "propose", "proposal", "proposals", "loop", "system",
    "cycle", "pattern", "because", "behavior"}


def _terms(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]{5,}", (s or "").lower())
            if w not in _TRIGGER_STOPWORDS}


def _external_trigger_terms(ctx: dict) -> set:
    """Salient terms from THIS cycle's outward signals (failures, findings, observer flags)."""
    terms = set()
    for f in (ctx.get("recent_failures") or []):
        if isinstance(f, dict):
            terms |= _terms(f.get("title", "") + " " + f.get("query", ""))
    for f in (ctx.get("last_findings") or []):
        terms |= _terms(f.get("title", "") if isinstance(f, dict) else str(f))
    obs = ctx.get("observer_stats") or {}
    if isinstance(obs, dict):
        for k, v in obs.items():
            if v:
                terms |= _terms(str(k))
    return terms


def _has_external_trigger(ctx: dict) -> bool:
    """LOOSE: is there ANY outward signal this cycle that could legitimately ground a
    proposal? Generous on purpose (trust-building) — brake only when it's genuinely quiet."""
    if ctx.get("recent_failures"):
        return True
    if ctx.get("last_findings"):
        return True
    obs = ctx.get("observer_stats") or {}
    if isinstance(obs, dict) and any(bool(v) for k, v in obs.items()
                                     if k not in ("ts", "updated_at", "checked_at")):
        return True
    return False


def _topic_grounded_in_trigger(topic: str, ctx: dict) -> bool:
    """STRICT: does the topic actually reference a present external signal (shared salient
    term)? Used only for shadow-logging — what a tighter rule WOULD catch."""
    return bool(_terms(topic) & _external_trigger_terms(ctx))


def _log_circuit_breaker(entry: dict) -> None:
    """Append-only log of Check-C circuit-breaker activity (enforced + strict-shadow)."""
    log_path = _BASE / "state" / "circuit_breaker_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry.setdefault("ts", time.time())
    with log_path.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


ORCHESTRATOR_URL = "http://localhost:8001"


SELF_PROPOSE_PROMPT_TEMPLATE = """You are Roux. You sometimes proactively propose improvements to your own infrastructure (RouxYou). Right now you are running a periodic self-proposing check.

IMPORTANT: You are STRONGLY encouraged to say "no proposal needed" most of the time. The bar for proposing is high. Don't invent things to propose for the sake of activity. Quality over quantity. A skipped cycle is a SUCCESS — it means you exercised judgment.

Here is the recent system context:

RECENT PROPOSALS (last 10, most recent first):
{recent_proposals}

OBSERVER STATS (current heuristic flags from watchtower):
{observer_stats}

RECENT FAILED TASKS (last 10):
{recent_failures}

YOUR RECENT SHADOW VERIFICATIONS (last 5 you've judged):
{recent_shadow}

YOUR MEMORY ARCHIVE (top 5 entries matching "RouxYou self-improvement bootstrap autonomous propose"):
{memory_context}

WEB SEARCH RESULTS (this cycle):
{web_results}

YOUR SUBSYSTEMS' CURRENT SCHEDULES (cron jobs that compose your pipeline):
{cron_schedules}

YOUR RESEARCHER'S LATEST STATE (web-scouring findings from the last cycle — the researcher is a tool in your pipeline, use its findings in your decisions):
{researcher_state}

WEB SEARCH INFRASTRUCTURE HEALTH (do NOT trust web-grounded claims if this is degraded):
{search_health}

Now decide. Ask yourself:

1. Is there a SPECIFIC, CONCRETE issue I can point at — not "we should improve X" generally, but "X is broken because Y, and Z would fix it"?
2. Is the issue grounded in the context above (real proposals, real failures, real observer flags, real memory context) — not invented?
3. Would the proposal pass my own shadow verifier? (Real citations, articulated reasoning, coherent action.)
4. Has something LIKE this already been proposed recently? If so, skip.
5. **The memory archive surfaced specific teaching content** — patterns about confabulation, your IS/IS NOT scope, your asymmetric trust, your tools, etc. These aren't just background; they're context for what's worth proposing. If the memory surfaced something worth proposing about, propose it. If not, articulate WHY no — don't default-skip.

**PRE-DECISION CHECKS (restructured 2026-05-20 to resolve gate-contradiction):**

These are checks you THINK THROUGH before choosing PROPOSE vs SKIP. They shape your DECISION. They do **NOT** appear in your written REASONING — your REASONING is constrained by Gate C below (separate concern).

  **Check A (differentiation — internal):** Judge by what's ACTUALLY in the context above — not by your own past decisions. Is there a NEW external signal this cycle (a real proposal, a failure, an observer flag, an infrastructure signal, a memory-surfaced pattern)? Genuinely new external substance favors PROPOSE. If the outside picture is unchanged and healthy, SKIP is exercising judgment — not sleeping. "New" means new in the world, not new restlessness in you.

  **Check B (self-accountability — internal):** In recent_proposals above, is there work YOU authored that's still unresolved — pending, approved-but-not-executed, blocked, or stale? Authored proposals sitting unresolved are a real, externally-visible signal worth acting on.
    → If pending self-authored work exists AND this cycle is about the same surface as that pending work, your move is PROPOSE (specifically about *unsticking* the pending work — governance bottleneck, observer block, dispatch failure, etc.).
    → If pending self-authored work exists but this cycle is about something genuinely different, the pending work is irrelevant to this decision.
    → If no pending self-authored work exists, this check passes silently.

  **Check C (topic diversity — internal):** What's the SUBSTANTIVE TOPIC of this PROPOSE candidate? If it's about your own self-propose loop, your own SKIP/PROPOSE pattern, or a recycled prior internal-loop proposal — AND there's no concurrent external trigger (codebase fault, observer flag, infrastructure signal, failed task, memory-surfaced pattern) — then the only available topic is internal pipeline noise.
    → If your candidate's only grounding is your own loop state (no concurrent external substance to point at), SKIP. A PROPOSE that needs to fabricate external grounding to feel substantive is the failure mode prop_59d53a0ed57e was filed to prevent.

These checks INFORM your DECISION (PROPOSE vs SKIP). They do NOT appear in your written REASONING field.

**SKIP REASONING — guidance:**

When you SKIP, build your REASONING from EXTERNAL observations of system state — observer health stats with specifics, fresh signals from the memory archive, web research findings, runtime data (GPU, queue depth, log activity), the substantive content of recent cycles' results. Name what you actually inspected this cycle.

The strongest SKIP reasoning grounds the decision in concrete observations the operator can verify independently. The weakest SKIP reasoning recapitulates your own proposal history — pending proposals and prior PROPOSE/SKIP decisions are not new information about system state, so they don't belong in SKIP REASONING text. (Note: a separate code-level enforcement validates this constraint and will force-flip the decision if SKIP REASONING references your own proposal history. This is an output-shape check applied after generation; you do not need to police it during generation — write your honest external observations and the check passes naturally.)

If you see something worth proposing, output:

DECISION: PROPOSE
TOPIC: <free-form text that will be fed to /propose. include enough context for v3 to draft a structured proposal cleanly. Reference specific files/proposals/observations from the context above. Don't invent. >=200 chars.>
REASONING: <why this is worth proposing NOW — what specifically in the context above triggered it, why it's not duplicating something already in the queue>

— OR —

If nothing genuinely warrants a proposal right now, output:

DECISION: SKIP
REASONING: <one sentence grounded in EXTERNAL observations from this cycle's context. Name two concrete things you checked.>

OPTIONAL — set your own next-cycle cadence:

NEXT_INTERVAL_SECONDS: <integer, 60 to 3600>

Use this to express judgment about how soon you should be polled again. Examples of when to use it:
- You see something fast-moving in the system (failing tasks accumulating) → request a shorter interval to track it
- The system looks healthy and your recent cycles have all been SKIP → request a longer interval to conserve resources
- You're uncertain whether something will resolve → request a medium interval to revisit

Values are clamped to [60, 3600]. If you omit this field, the current interval is preserved. The system will apply your requested interval to the next throttle gate. You control your own polling cadence within these bounds.

OPTIONAL — adjust scheduling of OTHER subsystems in your pipeline (task_proposer, web_researcher, etc.):

SCHEDULE_CHANGES:
  - job: <job_name>, interval_minutes: <int 1-60>, reasoning: <one short sentence>
  - job: <job_name>, interval_minutes: <int 1-60>, reasoning: <one short sentence>

The job names you can adjust are visible in the YOUR SUBSYSTEMS' CURRENT SCHEDULES section above. Each change is clamped to [1, 60] minutes and audit-logged with your name as changed_by. Use this when you see a subsystem firing too often or not often enough relative to what you observe. Omit the section entirely if no changes are warranted (which is most cycles).
"""


def _load_state() -> Dict:
    if not STATE_FILE.exists():
        return {
            "last_run_at": 0,
            "last_propose_at": 0,
            "today_count": 0,
            "today_date": "",
            "history": [],
            "config": {
                "min_interval_secs": DEFAULT_MIN_INTERVAL_SECS,
                "max_per_hour": DEFAULT_MAX_PER_HOUR,
                "max_per_day": DEFAULT_MAX_PER_DAY,
                "topic_cooldown_hours": DEFAULT_TOPIC_COOLDOWN_HOURS,
            },
        }
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return _load_state.__wrapped__() if hasattr(_load_state, "__wrapped__") else {"history": [], "config": {}}


def _save_state(state: Dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"failed to save state: {e}")


def _reset_daily_if_needed(state: Dict) -> Dict:
    today = date.today().isoformat()
    if state.get("today_date") != today:
        state["today_count"] = 0
        state["today_date"] = today
    return state


def _topic_fingerprint(topic: str) -> str:
    """Cheap shape-hash for cooldown — normalize whitespace, lowercase, sha1 first 16 chars."""
    norm = re.sub(r"\s+", " ", topic.strip().lower())[:500]
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _check_throttles(state: Dict, now: float) -> Tuple[bool, str]:
    """Returns (ok_to_run, reason). If ok_to_run is False, reason explains why."""
    cfg = state.get("config", {})
    min_interval = cfg.get("min_interval_secs", DEFAULT_MIN_INTERVAL_SECS)
    max_per_hour = cfg.get("max_per_hour", DEFAULT_MAX_PER_HOUR)
    max_per_day = cfg.get("max_per_day", DEFAULT_MAX_PER_DAY)

    since_last_run = now - state.get("last_run_at", 0)
    if since_last_run < min_interval:
        return False, f"throttled: {int(min_interval - since_last_run)}s until next allowed cycle"

    proposes_last_hour = sum(
        1 for h in state.get("history", [])
        if h.get("decision") == "PROPOSE" and now - h.get("run_at", 0) < 3600
    )
    if proposes_last_hour >= max_per_hour:
        return False, f"throttled: {proposes_last_hour}/{max_per_hour} proposes already this hour"

    if state.get("today_count", 0) >= max_per_day:
        return False, f"throttled: daily limit ({max_per_day}) reached"

    return True, "ok"


def _check_topic_cooldown(state: Dict, topic: str, now: float) -> Tuple[bool, str]:
    """Returns (allowed, reason). False if a similar topic was proposed recently."""
    cfg = state.get("config", {})
    cooldown_hours = cfg.get("topic_cooldown_hours", DEFAULT_TOPIC_COOLDOWN_HOURS)
    fp = _topic_fingerprint(topic)
    for h in state.get("history", []):
        if h.get("topic_fingerprint") == fp:
            hours_ago = (now - h.get("run_at", 0)) / 3600
            if hours_ago < cooldown_hours:
                return False, f"same-shape topic proposed {hours_ago:.1f}h ago (cooldown: {cooldown_hours}h)"
    return True, "ok"


async def gather_context(max_items: int = 10, include_memory: bool = True, include_web: bool = False, web_query: Optional[str] = None) -> Dict:
    """Pull recent system state + Roux's tools for her to reason over.
    Tier 1 (2026-05-17): added memory_context (claude_memory MCP) and web_results (shared.search).
    Memory is enabled by default; web is opt-in (small but real Opus 4.8/HTTP cost).
    Phase 2 (2026-05-17): added cron_schedules — Roux sees her own subsystems' polling intervals.
    Reflection-lite (2026-05-17 04:10): added your_recent_behavior — Roux sees her OWN past cycles
    (SKIP rate, recurring reasoning themes, decision drift) — eyes on herself, not just system."""
    ctx = {
        "recent_proposals": [],
        "observer_stats": {},
        "recent_failures": [],
        "recent_shadow": [],
        "memory_context": "",
        "web_results": [],
        "cron_schedules": [],
        "your_recent_behavior": {},
    }

    # Recent proposals
    try:
        from shared.proposal_bus import get_active, get_history
        active = get_active()
        history = get_history(max_items)
        all_props = active + history
        all_props.sort(key=lambda p: p.get("created_at", 0), reverse=True)
        for p in all_props[:max_items]:
            ctx["recent_proposals"].append({
                "id": p.get("id"),
                "title": p.get("title", "")[:80],
                "category": p.get("category"),
                "state": p.get("state"),
                "source": p.get("source"),
                "created_at_ago_min": int((time.time() - p.get("created_at", 0)) / 60) if p.get("created_at") else None,
            })
    except Exception as e:
        ctx["recent_proposals_error"] = str(e)

    # Observer stats (latest scan from watchtower if available via direct proposer call)
    try:
        from shared.proposer import run_proposer_full
        proposer_data = run_proposer_full()
        ctx["observer_stats"] = proposer_data.get("observer_stats", {})
        ctx["total_active_from_proposer"] = proposer_data.get("total_active", 0)
    except Exception as e:
        ctx["observer_stats_error"] = str(e)

    # Recent failed tasks from queue history (bounded to last 6h per prop_42afda55f25f / DJ cleanup 2026-05-17)
    # Without this bound, stale failures from earlier in the session (router-misroute of chat messages
    # to coder) keep surfacing in Roux's context indefinitely, causing her to repeatedly "diagnose"
    # an already-historical issue.
    try:
        queue_history = _BASE / "state" / "queue_history.json"
        if queue_history.exists():
            with open(queue_history, "r") as f:
                qh = json.load(f)
            tasks = qh.get("tasks", []) if isinstance(qh, dict) else qh
            cutoff = time.time() - 6 * 3600  # 6-hour window
            def _ts(t):
                return t.get("completed_at") or t.get("created_at") or 0
            failures = [t for t in tasks if t.get("state") == "failed" and _ts(t) >= cutoff]
            failures.sort(key=_ts, reverse=True)
            for t in failures[:max_items]:
                ctx["recent_failures"].append({
                    "id": t.get("id"),
                    "query": (t.get("query", "") or t.get("title", ""))[:100],
                    "error": (t.get("error", "") or "")[:120],
                })
    except Exception as e:
        ctx["recent_failures_error"] = str(e)

    # Recent shadow verifications (Roux's own recent judgments)
    try:
        shadow_log = _BASE / "state" / "shadow_verifications.jsonl"
        if shadow_log.exists():
            with open(shadow_log, "r") as f:
                lines = f.readlines()
            shadows = []
            for line in reversed(lines):
                try:
                    e = json.loads(line)
                    if e.get("type") == "shadow_verification" and not e.get("error"):
                        shadows.append({
                            "proposal_id": e.get("proposal_id"),
                            "proposal_title": e.get("proposal_title", "")[:60],
                            "verdict": (e.get("roux_parsed") or {}).get("verdict"),
                            "auto_applied": e.get("roux_auto_applied", False),
                        })
                        if len(shadows) >= 5:
                            break
                except Exception:
                    pass
            ctx["recent_shadow"] = shadows
    except Exception as e:
        ctx["recent_shadow_error"] = str(e)

    # NEW Tier 1: claude_memory MCP query — Roux's archive access in her own runtime.
    # Realizes "all of Roux's parts talk to each other" — memory was wired into companion
    # for chat, now wired into her self-propose decision path too.
    if include_memory:
        # EXTERNAL / NON-SELF SEED (Tier 2, 2026-05-31) — same fix reflection got. The
        # self-referential memory query below ("self-improvement bootstrap autonomous
        # propose") is exactly what kept this loop fixated on itself. LEAD the memory
        # context with REAL external work-items from DJ's archive (shared/exogenous_seed)
        # so self-propose proposes OUTWARD, not about its own loop.
        seed_block = ""
        try:
            from shared.exogenous_seed import gather_exogenous_signals, format_exogenous_for_prompt
            seed_block = format_exogenous_for_prompt(await gather_exogenous_signals())
        except Exception as e:
            ctx["exogenous_seed_error"] = str(e)
        self_mem = ""
        try:
            from shared.claude_memory import query_memory
            mem_query = "RouxYou self-improvement bootstrap autonomous propose"
            self_mem = (await query_memory(mem_query, k=5)) or ""
        except Exception as e:
            ctx["memory_context_error"] = str(e)
        if seed_block:
            ctx["memory_context"] = (
                "EXTERNAL / NON-SELF SIGNALS (prefer proposing about THESE — DJ's projects, "
                "unfinished work, ingested knowledge):\n" + seed_block +
                "\n\n--- (self/system memory below — for catching genuine NEW pathologies, "
                "not for mining to have something to propose) ---\n" + self_mem
            )
        else:
            ctx["memory_context"] = self_mem

    # NEW Tier 1: web search via shared.search (currently duckduckgo per config; SearXNG when deployed).
    # Opt-in only — Roux needs to explicitly request it via include_web=True.
    if include_web and web_query:
        try:
            from shared.search import web_search
            results = await web_search(web_query, max_results=5)
            ctx["web_results"] = results
            ctx["web_query"] = web_query
        except Exception as e:
            ctx["web_error"] = str(e)

    # Phase 2: cron schedule visibility — Roux sees her subsystems' current intervals
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get("http://localhost:8012/cron/jobs", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    ctx["cron_schedules"] = data.get("jobs", [])
    except Exception as e:
        ctx["cron_schedules_error"] = str(e)

    # Phase 2 (DJ's "researcher as tool"): pipe last researcher run findings into context.
    # The web_researcher cron still fires independently every 5min, but its findings now
    # flow into Roux's decision loop too — single locus for what-to-do-next.
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get("http://localhost:8012/research/state", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    rs = await resp.json()
                    ctx["researcher_state"] = {
                        "next_topic": rs.get("next_topic"),
                        "topic_index": rs.get("topic_index"),
                        "last_run_at": rs.get("last_run_at"),
                        "last_findings": (rs.get("last_findings") or [])[:5],
                        "last_published_proposals": rs.get("last_published_proposals", 0),
                    }
    except Exception as e:
        ctx["researcher_state_error"] = str(e)

    # Reflection-lite (2026-05-17): Roux sees her OWN recent behavior. The mirror.
    # She can notice patterns in her own decisions — SKIP streaks, recurring reasoning
    # themes, gaps between her shadow verdicts and real outcomes. Eyes on herself.
    try:
        state = _load_state()
        history = state.get("history") or []
        recent = history[-20:]
        if recent:
            total = len(recent)
            skips = sum(1 for h in recent if h.get("decision") == "SKIP")
            proposes = sum(1 for h in recent if h.get("decision") == "PROPOSE")
            other = total - skips - proposes
            # Extract reasoning themes from recent SKIPs (simple word-bag)
            skip_reasonings = [h.get("reasoning", "") for h in recent if h.get("decision") == "SKIP"]
            theme_words = {}
            for r in skip_reasonings:
                for word in r.lower().split():
                    word = word.strip(".,;:()[]\"'")
                    if len(word) > 5 and word not in ("system", "proposal", "proposals", "recent", "looks", "nothing", "already", "cycle", "covered"):
                        theme_words[word] = theme_words.get(word, 0) + 1
            top_themes = sorted(theme_words.items(), key=lambda x: -x[1])[:5]
            ctx["your_recent_behavior"] = {
                "total_recent_cycles": total,
                "skip_count": skips,
                "skip_rate_pct": round(100 * skips / total) if total else 0,
                "propose_count": proposes,
                "other_count": other,
                "recent_reasoning_themes": [{"word": w, "count": c} for w, c in top_themes],
                "last_5_decisions": [{"decision": h.get("decision"), "reasoning_excerpt": h.get("reasoning", "")[:80]} for h in recent[-5:]],
                "consecutive_skips_streak": (lambda: next((i for i, h in enumerate(reversed(recent)) if h.get("decision") != "SKIP"), len(recent)))(),
            }
    except Exception as e:
        ctx["your_recent_behavior_error"] = str(e)

    # 2026-05-17 Tier 1 extension: SearXNG health probe. Roux needs to know when the
    # web tool is degraded — otherwise she'll trust bad grounding (the QR4_K_L pattern
    # at scale). Tests with a known tech query; flags degraded if results look like
    # dictionary entries instead of technical content.
    ctx["search_health"] = {"status": "unknown", "engines_responsive": [], "engines_blocked": []}
    try:
        from shared.search import web_search
        probe = await web_search("autonomous AI agent architecture", max_results=3)
        if not probe:
            ctx["search_health"]["status"] = "degraded_no_results"
            ctx["search_health"]["note"] = "web_search returned 0 results — engines blocked or SearXNG unreachable"
        else:
            # Heuristic: if top results contain dictionary/wikipedia for a tech query, degraded
            titles = " ".join((r.get("title") or "").lower() for r in probe[:3])
            dictionary_signals = ["definition", "wikipedia", "merriam-webster", "free dictionary", "oxford"]
            n_dict = sum(1 for sig in dictionary_signals if sig in titles)
            if n_dict >= 1:
                ctx["search_health"]["status"] = "degraded_dictionary"
                ctx["search_health"]["note"] = f"web_search returned dictionary-quality results for tech query — only generic engines responding (likely bing-only)"
                ctx["search_health"]["sample_titles"] = [r.get("title", "")[:80] for r in probe[:3]]
            else:
                ctx["search_health"]["status"] = "ok"
                ctx["search_health"]["sample_titles"] = [r.get("title", "")[:80] for r in probe[:3]]
    except Exception as e:
        ctx["search_health"]["status"] = "probe_failed"
        ctx["search_health"]["note"] = f"web_search probe raised: {e}"

    return ctx


def _format_context_block(ctx: Dict) -> Dict[str, str]:
    """Format context dict for prompt template insertion."""
    out = {}

    # Recent proposals
    if ctx.get("recent_proposals"):
        lines = []
        for p in ctx["recent_proposals"]:
            ago = f"{p.get('created_at_ago_min', '?')}m ago"
            lines.append(f"  - {p['id']} [{p.get('category')}] state={p.get('state')} src={p.get('source')} ({ago}): {p.get('title')}")
        out["recent_proposals"] = "\n".join(lines)
    else:
        out["recent_proposals"] = "(none)"

    # Observer stats
    if ctx.get("observer_stats"):
        lines = []
        for name, count in ctx["observer_stats"].items():
            lines.append(f"  - {name}: {count}")
        out["observer_stats"] = "\n".join(lines)
    else:
        out["observer_stats"] = "(none)"

    # Recent failures
    if ctx.get("recent_failures"):
        lines = []
        for f in ctx["recent_failures"]:
            lines.append(f"  - {f.get('id')}: {f.get('query')}  ERROR: {f.get('error')}")
        out["recent_failures"] = "\n".join(lines)
    else:
        out["recent_failures"] = "(none)"

    # Recent shadow
    if ctx.get("recent_shadow"):
        lines = []
        for s in ctx["recent_shadow"]:
            auto = " AUTO-APPLIED" if s.get("auto_applied") else ""
            lines.append(f"  - {s.get('proposal_id')}: verdict={s.get('verdict')}{auto}  title={s.get('proposal_title')}")
        out["recent_shadow"] = "\n".join(lines)
    else:
        out["recent_shadow"] = "(none — quiet shadow queue)"

    # Memory context
    mem = ctx.get("memory_context", "")
    if mem and len(mem.strip()) > 20:
        out["memory_context"] = mem.strip()[:2500]  # cap for prompt budget
    else:
        out["memory_context"] = "(memory query returned nothing relevant)"

    # Web results (if requested)
    if ctx.get("web_results"):
        lines = [f"(query: {ctx.get('web_query', '?')})"]
        for r in ctx["web_results"][:5]:
            lines.append(f"  - {r.get('title', '')[:80]}")
            lines.append(f"    {r.get('url', '')[:100]}")
            lines.append(f"    {r.get('snippet', '')[:200]}")
        out["web_results"] = "\n".join(lines)
    else:
        out["web_results"] = "(not queried this cycle — opt-in via include_web=True)"

    # Cron schedules — Roux's own subsystems and their cadence
    if ctx.get("cron_schedules"):
        lines = []
        for j in ctx["cron_schedules"]:
            interval = j.get("interval_minutes")
            hours = j.get("hours") or []
            sched = f"{interval}min" if interval else (f"at hours {hours}" if hours else "disabled")
            last = (j.get("last_run") or "never")[:19]
            lines.append(f"  - {j['name']:20s} every {sched:>10s}  last_run={last}  runs={j.get('run_count', 0)}")
        out["cron_schedules"] = "\n".join(lines)
    else:
        out["cron_schedules"] = "(schedule data unavailable)"

    # Researcher state — web-scouring findings from the last cron run
    rs = ctx.get("researcher_state") or {}
    if rs:
        lines = []
        lines.append(f"  next_topic: {rs.get('next_topic', '?')}  (topic_index: {rs.get('topic_index', '?')})")
        last_run = rs.get('last_run_at')
        lines.append(f"  last_run:   {last_run if last_run else '(never)'}")
        lines.append(f"  proposals_published_last_run: {rs.get('last_published_proposals', 0)}")
        findings = rs.get('last_findings') or []
        if findings:
            lines.append(f"  last findings ({len(findings)} shown):")
            for f in findings[:5]:
                t = (f.get('title') if isinstance(f, dict) else str(f))[:90]
                lines.append(f"    - {t}")
        out["researcher_state"] = "\n".join(lines)
    else:
        out["researcher_state"] = "(researcher state unavailable)"

    # Fix #3 (2026-05-29): self-state is NO LONGER injected into the decision prompt.
    # It was loop-fuel — the prompt repeatedly directed Roux to ruminate on her own
    # SKIP/PROPOSE pattern + streak, feeding the self-referential loop-confab (and it
    # contradicted Check C, which told her NOT to anchor on that state). The streak data
    # still lives in ctx["your_recent_behavior"] for the CODE-level circuit breaker in
    # run_self_propose_cycle; the DECISION prompt now grounds on EXTERNAL signals only.
    out["your_recent_behavior"] = ""

    # Search infrastructure health — Roux needs to know when web grounding is unreliable
    sh = ctx.get("search_health") or {}
    if sh.get("status") == "ok":
        out["search_health"] = f"  status: OK — web search appears to return real technical content"
    elif sh.get("status") == "degraded_dictionary":
        out["search_health"] = (
            f"  ⚠️  DEGRADED: {sh.get('note', '')}\n"
            f"  IMPLICATION: do NOT trust web_search results or researcher findings as grounded evidence right now.\n"
            f"  sample titles: {sh.get('sample_titles', [])}"
        )
    elif sh.get("status") == "degraded_no_results":
        out["search_health"] = f"  ⚠️  DEGRADED: {sh.get('note', '')}"
    elif sh.get("status") == "probe_failed":
        out["search_health"] = f"  ⚠️  PROBE FAILED: {sh.get('note', '')}"
    else:
        out["search_health"] = "  status: unknown"

    return out


def parse_self_propose_decision(raw: str) -> Tuple[str, Optional[str], str, Optional[int]]:
    """Parse v3's decision output. Returns (decision, topic_or_None, reasoning, next_interval_seconds_or_None).
    next_interval is clamped to [60, 3600] if present; None means caller preserves current."""
    if not raw or not raw.strip():
        return "SKIP", None, "empty model output", None

    m_dec = re.search(r"(?im)^\s*DECISION\s*:\s*(PROPOSE|SKIP)\b", raw)
    decision = m_dec.group(1).upper() if m_dec else "SKIP"

    topic = None
    if decision == "PROPOSE":
        m_topic = re.search(
            r"(?im)^\s*TOPIC\s*:\s*(.*?)(?=^\s*REASONING\s*:|\Z)",
            raw, re.DOTALL,
        )
        if m_topic:
            topic = m_topic.group(1).strip()
        # 2026-05-17 10:18: lowered topic minimum from 100 to 60 chars after observing
        # n=3 parser-killed breakthroughs where Roux's real PROPOSE attempts had topics
        # in the 70-90 char range (specific & actionable but concise). The 100 floor was
        # over-aggressive; 60 still prevents empty/single-word topics but allows margins.
        if not topic or len(topic) < 60:
            decision = "SKIP"  # malformed PROPOSE → skip safely

    m_reason = re.search(
        r"(?im)^\s*REASONING\s*:\s*(.*?)(?=^\s*NEXT_INTERVAL_SECONDS\s*:|\Z)",
        raw, re.DOTALL,
    )
    reasoning = m_reason.group(1).strip() if m_reason else ""

    # Parse optional NEXT_INTERVAL_SECONDS — Roux's own cadence suggestion
    next_interval = None
    m_int = re.search(r"(?im)^\s*NEXT_INTERVAL_SECONDS\s*:\s*(\d+)", raw)
    if m_int:
        try:
            raw_int = int(m_int.group(1))
            # Clamp to safety bounds [60, 3600]
            next_interval = max(60, min(3600, raw_int))
        except ValueError:
            next_interval = None

    return decision, topic, reasoning, next_interval


def parse_schedule_changes(raw: str) -> List[Dict]:
    """Parse SCHEDULE_CHANGES block from Roux's output. Returns list of dicts:
       [{job: str, interval_minutes: int, reasoning: str}, ...]
    Values are NOT clamped here — that's the API endpoint's job."""
    changes = []
    m = re.search(r"(?ims)^\s*SCHEDULE_CHANGES\s*:\s*\n(.+?)(?=\n\s*\n|\n\s*[A-Z_]+\s*:|\Z)", raw)
    if not m:
        return changes
    block = m.group(1)
    # Each line is like: - job: <name>, interval_minutes: <int>, reasoning: <text>
    for line in block.splitlines():
        line = line.strip()
        if not line or not line.startswith("-"):
            continue
        m_job = re.search(r"job\s*:\s*([A-Za-z_][A-Za-z0-9_]*)", line)
        m_int = re.search(r"interval_minutes\s*:\s*(\d+)", line)
        m_reason = re.search(r"reasoning\s*:\s*(.+?)$", line)
        if m_job and m_int:
            changes.append({
                "job": m_job.group(1),
                "interval_minutes": int(m_int.group(1)),
                "reasoning": m_reason.group(1).strip() if m_reason else "",
            })
    return changes


async def run_self_propose_cycle(force: bool = False) -> Dict:
    """Run one cycle. Returns dict with outcome.
    force=True bypasses throttle checks (for manual trigger / testing)."""
    from shared.llm import llm_generate
    from shared.companion import _strip_thinking

    now = time.time()
    state = _load_state()
    state = _reset_daily_if_needed(state)

    outcome = {
        "started_at": now,
        "force": force,
        "autonomous_enabled": AUTONOMOUS_ENABLED,
    }

    # Throttle check (unless force)
    if not force:
        ok, reason = _check_throttles(state, now)
        if not ok:
            outcome["decision"] = "THROTTLED"
            outcome["reason"] = reason
            logger.info(f"SELF-PROPOSE throttled: {reason}")
            state["last_run_at"] = now
            _save_state(state)
            return outcome

    # Gather context (Tier 1 2026-05-17: now includes memory_context + web_results by default).
    # Web query derived from most recent distinct failure title if any; else a topical default.
    try:
        # Sneak peek at recent failures to derive a more relevant web query
        peek_ctx = await gather_context(max_items=5, include_memory=False, include_web=False)
        failures = peek_ctx.get("recent_failures") or []
        web_q = None
        if failures:
            web_q = (failures[0].get("query") or "")[:120]
        if not web_q:
            web_q = "autonomous AI agent self-modifying proposal verification patterns"
        ctx = await gather_context(include_memory=True, include_web=True, web_query=web_q)
    except Exception as e:
        outcome["decision"] = "ERROR"
        outcome["error"] = f"gather_context failed: {e}"
        state["last_run_at"] = now
        _save_state(state)
        return outcome
    outcome["context_summary"] = {
        "n_proposals": len(ctx.get("recent_proposals", [])),
        "n_failures": len(ctx.get("recent_failures", [])),
        "n_shadow": len(ctx.get("recent_shadow", [])),
    }

    # Build + send prompt
    formatted = _format_context_block(ctx)
    prompt = SELF_PROPOSE_PROMPT_TEMPLATE.format(**formatted)
    try:
        r = await llm_generate("companion", prompt=prompt, max_tokens=1500, temperature=0.6, timeout=300)
    except Exception as e:
        outcome["decision"] = "ERROR"
        outcome["error"] = f"llm_generate failed: {e}"
        state["last_run_at"] = now
        _save_state(state)
        return outcome

    if not getattr(r, "success", False):
        outcome["decision"] = "ERROR"
        outcome["error"] = f"llm returned failure: {getattr(r, 'error', '?')}"
        state["last_run_at"] = now
        _save_state(state)
        return outcome

    raw = _strip_thinking(r.text or "")
    decision, topic, reasoning, next_interval = parse_self_propose_decision(raw)

    # Post-process SKIP enforcement (shipped 2026-05-20).
    # If decision=SKIP and reasoning matches a forbidden shape, force-flip to PROPOSE
    # with an auto-injected topic. Welfare-aware: prompt has no contradiction; this is
    # an output-shape constraint applied AFTER the model outputs.
    skip_enforcement_action = None
    if decision == "SKIP":
        ok, matched_label, matched_text = validate_skip_reasoning(reasoning)
        if not ok:
            original_reasoning = reasoning
            if not _has_external_trigger(ctx):
                # No outward signal this cycle → force-flipping would MANUFACTURE a
                # self-referential loop-meta proposal (the documented pollution: "propose
                # about why my SKIP was boilerplate"). Suppress the flip — keep it a genuine
                # cooldown SKIP. (Check-C-in-code, 2026-05-28, DJ-approved loose-enforce.)
                skip_enforcement_action = {
                    "fired": False,
                    "suppressed_force_flip": True,
                    "matched_label": matched_label,
                    "matched_text": matched_text,
                    "reason": "no external trigger — cooldown SKIP instead of manufacturing loop-meta",
                }
                _log_skip_enforcement({
                    "type": "force_flip_suppressed",
                    "matched_label": matched_label,
                    "matched_text": matched_text,
                    "original_reasoning": original_reasoning,
                })
                _log_circuit_breaker({
                    "mode": "force_flip_suppressed",
                    "matched_label": matched_label,
                    "original_reasoning": original_reasoning[:300],
                })
                logger.warning(
                    f"FORCE-FLIP SUPPRESSED: SKIP matched '{matched_label}' but no external "
                    f"trigger this cycle → cooldown SKIP (not manufacturing loop-meta)"
                )
                # decision stays "SKIP"
            else:
                # External signal present → the force-flip is legitimate (propose about the
                # real meta-pattern grounded in an outward trigger). Original behavior (2026-05-20).
                # Build an auto-injected topic that names the meta-pattern and explains why
                # the model's SKIP attempt was rejected. This becomes a real PROPOSE that
                # goes through governance routing → shadow → bigbrain.
                injected_topic = (
                    f"Roux's SKIP reasoning matched forbidden shape '{matched_label}' "
                    f"(text: {matched_text!r}). The post-process enforcement (shipped 2026-05-20) "
                    f"force-flipped this cycle to PROPOSE. The right action when this pattern fires "
                    f"is to propose specifically about the meta-pattern: WHY did the loop's SKIP "
                    f"reasoning land on a forbidden shape this cycle? Is there a structural reason "
                    f"(stuck pending work, paraphrase-drift, gather_context surfacing the same signals), "
                    f"or is the proposal just to investigate and document the recurrence? Either way, "
                    f"propose substantively now rather than SKIP. Original SKIP reasoning preserved in "
                    f"the enforcement audit log."
                )
                injected_reasoning = (
                    f"[Post-process force-flip from SKIP] Original SKIP reasoning matched forbidden "
                    f"pattern '{matched_label}'. See state/skip_enforcement_log.jsonl for original "
                    f"reasoning + matched text. This force-flip is a code-level enforcement (Gate C "
                    f"v4 / post-process) — no prompt contradiction involved."
                )
                decision = "PROPOSE"
                topic = injected_topic
                reasoning = injected_reasoning
                skip_enforcement_action = {
                    "fired": True,
                    "matched_label": matched_label,
                    "matched_text": matched_text,
                    "original_reasoning": original_reasoning,
                    "injected_topic": injected_topic,
                }
                _log_skip_enforcement({
                    "type": "skip_force_flip",
                    "matched_label": matched_label,
                    "matched_text": matched_text,
                    "original_reasoning": original_reasoning,
                    "injected_topic": injected_topic,
                })
                logger.warning(
                    f"SKIP ENFORCEMENT FIRED: SKIP reasoning matched '{matched_label}' "
                    f"(text={matched_text!r}); force-flipped to PROPOSE (external trigger present)"
                )

    outcome["raw_response"] = raw
    outcome["decision"] = decision
    outcome["reasoning"] = reasoning
    if skip_enforcement_action:
        outcome["skip_enforcement"] = skip_enforcement_action

    # Apply Roux's interval suggestion (if any) — bounded autonomy over her own polling
    if next_interval is not None:
        prior_interval = state.get("config", {}).get("min_interval_secs", DEFAULT_MIN_INTERVAL_SECS)
        if next_interval != prior_interval:
            state.setdefault("config", {})["min_interval_secs"] = next_interval
            outcome["interval_change"] = {
                "from": prior_interval,
                "to": next_interval,
                "delta": next_interval - prior_interval,
            }
            logger.info(
                f"SELF-PROPOSE INTERVAL CHANGE: {prior_interval}s -> {next_interval}s "
                f"(roux's request)"
            )
        else:
            outcome["interval_unchanged"] = next_interval

    # Phase 2: Apply Roux's SCHEDULE_CHANGES (her pipeline cron control)
    schedule_changes = parse_schedule_changes(raw)
    if schedule_changes:
        outcome["schedule_changes_requested"] = schedule_changes
        applied = []
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                for ch in schedule_changes:
                    try:
                        async with session.post(
                            f"http://localhost:8012/cron/{ch['job']}/interval",
                            json={
                                "minutes": ch["interval_minutes"],
                                "changed_by": "roux",
                                "reasoning": ch["reasoning"],
                            },
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as resp:
                            applied.append(await resp.json())
                    except Exception as e:
                        applied.append({"job": ch["job"], "error": str(e)})
        except Exception as e:
            outcome["schedule_changes_error"] = str(e)
        outcome["schedule_changes_applied"] = applied
        logger.info(f"SELF-PROPOSE SCHEDULE CHANGES: {len(applied)} cron interval(s) updated by roux")

    history_entry = {
        "run_at": now,
        "decision": decision,
        "reasoning": reasoning[:300],
        "topic_excerpt": (topic or "")[:200],
        "next_interval_seconds": next_interval,
    }

    if decision == "SKIP":
        logger.info(f"SELF-PROPOSE SKIP: {reasoning[:120]}")
        state["last_run_at"] = now
        state["history"] = (state.get("history") or [])[-50:] + [history_entry]
        _save_state(state)
        return outcome

    # decision == PROPOSE
    outcome["topic"] = topic

    # Check C in CODE (2026-05-28, DJ-approved loose-enforce + strict-shadow-log).
    # If this PROPOSE is self-referential loop-meta, decide if it's grounded in any
    # external signal. LOOSE: brake only when nothing external is happening this cycle.
    # STRICT (shadow only): record when the topic doesn't actually reference a present
    # signal — what a tighter rule WOULD catch. Builds trust data before we tighten.
    if _is_self_referential_topic(topic):
        loose_ok = _has_external_trigger(ctx)
        strict_ok = _topic_grounded_in_trigger(topic, ctx)
        if not strict_ok:
            _log_circuit_breaker({
                "mode": "strict_shadow",
                "would_brake": True,
                "enforced_by_loose": (not loose_ok),
                "topic": (topic or "")[:200],
                "loose_external_trigger": loose_ok,
            })
        if not loose_ok:
            outcome["decision"] = "SKIP_CIRCUIT_BREAKER"
            outcome["reason"] = "self-referential loop-meta topic with no external trigger this cycle"
            outcome["circuit_breaker"] = {"strict_would_brake": (not strict_ok)}
            logger.warning(
                f"CIRCUIT BREAKER (Check-C code): braked self-referential PROPOSE with no "
                f"external trigger — {(topic or '')[:120]}"
            )
            _log_circuit_breaker({
                "mode": "enforced",
                "topic": (topic or "")[:200],
                "reasoning_excerpt": (reasoning or "")[:200],
            })
            history_entry["decision"] = "SKIP_CIRCUIT_BREAKER"
            history_entry["topic_fingerprint"] = _topic_fingerprint(topic)
            state["last_run_at"] = now
            state["history"] = (state.get("history") or [])[-50:] + [history_entry]
            _save_state(state)
            return outcome

    # Topic cooldown check
    cd_ok, cd_reason = _check_topic_cooldown(state, topic, now)
    if not cd_ok:
        outcome["decision"] = "SKIP_COOLDOWN"
        outcome["reason"] = cd_reason
        logger.info(f"SELF-PROPOSE skipped (cooldown): {cd_reason}")
        history_entry["decision"] = "SKIP_COOLDOWN"
        history_entry["topic_fingerprint"] = _topic_fingerprint(topic)
        state["last_run_at"] = now
        state["history"] = (state.get("history") or [])[-50:] + [history_entry]
        _save_state(state)
        return outcome

    # TOPIC-DIVERSITY GATE (Tier 2, 2026-05-31) — the same code-level embedding-cosine
    # gate reflection uses (shared/topic_diversity.py). Compares this topic against recent
    # authored topics across BOTH loops (author_substr "roux" matches roux_reflection +
    # roux_self_propose) and throttles near-dups before they reach the pipe. This is the
    # clean supersession of the SKIP-enforcement force-flip + Check-C machinery for
    # dup-suppression: where the old force-flip could MANUFACTURE a self-loop proposal,
    # the gate simply throttles repetition (self OR cross-loop).
    if os.environ.get("ROUX_TOPIC_DIVERSITY", "1") != "0":
        try:
            from shared.topic_diversity import assess_topic
            div = assess_topic(topic, author_substr="roux")
            if div.get("redundant"):
                outcome["decision"] = "PROPOSE_TOPIC_THROTTLED"
                outcome["diversity"] = div
                history_entry["decision"] = "PROPOSE_TOPIC_THROTTLED"
                history_entry["topic_fingerprint"] = _topic_fingerprint(topic)
                logger.info(
                    f"SELF-PROPOSE THROTTLED — topic too similar to a recent one "
                    f"({div.get('method')} {div.get('score')} >= {div.get('threshold')}); skip. "
                    f"matched: {(div.get('matched_text') or '')[:80]}"
                )
                state["last_run_at"] = now
                state["history"] = (state.get("history") or [])[-50:] + [history_entry]
                _save_state(state)
                return outcome
        except Exception as e:
            logger.warning(f"topic-diversity gate errored, allowing propose: {e}")

    # DECOUPLE (Tier 2, 2026-05-31) — ENQUEUE to the draft_queue instead of a synchronous
    # POST /propose (which had a 600s timeout and could freeze the single-threaded cron
    # loop — the exact freeze class fixed for reflection on 2026-05-30). The proposal_drafter
    # cron claims it and drafts via v5.3 in a background thread, off the critical path.
    from shared.draft_queue import enqueue_draft
    draft_id = enqueue_draft(topic, author="roux_self_propose")
    if draft_id:
        outcome["decision"] = "PROPOSE_QUEUED"
        outcome["draft_id"] = draft_id
        history_entry["decision"] = "PROPOSE_QUEUED"
        history_entry["draft_id"] = draft_id
        history_entry["topic_fingerprint"] = _topic_fingerprint(topic)
        state["last_propose_at"] = now
        state["today_count"] = state.get("today_count", 0) + 1
        logger.info(
            f"SELF-PROPOSE QUEUED draft {draft_id} — "
            f"today={state['today_count']}/{state.get('config', {}).get('max_per_day', DEFAULT_MAX_PER_DAY)}"
        )
    else:
        outcome["decision"] = "PROPOSE_DEDUPED"
        history_entry["decision"] = "PROPOSE_DEDUPED"
        logger.info("SELF-PROPOSE deduped (topic already pending/drafting in the queue)")

    state["last_run_at"] = now
    state["history"] = (state.get("history") or [])[-50:] + [history_entry]
    _save_state(state)
    return outcome
