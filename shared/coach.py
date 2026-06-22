"""
LLM Coach — Proposal Enrichment
=================================
Takes raw heuristic proposals and enriches them with LLM reasoning.

After observers generate proposals the Coach:
  1. Queries episodic memory for related past events
  2. Gets proposal stats for recurrence context
  3. Calls the local LLM (router model) for analysis
  4. Returns enriched proposals with confidence scores, better descriptions,
     priority adjustments, and reasoning

Design:
  - OPTIONAL — if Ollama is down, heuristic proposals pass through unchanged
  - Source field changes from "heuristic" to "coach" when enriched
  - Wired into: shared/proposer.py after run_proposer_full()
"""

import json
import re
import sys
import time
import requests
from typing import List, Dict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.logger import get_logger
from shared.json_extract import extract_json
from config import CONFIG

logger = get_logger("coach")

# 2026-06-18 — Coach backend switched authoring-cpu → v5.3 (llama-server :8090).
#  WHY: (1) authoring-cpu is a tiny CPU LoRA (~6 tok/s) with the OLD coach example baked
#  into its weights → it CONFABULATED enrichments (regurgitated "Worker crashed — 3rd
#  time in 48h" for unrelated proposals, deterministically). (2) v5.3 is the 30B brain,
#  already hot on the GPU at ~38 t/s, far more capable, not corpus-poisoned. config.yaml
#  roles.coach ALREADY intended "llamacpp/roux-v53"; the code just never honored it.
#  v5.3 is a REASONING model (served --reasoning-budget 256): give max_tokens room so the
#  ~256 reasoning tokens don't starve the JSON (the "brain went blank" lesson), then strip
#  the <think> block via the shared extractor. Per-proposal single-object call = easy for
#  it (the OLD batch JSON-array was what caused the historical format failures).
#  Coach is OPTIONAL (degrades to pass-through), fires ~every 5min via task_proposer (GPU-gated).
V53_CHAT_URL   = "http://localhost:8090/v1/chat/completions"  # llama-server, OpenAI-compatible
V53_MODEL      = "roux-v53"
COACH_TIMEOUT  = 90       # PER PROPOSAL; v5.3 reasoning+JSON ~10-15s warm, headroom for load/contention
COACH_NUM_PREDICT = 2048  # 2026-06-18: was 512 (a band-aid for truncation). Local model = tokens free;
                          # generous runaway guard lets v5.3 finish reasoning THEN emit clean JSON instead of
                          # truncating mid-object. json_object grammar still bounds the shape; retry = backstop.
                          # See [[no-tight-token-cap-local]].
COACH_MAX_ATTEMPTS = 3    # v5.3's reasoning intermittently leaks prose into content instead of JSON
                          # (~20% of calls; response_format is bypassed when it does). A retry almost
                          # always lands clean (0.2^3 ≈ 0.8% all-fail). Only the failing calls retry.
MAX_PROPOSALS_PER_BATCH = 6

# Legacy Ollama/authoring-cpu backend (kept for easy revert if v5.3 proves worse).
OLLAMA_URL     = f"{CONFIG.OLLAMA_HOST}/api/chat"
MODEL_NAME     = CONFIG.MODEL_AUTHORING


def _query_memory_for_proposals(proposals: List[Dict]) -> str:
    try:
        from shared.memory import memory
        fragments = []
        seen = set()
        for p in proposals[:MAX_PROPOSALS_PER_BATCH]:
            query = f"[PROPOSAL] {p.get('title', '')}"
            if query in seen:
                continue
            seen.add(query)
            matches = memory.retrieve_relevant(query, limit=2, min_score=2.0)
            for m in matches:
                age_h = (time.time() - m.timestamp) / 3600
                fragments.append(
                    f"- {m.task_query[:80]} | "
                    f"{'Success' if m.success else 'Failed'} | "
                    f"{age_h:.0f}h ago | Utility: {m.utility:.2f}"
                )
        return "RELATED MEMORY:\n" + "\n".join(fragments[:8]) if fragments else "RELATED MEMORY: None found."
    except Exception as e:
        logger.warning(f"Memory query failed: {e}")
        return "RELATED MEMORY: Unavailable."


def _get_stats_context() -> str:
    try:
        from shared.proposal_bus import get_proposal_stats
        stats = get_proposal_stats()
        parts = [f"Total historical proposals: {stats['total']}"]
        if stats.get("recurrences"):
            recur_lines = [
                f"  - \"{r['title']}\" occurred {r['count']}x (last: {r['last_state']})"
                for r in stats["recurrences"][:5]
            ]
            parts.append("Recurring issues:\n" + "\n".join(recur_lines))
        if stats.get("failure_rate", 0) > 0:
            parts.append(f"Overall failure rate: {stats['failure_rate']:.1%}")
        return "\n".join(parts)
    except Exception as e:
        logger.warning(f"Stats query failed: {e}")
        return "Stats: Unavailable."


_COACH_SYSTEM = """You are the Coach for an autonomous agent system called RouxYou.
Analyze ONE system proposal (generated by a heuristic observer) and enrich it.

Provide:
1. confidence: 0.0-1.0 (how likely this is a real issue worth acting on)
2. enriched_description: ONE concise sentence (max ~25 words) naming the likely root cause
3. priority_adjustment: -2 to +2 (0 = keep as-is)
4. reasoning: ONE short sentence explaining your assessment

Be brief — the whole object must be short, valid JSON. Do NOT pad or elaborate.
Base EVERY field strictly on the proposal shown above. NEVER copy or reuse any wording
from the format example below — it is only there to show the JSON shape.
Respond with ONLY a single JSON object — no array, no markdown, no explanation.
Format example (shape only, do not reuse its content):
{"confidence": 0.7, "enriched_description": "<one-sentence root cause for THIS proposal>", "priority_adjustment": 0, "reasoning": "<one short sentence>"}"""


def _build_single_coach_prompt(p: Dict, memory_context: str, stats_context: str):
    user = (
        "Proposal:\n"
        f"  Title: {p.get('title','?')}\n"
        f"  Category: {p.get('category','?')}\n"
        f"  Priority: {p.get('priority','?')}\n"
        f"  Description: {p.get('description','?')}\n"
        f"  Evidence: {p.get('evidence','?')}\n"
        f"  Proposed Action: {p.get('proposed_action','?')}\n\n"
        f"{memory_context}\n\n{stats_context}\n\n/no_think"
    )
    return _COACH_SYSTEM, user


def _parse_one(content: str):
    """Parse a single JSON enrichment object. Uses the shared extractor (drops qwen3
    <think> reasoning + ``` fences, finds the JSON); if the model truncated the JSON,
    regex-salvage the high-value fields rather than dropping the whole enrichment.
    Returns dict or None."""
    try:
        obj = extract_json(content)
        if isinstance(obj, list):
            obj = obj[0] if obj else None
        if isinstance(obj, dict):
            if not any(
                k in obj for k in ("confidence", "enriched_description", "priority_adjustment", "reasoning")
            ):
                for k in ("proposal", "result", "enrichment"):
                    if isinstance(obj.get(k), dict):
                        obj = obj[k]
                        break
            return obj
    except Exception:
        pass  # fall through to salvage
    # Salvage works on the post-reasoning text; drop any leading <think> block first.
    if "</think>" in content:
        content = content.split("</think>")[-1]
    # Salvage from truncated/invalid JSON: pull numeric high-value fields always; take
    # description/reasoning only if they are CLOSED quoted strings (not cut off mid-value).
    salvaged: Dict = {}
    m = re.search(r'"confidence"\s*:\s*([0-9.]+)', content)
    if m:
        try: salvaged["confidence"] = float(m.group(1))
        except ValueError: pass
    m = re.search(r'"priority_adjustment"\s*:\s*(-?[0-9]+)', content)
    if m:
        try: salvaged["priority_adjustment"] = int(m.group(1))
        except ValueError: pass
    m = re.search(r'"enriched_description"\s*:\s*"([^"]{10,})"', content)
    if m:
        salvaged["enriched_description"] = m.group(1)
    m = re.search(r'"reasoning"\s*:\s*"([^"]{3,})"', content)
    if m:
        salvaged["reasoning"] = m.group(1)
    return salvaged or None


def _call_coach_one(p: Dict, memory_context: str, stats_context: str):
    """Enrich ONE proposal via v5.3 (:8090). Returns the enrichment dict or None.

    v5.3 is a reasoning model: ~80% of calls return clean grammar-forced JSON, but ~20%
    leak raw reasoning prose into `content` (response_format gets bypassed) → unparseable.
    We retry those (think=false biases toward clean JSON; json_object forces the shape).
    Infra errors (timeout/connection) bail immediately — only unparseable 200s retry."""
    system_prompt, user_prompt = _build_single_coach_prompt(p, memory_context, stats_context)
    title = p.get("title", "?")[:40]
    for attempt in range(1, COACH_MAX_ATTEMPTS + 1):
        try:
            start = time.time()
            resp = requests.post(
                V53_CHAT_URL,
                json={
                    "model": V53_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    "temperature": 0.2,
                    "stream": False,
                    "max_tokens": COACH_NUM_PREDICT,
                    "think": False,                          # bias off the reasoning channel
                    "response_format": {"type": "json_object"},  # grammar-force JSON shape
                },
                timeout=COACH_TIMEOUT,
            )
            elapsed = time.time() - start
            if resp.status_code != 200:
                logger.warning(f"COACH: v53 {resp.status_code} on '{title}', skipping")
                return None
            content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        except requests.Timeout:
            logger.warning(f"COACH: v53 timed out after {COACH_TIMEOUT}s on '{title}', skipping")
            return None
        except requests.ConnectionError:
            logger.warning("COACH: v53 (:8090) not reachable, skipping enrichment")
            return None
        except Exception as e:
            logger.warning(f"COACH: v53 call failed on '{title}': {e}")
            return None

        parsed = _parse_one(content)
        if parsed:
            tag = f" (attempt {attempt})" if attempt > 1 else ""
            logger.info(f"COACH: '{title}' in {elapsed:.1f}s{tag}")
            return parsed
        if attempt < COACH_MAX_ATTEMPTS:
            logger.info(f"COACH: '{title}' attempt {attempt} unparseable (reasoning leak), retrying")
        else:
            logger.warning(f"COACH: '{title}' unparseable after {COACH_MAX_ATTEMPTS} attempts, skipping")
    return None


def enrich_proposals(proposals: List[Dict]) -> List[Dict]:
    """
    Main entry point. Enriches raw heuristic proposals via LLM.
    Returns proposals unchanged if Ollama is unavailable.
    """
    if not proposals:
        return proposals

    batch = proposals[:MAX_PROPOSALS_PER_BATCH]
    logger.info(f"COACH: Enriching {len(batch)} proposal(s) via {V53_MODEL} (one call each)...")

    memory_context = _query_memory_for_proposals(batch)
    stats_context  = _get_stats_context()

    enriched_count = 0
    for p in batch:
        e = _call_coach_one(p, memory_context, stats_context)
        if not isinstance(e, dict):
            continue
        confidence = e.get("confidence")
        if isinstance(confidence, (int, float)) and 0 <= confidence <= 1:
            p["confidence"] = round(float(confidence), 2)
        enriched_desc = e.get("enriched_description", "")
        if enriched_desc and len(enriched_desc) > 10:
            p["description"] = enriched_desc
        adj = e.get("priority_adjustment", 0)
        if isinstance(adj, (int, float)) and -2 <= adj <= 2:
            new_priority = max(1, min(10, p.get("priority", 5) + int(adj)))
            if new_priority != p.get("priority", 5):
                logger.info(f"COACH: Priority {p.get('priority')}→{new_priority} for: {p.get('title','?')[:50]}")
                p["priority"] = new_priority
        p["source"] = "coach"
        reasoning = e.get("reasoning", "")
        if reasoning:
            p["coach_reasoning"] = reasoning
        enriched_count += 1

    logger.info(f"COACH: Enriched {enriched_count}/{len(batch)} proposals")
    return batch + proposals[MAX_PROPOSALS_PER_BATCH:] if len(proposals) > MAX_PROPOSALS_PER_BATCH else proposals
