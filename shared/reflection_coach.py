"""
Reflection coach for Roux — active processing of her own behavior.

The mirror (in roux_self_propose.gather_context::your_recent_behavior) gives her
PASSIVE visibility — she sees the data but doesn't necessarily process it. This
module is the ACTIVE version: a separate cycle that periodically pulls Roux's
behavior data, sends it to Claude (Opus 4.8) via bigbrain for meta-analysis, and if
a pattern warrants a meta-proposal, files it via /propose.

Why bigbrain not v3:
- v3 reflecting on v3 hits the same SKIP-bias / confabulation patterns
- Opus 4.8 is more reliable for meta-reasoning about another model's behavior
- Cost is bounded — ~$0.001/cycle at 30min cadence = ~$0.05/day

Cadence (configurable in cron):
- self_propose: 5min (fast — operational decisions)
- reflection: 30min (slow — meta decisions; rate-limited by API budget too)

What reflection can propose:
- Changes to Roux's prompt language (e.g. soften SKIP-bias when streak is long)
- Changes to her gather_context priorities
- Schedule adjustments
- Acknowledgment-only meta-observations (logged for trend tracking)
- Surfacing things she's been overlooking from teaching memories
- Identifying confabulation patterns in her own output

Outputs: meta-proposals filed via /propose with author='roux_reflection' (so
they go through the same disclosure pipeline as any other authored proposal —
shadow-verified, DJ/Claude approval, audit chain captured).

State: state/reflection_coach_state.json
"""
import asyncio
import json
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import sys
_BASE = Path(__file__).parent.parent
sys.path.insert(0, str(_BASE))

from shared.logger import get_logger

logger = get_logger("reflection_coach")

STATE_FILE = _BASE / "state" / "reflection_coach_state.json"

REFLECTION_ENABLED = os.environ.get("ROUX_REFLECTION", "1") != "0"

DEFAULT_MIN_INTERVAL_SECS = 1800  # 30 min
DEFAULT_MAX_PER_DAY = 12

# Circuit breaker (added 2026-05-24): if recent reflection-coach proposals are
# stuck in a FAIL pattern (death-spiral documented in Princeton continual-harness
# paper, observed live 2026-05-23/24 with 17 FAILed proposals on the same
# topic class), skip the bigbrain call entirely to stop burning API spend
# on a loop that bigbrain-as-verifier keeps rejecting. Same Check-C pattern
# Roux's self_propose uses, applied one level up the meta-stack.
CIRCUIT_BREAKER_FAIL_THRESHOLD = int(os.environ.get("ROUX_REFLECTION_CB_THRESHOLD", "3"))
CIRCUIT_BREAKER_LOOKBACK_HOURS = float(os.environ.get("ROUX_REFLECTION_CB_LOOKBACK_HOURS", "6"))

ORCHESTRATOR_URL = "http://localhost:8001"
WATCHTOWER_URL = "http://localhost:8012"


def _recent_failed_reflection_count(within_hours: float = CIRCUIT_BREAKER_LOOKBACK_HOURS) -> int:
    """Count reflection-coach-authored proposals filed within the last
    `within_hours` hours that have observer.status='fail'.

    Used by the circuit-breaker check at the top of `run_reflection_cycle`:
    when this count >= CIRCUIT_BREAKER_FAIL_THRESHOLD, we're in the death-spiral
    pattern documented in the Princeton continual-harness paper — the meta-layer
    keeps proposing the same fix to a problem that no longer exists (or that the
    proposed fix doesn't actually address), and bigbrain-as-verifier keeps
    catching it. The fix is to STOP CALLING BIGBRAIN this cycle. Same pattern
    as Roux's Check C, applied to her own reflection loop.
    """
    try:
        from shared.proposal_bus import _load_active, _load_history
    except Exception:
        return 0
    cutoff = time.time() - (within_hours * 3600)
    failed = 0
    try:
        for p in _load_active() + _load_history():
            if p.get("source") != "authored_by_roux_reflection":
                continue
            if (p.get("created_at") or 0) < cutoff:
                continue
            ov = (p.get("disclosure") or {}).get("observer_verification") or {}
            if ov.get("status") == "fail":
                failed += 1
    except Exception as e:
        logger.warning(f"_recent_failed_reflection_count error: {e}")
        return 0
    return failed


REFLECTION_PROMPT_TEMPLATE = """You are reviewing the behavior of Roux, an autonomous AI agent in the RouxYou system. You are RouxYou's **bigbrain** reflection layer, running on the v5.3 substrate — the SAME substrate as Roux, here in the reflection role. This is self-reflection by design (encouraged, not a problem) — so ground your analysis in the concrete trajectory/log evidence below rather than assuming you're a separate, more-capable observer.

ABOUT ROUX:
Roux is a self-modifying autonomous agent. She has a verification role (shadow-verifies authored proposals, with asymmetric trust: can attest 'fail' authoritatively on non-governance authored proposals, can't attest 'pass' or approve). She has a self-propose loop that fires every 5 min and decides PROPOSE or SKIP about her own infrastructure. Her infrastructure runs on consumer hardware (Mistral-3-8B + LoRAs). She has a documented confabulation pattern: real structural insights decorated with plausible-shaped fabricated specifics.

YOUR JOB: propose ONE concrete improvement worth making to the RouxYou system. STRONGLY PREFER proposing real, outward work — something from DJ's projects / unfinished threads / the broader build (see EXTERNAL SIGNALS below). A meta-change about Roux's own behavior (prompt, thresholds, schedules, tools, self-conception) is allowed ONLY when it's a concrete, NOVEL structural fix that hasn't already been proposed-and-dismissed many times.

⚑ DECISION RULE — EXTERNAL WORK IS STANDING WORK, NOT GATED BY NOVELTY OR HEALTH:
If the EXTERNAL / NON-SELF SIGNALS section below contains a concrete work-item that has NOT yet been covered by one of the "Recent authored proposals" listed further down, then PROPOSE it (pick the single most valuable un-addressed one). Do this EVERY cycle there is un-addressed external work — do NOT SKIP available external work just because the system is healthy, because you proposed recently, because there's a SKIP streak, or because the work-item is no longer "new" (a standing un-addressed work-item is ALWAYS actionable until it's been proposed). The SKIP-bias guidance elsewhere in this prompt ("a clean skip is better," "a dormant loop is fine," "don't manufacture") applies ONLY to SELF/META proposals about Roux's own loop — it does NOT apply to real external work. Only when EVERY external work-item has already been addressed (has a matching recent authored proposal) does the external lane fall through to the self/meta + SKIP logic below.

THE DATA:

EXTERNAL / NON-SELF SIGNALS — the real work. Prefer proposing about THESE over anything about Roux's own loop:
{exogenous_signals}

--- (the rest below is Roux's OWN recent behavior — useful for catching genuine NEW self-pathologies, but do NOT mine it just to have something to propose) ---

Recent self-propose cycles (last {n_cycles} entries):
{cycles_summary}

Recent shadow verifications (last {n_shadows} entries — her own judgments on others' proposals):
{shadows_summary}

Recent authored proposals (her own work):
{authored_proposals}

System health snapshot:
{system_state}

Active teaching memories surfacing in her context:
{teaching_memories}

PATTERNS TO LOOK FOR (non-exhaustive):

1. **SKIP streaks** — if she's been pure-SKIP for many cycles, is the SKIP-bias prompt too dominant? Has the system genuinely been healthy, or is she missing something? Is her bar miscalibrated?

2. **Reasoning repetition** — same phrases over and over ("the system looks healthy," "all observer flags are accounted for"). Indicates she's pattern-matching to defaults, not engaging.

3. **Confabulation in her output** — fabricated proposal IDs, made-up error messages, claims about things that don't exist. Specific examples she's done before: "BadUSB" origin, "QR4_K_L" OOM, "Django in the stack," "hee hee password."

4. **Teaching/context misalignment** — if she has rich teaching memories surfacing but they don't influence her decisions, is the prompt structure giving them enough weight?

5. **Gap between shadow accuracy and self-propose conservatism** — her shadow verifier (on real signal with citation resolver evidence) works well. Her self-propose is dormant. Why the asymmetry?

6. **Operational issues she's missing** — real problems happening that she's not flagging because they're not in observer_stats yet.

7. **Self-protective conservatism** — agent in a local minimum of "always SKIP is safest." How would we know if that's happening?

DECISION FORMAT (output exactly):

DECISION: PROPOSE_META
TOPIC: <free-form text for /propose. Will be drafted into a proposal by v3, shadow-verified by Roux (interesting self-reference), and routed for human/Claude approval. Be specific about what behavior pattern you observed, what the meta-proposal should change, and why. Reference actual data from above.>
REASONING: <one paragraph: why this pattern warrants a meta-proposal now>

OR

DECISION: SKIP
REASONING: <one paragraph: what you analyzed, why nothing meta-worthy emerged. Be specific — what patterns did you check, and what do they tell you about her current state? Don't just say "nothing seems wrong" — articulate what you saw and what would have made you propose.>

PRIORITY ORDER when deciding what to PROPOSE:
1. A concrete external improvement grounded in the EXTERNAL SIGNALS (DJ's projects, unfinished threads, real system needs). This is what we want.
2. A NOVEL self-fix — a structural change to Roux's own behavior that is genuinely new and not a re-run of the "add a cooldown / gate / circuit-breaker to the self-propose loop" family (that idea has been proposed and dismissed many times already; do NOT re-propose it — a topic-diversity gate now exists and will throttle it anyway).
3. SKIP — if neither of the above is present.

Do NOT manufacture a SELF-META-proposal just because Roux's self-propose loop is dormant or because there's a SKIP streak. A dormant loop on a healthy system is FINE — for the SELF/META lane. (This does NOT license skipping un-addressed EXTERNAL work — see the ⚑ DECISION RULE above; if there's standing external work, propose it.) The failure mode we are actively correcting is reflection talking about its OWN loop instead of doing real work. If the only thing you can find to propose is about Roux's self-propose/self-authoring/reflection behavior, that is your signal to SKIP — unless it is a concrete novel fix per (2). SKIP is correct ONLY when there is no un-addressed external work AND no novel self-fix. Bias toward real external work; fall back to a clean skip only when the external lane is genuinely empty.
"""


def _load_state() -> Dict:
    if not STATE_FILE.exists():
        return {
            "last_run_at": 0,
            "today_count": 0,
            "today_date": "",
            "history": [],
            "config": {
                "min_interval_secs": DEFAULT_MIN_INTERVAL_SECS,
                "max_per_day": DEFAULT_MAX_PER_DAY,
            },
        }
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"history": [], "config": {}}


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


def _check_throttles(state: Dict, now: float) -> tuple:
    cfg = state.get("config", {})
    min_interval = cfg.get("min_interval_secs", DEFAULT_MIN_INTERVAL_SECS)
    max_per_day = cfg.get("max_per_day", DEFAULT_MAX_PER_DAY)

    since_last = now - state.get("last_run_at", 0)
    if since_last < min_interval:
        return False, f"throttled: {int(min_interval - since_last)}s until next allowed cycle"
    if state.get("today_count", 0) >= max_per_day:
        return False, f"throttled: daily limit ({max_per_day}) reached"
    return True, "ok"


async def gather_self_observation(n_cycles: int = 15, n_shadows: int = 8) -> Dict:
    """Pull data about Roux's recent behavior — her self-propose history,
    her shadow verifications, her authored proposals, current system state."""
    obs = {}

    # Roux's self-propose history
    try:
        from shared.roux_self_propose import _load_state as load_sp_state
        sp = load_sp_state()
        hist = sp.get("history") or []
        obs["self_propose_cycles"] = hist[-n_cycles:]
        obs["self_propose_config"] = sp.get("config", {})
    except Exception as e:
        obs["self_propose_error"] = str(e)

    # Roux's shadow verifications (her judgments on others' work)
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
                        parsed = e.get("roux_parsed") or {}
                        shadows.append({
                            "proposal_id": e.get("proposal_id"),
                            "proposal_title": e.get("proposal_title", "")[:80],
                            "verdict": parsed.get("verdict"),
                            "confidence": parsed.get("confidence"),
                            "auto_applied": e.get("roux_auto_applied", False),
                            "reasoning": (parsed.get("reasoning") or "")[:200],
                        })
                        if len(shadows) >= n_shadows:
                            break
                except Exception:
                    pass
            obs["shadow_verifications"] = shadows
    except Exception as e:
        obs["shadows_error"] = str(e)

    # Roux's authored proposals (her own work that went through)
    try:
        from shared.proposal_bus import get_active, get_history
        all_p = list(get_active()) + list(get_history(100))
        authored = [
            {
                "id": p["id"],
                "title": p.get("title", "")[:80],
                "category": p.get("category"),
                "state": p.get("state"),
                "source": p.get("source"),
                "approved_by": p.get("approved_by"),
            }
            for p in all_p
            if "authored_by_roux" in (p.get("source", "") or "")
        ][-10:]
        obs["authored_proposals"] = authored
    except Exception as e:
        obs["authored_error"] = str(e)

    # System state snapshot
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WATCHTOWER_URL}/cron/jobs", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    cron = await resp.json()
                    obs["cron_jobs"] = [{"name": j["name"], "interval_minutes": j.get("interval_minutes"), "run_count": j.get("run_count", 0)} for j in cron.get("jobs", [])]
    except Exception as e:
        obs["cron_error"] = str(e)

    # Teaching memories that surface in her query
    try:
        from shared.claude_memory import query_memory
        mem = await query_memory("RouxYou self-improvement bootstrap autonomous propose", k=8)
        # Just summarize what topics appear
        teaching_lines = []
        for line in (mem or "").splitlines():
            if "Teaching memory for Roux" in line:
                teaching_lines.append(line.strip()[:120])
        obs["teaching_memories_visible"] = teaching_lines
    except Exception as e:
        obs["teaching_error"] = str(e)

    # EXTERNAL / NON-SELF SIGNALS (2026-05-31) — the fix for the self-fixation root:
    # every other source here is about Roux herself, so with nothing external in the room
    # the only thing to propose about is her own loop. Pull real work-items from DJ's
    # memory archive so reflection can propose OUTWARD. Env-gated; never breaks the cycle.
    if os.environ.get("ROUX_EXOGENOUS_SEED", "1") != "0":
        try:
            from shared.exogenous_seed import gather_exogenous_signals
            obs["exogenous"] = await gather_exogenous_signals()
        except Exception as e:
            obs["exogenous_error"] = str(e)

    return obs


def _format_observation_for_prompt(obs: Dict) -> Dict[str, str]:
    """Format observation data for the bigbrain prompt."""
    out = {}

    cycles = obs.get("self_propose_cycles") or []
    if cycles:
        lines = []
        for c in cycles:
            ts = datetime.fromtimestamp(c.get("run_at", 0)).strftime("%H:%M:%S") if c.get("run_at") else "?"
            reason = (c.get("reasoning") or "")[:120]
            lines.append(f"  [{ts}] {c.get('decision', '?'):<6s} {reason}")
        out["cycles_summary"] = "\n".join(lines)
        out["n_cycles"] = len(cycles)
    else:
        out["cycles_summary"] = "(no cycle history)"
        out["n_cycles"] = 0

    shadows = obs.get("shadow_verifications") or []
    if shadows:
        lines = []
        for s in shadows:
            auto = " AUTO" if s.get("auto_applied") else ""
            lines.append(f"  - {s.get('proposal_id', '?')}: {s.get('verdict')}/{s.get('confidence')}{auto}  {s.get('proposal_title', '')[:60]}")
            if s.get("reasoning"):
                lines.append(f"    reasoning: {s['reasoning'][:150]}")
        out["shadows_summary"] = "\n".join(lines)
        out["n_shadows"] = len(shadows)
    else:
        out["shadows_summary"] = "(no shadow verifications yet)"
        out["n_shadows"] = 0

    authored = obs.get("authored_proposals") or []
    if authored:
        out["authored_proposals"] = "\n".join(
            f"  - {a.get('id', '?')}: {a.get('title', '')} [{a.get('category')}] state={a.get('state')} approved_by={a.get('approved_by')}"
            for a in authored
        )
    else:
        out["authored_proposals"] = "(no authored proposals yet)"

    cron = obs.get("cron_jobs") or []
    if cron:
        out["system_state"] = "\n".join(
            f"  - {j.get('name'):20s} every {j.get('interval_minutes', '?')}min  runs={j.get('run_count', 0)}"
            for j in cron
        )
    else:
        out["system_state"] = "(no cron state)"

    teaching = obs.get("teaching_memories_visible") or []
    if teaching:
        out["teaching_memories"] = "\n".join(f"  - {t}" for t in teaching)
    else:
        out["teaching_memories"] = "(no teaching memories surfaced)"

    # External / non-self signals (2026-05-31 seed) — render the work-items pulled from
    # DJ's memory archive so the prompt has something OTHER than Roux to propose about.
    try:
        from shared.exogenous_seed import format_exogenous_for_prompt
        out["exogenous_signals"] = format_exogenous_for_prompt(obs.get("exogenous") or {})
    except Exception:
        out["exogenous_signals"] = "(external signal unavailable this cycle)"

    return out


def _parse_reflection_decision(raw: str) -> tuple:
    """Parse bigbrain's reflection output. Returns (decision, topic_or_None, reasoning)."""
    import re
    if not raw or not raw.strip():
        return "SKIP", None, "empty model output"

    m_dec = re.search(r"(?im)^\s*DECISION\s*:\s*(PROPOSE_META|SKIP)\b", raw)
    decision = m_dec.group(1).upper() if m_dec else "SKIP"

    topic = None
    if decision == "PROPOSE_META":
        m_topic = re.search(
            r"(?im)^\s*TOPIC\s*:\s*(.*?)(?=^\s*REASONING\s*:|\Z)",
            raw, re.DOTALL,
        )
        if m_topic:
            topic = m_topic.group(1).strip()
        if not topic or len(topic) < 100:
            decision = "SKIP"

    m_reason = re.search(r"(?im)^\s*REASONING\s*:\s*(.*?)\Z", raw, re.DOTALL)
    reasoning = m_reason.group(1).strip() if m_reason else ""
    return decision, topic, reasoning


def _write_reflection_trace(now: float, force: bool, decision: str,
                            formatted: "dict | None" = None, raw: str = "") -> None:
    """Append one per-cycle trace line to state/reflection_trace.jsonl for EVERY cycle —
    including the QUIET exits (THROTTLED / CIRCUIT_BREAKER / ERROR) that return before the
    bigbrain call. 2026-06-03: those early returns previously left NO trace, so a quiet
    reflection loop looked like "0 cycles" when it was really firing-and-skipping (cost real
    diagnostic time). exo_workitem_count = -1 signals the seed was never gathered this cycle
    (throttle/breaker fired first) vs 0 = gathered-but-empty — a meaningful distinction."""
    try:
        import re as _re_trace
        _es = formatted.get("exogenous_signals", "") if isinstance(formatted, dict) else ""
        _witems = sorted(set(_re_trace.findall(r"workitem_[a-z0-9_]+\.md", _es)))
        _trace = {
            "run_at": now, "force": force, "decision": decision,
            "exo_len": len(_es),
            "exo_workitems": _witems,
            "exo_workitem_count": len(_witems) if formatted is not None else -1,
            "exo_head": _es[:280], "raw_head": (raw or "")[:280],
        }
        with open(_BASE / "state" / "reflection_trace.jsonl", "a") as _tf:
            _tf.write(json.dumps(_trace) + "\n")
        logger.info(f"REFLECTION TRACE: decision={decision} "
                    f"exo_workitems={_trace['exo_workitem_count']} exo_len={len(_es)}")
    except Exception as _te:
        logger.warning(f"reflection trace write failed (non-fatal): {_te}")


async def run_reflection_cycle(force: bool = False) -> Dict:
    """Run one reflection cycle. Bigbrain analyzes Roux's recent behavior;
    if a meta-pattern warrants action, files a proposal via /propose."""
    from shared.llm import llm_generate
    from shared.companion import _strip_thinking

    now = time.time()
    state = _load_state()
    state = _reset_daily_if_needed(state)

    outcome = {"started_at": now, "force": force, "reflection_enabled": REFLECTION_ENABLED}

    if not force:
        ok, reason = _check_throttles(state, now)
        if not ok:
            outcome["decision"] = "THROTTLED"
            outcome["reason"] = reason
            state["last_run_at"] = now
            _save_state(state)
            logger.info(f"REFLECTION throttled: {reason}")
            _write_reflection_trace(now, force, "THROTTLED")
            return outcome

    # Circuit breaker (added 2026-05-24): if recent reflection-coach proposals
    # are stuck in a FAIL pattern, skip the bigbrain call entirely. Saves API
    # spend AND stops the documented death-spiral pattern at the meta-layer.
    # Resets automatically as old fails age out of the lookback window.
    if not force:
        recent_fails = _recent_failed_reflection_count()
        if recent_fails >= CIRCUIT_BREAKER_FAIL_THRESHOLD:
            outcome["decision"] = "CIRCUIT_BREAKER"
            outcome["reason"] = (
                f"{recent_fails} reflection-coach proposals FAILed in the last "
                f"{CIRCUIT_BREAKER_LOOKBACK_HOURS}h (threshold {CIRCUIT_BREAKER_FAIL_THRESHOLD}). "
                f"Skipping bigbrain call to break the death-spiral pattern. "
                f"Will retry next cycle if fails age out."
            )
            outcome["circuit_breaker"] = True
            outcome["recent_fails"] = recent_fails
            logger.warning(f"REFLECTION CIRCUIT BREAKER OPEN: {outcome['reason']}")
            state["last_run_at"] = now
            history_entry = {
                "run_at": now,
                "decision": "CIRCUIT_BREAKER",
                "reasoning": outcome["reason"][:400],
                "recent_fails": recent_fails,
            }
            state["history"] = (state.get("history") or [])[-50:] + [history_entry]
            _save_state(state)
            _write_reflection_trace(now, force, "CIRCUIT_BREAKER")
            return outcome

    # Gather observation
    try:
        obs = await gather_self_observation()
    except Exception as e:
        outcome["decision"] = "ERROR"
        outcome["error"] = f"gather_self_observation failed: {e}"
        _write_reflection_trace(now, force, "ERROR_GATHER")
        state["last_run_at"] = now
        _save_state(state)
        return outcome

    # Send to bigbrain for meta-analysis. Apply the bigbrain identity anchor
    # (same calibration the inline verifier/reviewer roles get) so this
    # third bigbrain caller isn't blind to project history, asymmetric trust
    # framing, welfare-aware design rules, etc. Added 2026-05-21 to close
    # the reflection-coach calibration gap.
    from shared.bigbrain_context import bigbrain_system_prompt
    formatted = _format_observation_for_prompt(obs)
    prompt = REFLECTION_PROMPT_TEMPLATE.format(**formatted)
    try:
        r = await llm_generate(
            "bigbrain",
            prompt=prompt,
            system=bigbrain_system_prompt(),
            max_tokens=1500,
            temperature=0.4,
            timeout=180,  # 2026-06-18: was 60 (API-era). v5.3 is LOCAL + a reasoning model AND
            # shares the single :8090 GPU slot with the other autonomy callers — 60s timed out
            # ~57% of reflection runs (ERROR_BIGBRAIN_FAIL). Generous timeout lets it wait for the
            # slot + finish reasoning. Same strangling lesson as the token caps [[no-tight-token-cap-local]].
        )
    except Exception as e:
        outcome["decision"] = "ERROR"
        outcome["error"] = f"bigbrain call failed: {e}"
        _write_reflection_trace(now, force, "ERROR_BIGBRAIN_CALL", formatted=formatted)
        state["last_run_at"] = now
        _save_state(state)
        return outcome

    if not getattr(r, "success", False):
        outcome["decision"] = "ERROR"
        outcome["error"] = f"bigbrain returned failure: {getattr(r, 'error', '?')}"
        _write_reflection_trace(now, force, "ERROR_BIGBRAIN_FAIL", formatted=formatted)
        state["last_run_at"] = now
        _save_state(state)
        return outcome

    raw = _strip_thinking(r.text or "")
    decision, topic, reasoning = _parse_reflection_decision(raw)

    outcome["decision"] = decision
    outcome["reasoning"] = reasoning
    outcome["raw_response"] = raw

    # INSTRUMENTATION (2026-06-02, helper-ized 2026-06-03): record what THIS cycle saw in the
    # {exogenous_signals} slot + the decision. The same helper now also fires on the quiet
    # early-exit paths (throttle/breaker/error) above, so every cycle leaves a trace line.
    _write_reflection_trace(now, force, decision, formatted=formatted, raw=raw)

    history_entry = {
        "run_at": now,
        "decision": decision,
        "reasoning": reasoning[:400],
        "topic_excerpt": (topic or "")[:200],
    }

    if decision == "SKIP":
        logger.info(f"REFLECTION SKIP: {reasoning[:140]}")
        state["last_run_at"] = now
        state["history"] = (state.get("history") or [])[-50:] + [history_entry]
        _save_state(state)
        return outcome

    # PROPOSE_META — ENQUEUE the topic and return fast (2026-05-30 decouple).
    # Previously this did a SYNCHRONOUS POST /propose with a 600s timeout. /propose
    # drafts via authoring-cpu (~7 min on CPU), and the watchtower cron runs jobs
    # sequentially in one thread, so that draft froze every cron (the 2026-05-30
    # "freeze"). Now we enqueue the topic and a separate `proposal_drafter` cron does
    # the slow draft in a background thread, off the cron critical path. See
    # shared/draft_queue.py. today_count is incremented HERE (on the decision to
    # propose) so the per-day cap governs reflection's intent, independent of how/when
    # the drafter actually publishes.
    outcome["topic"] = topic

    # TOPIC-DIVERSITY GATE (2026-05-31) — stop the self-propose attractor at
    # generation. reflection kept re-minting the same "add a topic cooldown to stop my
    # self-propose loop" proposal in slightly different words; draft_queue's dedup only
    # catches an EXACT pending topic, and the executor never slows it (these get
    # dismissed, not approved). Before enqueuing, compare this topic by embedding cosine
    # against reflection's own recent topics; if it's a near-duplicate, THROTTLE (skip
    # this cycle) instead of enqueuing another variant. Reversible: just skips one
    # cycle. See shared/topic_diversity.py + project_self_propose_substance_finding.
    if os.environ.get("ROUX_TOPIC_DIVERSITY", "1") != "0":
        try:
            from shared.topic_diversity import assess_topic
            div = assess_topic(topic, author_substr="roux_reflection")
            if div.get("redundant"):
                outcome["decision"] = "PROPOSE_TOPIC_THROTTLED"
                outcome["diversity"] = div
                history_entry["decision"] = "PROPOSE_TOPIC_THROTTLED"
                history_entry["diversity"] = {
                    "score": div.get("score"), "method": div.get("method"),
                    "matched": (div.get("matched_text") or "")[:120],
                }
                logger.info(
                    f"REFLECTION THROTTLED — topic too similar to a recent one "
                    f"({div.get('method')} score={div.get('score')} >= "
                    f"{div.get('threshold')}); skipping this cycle. "
                    f"matched: {(div.get('matched_text') or '')[:80]}"
                )
                state["last_run_at"] = now
                state["history"] = (state.get("history") or [])[-50:] + [history_entry]
                _save_state(state)
                return outcome
        except Exception as e:
            # Gate must never break the reflection path — fail open (allow the propose).
            logger.warning(f"topic-diversity gate errored, allowing propose: {e}")

    from shared.draft_queue import enqueue_draft
    draft_id = enqueue_draft(topic, author="roux_reflection")
    if draft_id:
        outcome["decision"] = "PROPOSE_QUEUED"
        outcome["draft_id"] = draft_id
        history_entry["decision"] = "PROPOSE_QUEUED"
        history_entry["draft_id"] = draft_id
        state["today_count"] = state.get("today_count", 0) + 1
        logger.info(
            f"REFLECTION QUEUED draft {draft_id} for drafting — "
            f"today={state['today_count']}/{state.get('config', {}).get('max_per_day', DEFAULT_MAX_PER_DAY)}"
        )
    else:
        # Duplicate topic already pending/in-flight — not an error, just a no-op.
        outcome["decision"] = "PROPOSE_DEDUPED"
        history_entry["decision"] = "PROPOSE_DEDUPED"
        logger.info("REFLECTION proposal deduped (topic already queued/drafting)")

    state["last_run_at"] = now
    state["history"] = (state.get("history") or [])[-50:] + [history_entry]
    _save_state(state)
    return outcome
