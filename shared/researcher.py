"""
WEB RESEARCHER — Research-Driven Proposals
============================================
Searches the web for improvement patterns relevant to RouxYou's
tech stack, then uses the reasoning LLM to evaluate findings
and generate proposals.

Design principles:
  - LOW FREQUENCY — runs once daily (or on-demand)
  - LOW PRIORITY — research proposals are P2-P3
  - HUMAN GATED — every finding needs approval before action
  - LLM FILTERED — raw results → LLM evaluates → publish relevant ones
  - OPTIONAL — if SearXNG or Ollama are down, skip gracefully

Called by: services/watchtower/api.py as a daily cron job
Publishes to: shared/proposal_bus via publish_proposal()
"""

import os
import json
import time
import requests
from typing import List, Dict, Any, Optional
from pathlib import Path

import sys
_BASE = Path(__file__).parent.parent
sys.path.insert(0, str(_BASE))

from shared.logger import get_logger
from shared.json_extract import extract_json
from config import CONFIG

logger = get_logger("researcher")

SEARXNG_URL = CONFIG.SEARXNG_URL
OLLAMA_CHAT_URL = f"{CONFIG.OLLAMA_HOST}/api/chat"
MODEL_NAME = CONFIG.MODEL_REASON
SEARCH_TIMEOUT = 15
LLM_TIMEOUT = 300  # 2026-05-27: covers a cold v4 load (~247s off SATA) + the gen. Warm gen is ~1.4s.
MAX_RESULTS_PER_QUERY = 5
MAX_PROPOSALS_PER_RUN = 2  # 2026-05-17: lowered from 3 — quality over quantity
# 2026-06-03: "good, not a lot" — discovery wants quality + freshness, not frequency. The
# cron interval is clamped to <=60min, so enforce the real cadence here. 8h ≈ 3 runs/day to
# start; step DOWN (more often) if signal is high, step UP if dedup-heavy. Roux-tunable later.
RESEARCH_MIN_INTERVAL_S = int(os.environ.get("ROUX_RESEARCH_MIN_INTERVAL", "28800"))

STATE_FILE = _BASE / "state" / "researcher_state.json"


# === RESEARCH TOPICS ===
# Rotated through — one batch per run, cycling over ~1 week.
#
# 2026-06-03 OVERHAUL: the prior list produced low-signal noise (0/47 proposals ever
# used) partly because its CONTEXT was generic AND in places FACTUALLY WRONG (it claimed
# "RAG uses ChromaDB" — RouxYou uses LanceDB + BM25 — which spawned bogus ChromaDB research).
# Refined below: contexts now match the REAL stack, queries are sharpened to RouxYou's actual
# constraints (16GB VRAM, MoE, local-offline), and a Guardian/home-network topic was added.
#
# SELF-MODIFIABLE: these are the DEFAULT seed. At runtime _load_research_topics() reads
# state/research_topics.json if present, so the live topic set can be edited WITHOUT a code
# change — by DJ, or eventually by Roux herself via a proposal that rewrites that JSON
# (state/ is writable + non-tier-0). Edit the file to refine/lessen/widen the research focus.

DEFAULT_RESEARCH_TOPICS = [
    {
        "focus": "agent_orchestration",
        "queries": [
            "FastAPI multi-agent orchestration patterns 2026",
            "autonomous agent task queue supervisor framework",
        ],
        "context": "RouxYou: FastAPI services; an Orchestrator routes tasks to Coder + Worker; a Watchtower supervisor (tier-0) manages restarts + blue-green deploys.",
        "search": {"engines": "github,duckduckgo,google"},  # orchestration frameworks/patterns live as repos
    },
    {
        "focus": "memory_retrieval",
        "queries": [
            "hybrid BM25 dense vector retrieval reciprocal rank fusion",
            "small CPU cross-encoder reranker RAG offline",
        ],
        "context": "RouxYou memory = LanceDB (vector) + BM25 (lexical) hybrid over ~37K memories, nomic-embed-text CPU embedder. Interested in hybrid fusion + reranking that runs CPU-only (GPU is monopolized by the resident model).",
        "search": {"categories": "news,science"},  # retrieval/reranking research + writeups
    },
    {
        "focus": "local_llm_inference",
        "queries": [
            "new open weight LLM release 2026",
            "MoE quantized model 16GB VRAM local inference",
        ],
        "context": "Single RTX 5060 Ti (16GB). Resident model = Qwen3-30B-A3B MoE Q4 via Vulkan llama-server; coder = qwen2.5-coder:14b (GPU-swapped in). Offline-first. Interested in fitting more capability/context into 16GB — ESPECIALLY new open-weight model releases that could run here (directly or via pinning/offload).",
        "search": {"categories": "news,science", "time_range": "month"},  # FRESH model-release watch
    },
    {
        "focus": "deployment_reliability",
        "queries": [
            "blue green deployment Python zero downtime port swap",
            "process supervisor self-healing restart correctness",
        ],
        "context": "RouxYou: blue-green deploy w/ health checks, Watchtower supervisor (PID-registry + port-reconcile), proposal-based self-healing, rolling snapshots for rollback.",
        "search": {"engines": "github,duckduckgo,google"},  # deploy/supervisor patterns = repos
    },
    {
        "focus": "code_intelligence",
        "queries": [
            "LLM code edit reliability structured output JSON plan",
            "code generation agent AST grounding framework",
        ],
        "context": "RouxYou Coder plans edits as JSON, grounded against an AST codebase index. Pain points = structured-output reliability + grounding plans in real symbols (not phantom).",
        "search": {"engines": "github,duckduckgo,google"},  # code-gen tools/frameworks = repos
    },
    {
        "focus": "agent_safety",
        "queries": [
            "autonomous AI agent kill switch human oversight 2026",
            "agent execution budget rate limiting safety",
        ],
        "context": "RouxYou: tier-0 immutable Watchtower, kill switch, execution budget, trust-ledger graduation gate, human-approval on self-mods. The Guardian-safety spine. (This topic's findings have been the most on-theme — keep it sharp.)",
        "search": {"categories": "news,science"},  # safety research + writeups
    },
    {
        "focus": "guardian_home_network",
        "queries": [
            "Home Assistant MCP server automation",
            "self-hosted local network security monitoring agent",
        ],
        "context": "DJ's Guardian vision: RouxYou as a sovereign home/network guardian — Home Assistant integration, MCP-based home control, local network + security monitoring. (Added 2026-06-03 to widen research toward the Guardian track.)",
        "search": {"engines": "github,duckduckgo,google"},  # HA/MCP integrations + network tools = repos
    },
    {
        "focus": "emerging_patterns",
        "queries": [
            "MCP model context protocol agent tools 2026",
            "autonomous self-improving agent architecture 2026",
        ],
        "context": "RouxYou is a local self-modifying agent. Scanning for new agent/AI-engineering patterns that fit an offline single-GPU self-improvement loop.",
        "search": {"categories": "news,science", "time_range": "month"},  # fresh agent patterns
    },
]

RESEARCH_TOPICS_FILE = _BASE / "state" / "research_topics.json"

def _load_research_topics() -> list:
    """Load the live research topics from state/research_topics.json so the set is
    editable WITHOUT a code change (DJ now, Roux later via a proposal that rewrites the
    JSON). Falls back to DEFAULT_RESEARCH_TOPICS and seeds the file on first run / on any
    malformed file. Each topic must be a dict with non-empty 'focus' + 'queries' (list)
    + 'context'. 2026-06-03."""
    try:
        if RESEARCH_TOPICS_FILE.exists():
            with open(RESEARCH_TOPICS_FILE, "r") as f:
                data = json.load(f)
            valid = [t for t in data if isinstance(t, dict)
                     and t.get("focus") and isinstance(t.get("queries"), list) and t["queries"]
                     and t.get("context")]
            if valid:
                return valid
            logger.warning("research_topics.json present but no valid topics; using defaults")
        # seed the file with defaults so it's there to edit
        RESEARCH_TOPICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RESEARCH_TOPICS_FILE, "w") as f:
            json.dump(DEFAULT_RESEARCH_TOPICS, f, indent=2)
    except Exception as e:
        logger.warning(f"_load_research_topics failed ({e}); using in-code defaults")
        return DEFAULT_RESEARCH_TOPICS
    return DEFAULT_RESEARCH_TOPICS


def _load_state() -> Dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_run": 0, "topic_index": 0, "runs": 0, "findings_total": 0}


def _save_state(state: Dict):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save state: {e}")


def _search_searxng(query: str, search_params: Optional[Dict] = None) -> List[Dict]:
    """Search SearXNG. `search_params` (per-topic, from the topic's optional "search" block in
    state/research_topics.json) tunes HOW each topic searches — the SearXNG "tricks":
      - categories: e.g. "news,science" (default) — the "it" category is Docker/GitHub junk on
        this instance, so we avoid it.
      - time_range: "day"|"week"|"month"|"year" — fresh-only filter (e.g. month → catch new model
        releases, the open-weights-watch use case).
      - engines: e.g. "github" — target a specific engine directly (real repos vs random web).
    2026-06-03: per-topic search strategy. Roux-tunable (it's in the topic JSON)."""
    if not SEARXNG_URL:
        logger.warning("RESEARCHER: SearXNG URL not configured in config.yaml")
        return []
    sp = search_params or {}
    params = {"q": query, "format": "json", "categories": sp.get("categories", "general,news,science")}
    if sp.get("time_range"):
        params["time_range"] = sp["time_range"]
    if sp.get("engines"):
        params["engines"] = sp["engines"]
    try:
        resp = requests.get(
            f"{SEARXNG_URL}/search",
            params=params,
            headers={"User-Agent": "RouxYou/1.0"},
            timeout=SEARCH_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            results = []
            for hit in data.get("results", [])[:MAX_RESULTS_PER_QUERY]:
                results.append({
                    "title": hit.get("title", ""),
                    "url": hit.get("url", ""),
                    "snippet": hit.get("content", "")[:400],
                    "source": hit.get("engine", ""),
                })
            return results
        else:
            logger.warning(f"SearXNG returned {resp.status_code}")
            return []
    except requests.ConnectionError:
        logger.warning("SearXNG not reachable")
        return []
    except Exception as e:
        logger.warning(f"Search failed: {e}")
        return []


def _get_recurrence_context() -> str:
    try:
        from shared.proposal_bus import get_proposal_stats
        stats = get_proposal_stats()
        recurrences = stats.get("recurrences", [])
        if not recurrences:
            return ""
        lines = ["Recent recurring issues in the system:"]
        for r in recurrences[:3]:
            lines.append(f"  - \"{r['title']}\" occurred {r['count']}x (category: {r['category']})")
        return "\n".join(lines)
    except Exception:
        return ""


def _evaluate_with_llm(topic: Dict, all_results: List[Dict], recurrence_context: str) -> List[Dict]:
    if not all_results:
        return []

    results_text = ""
    for i, r in enumerate(all_results):
        results_text += (
            f"\nResult {i+1}:\n"
            f"  Title: {r['title']}\n"
            f"  URL: {r['url']}\n"
            f"  Snippet: {r['snippet']}\n"
        )

    system_prompt = """You are a research analyst for RouxYou. You evaluate web search results and identify findings that could CONCRETELY improve THIS specific system.

CRITICAL — what RouxYou IS:
- A self-modifying autonomous AI agent system running on consumer hardware (single RTX 5060 Ti, 16GB VRAM)
- Local-first: vLLM serving Mistral-3-8B (v3) + Qwen 2.5-1.5B router LoRA; Ollama for coder (qwen2.5-coder:14b-instruct) and embeddings (nomic)
- FastAPI microservices: gateway, orchestrator, coder, worker, memory, watchtower, dashboard (Streamlit)
- Proposal-based self-modification with disclosure rule, observer attestation, asymmetric trust scope
- Episodic memory + RAG, SearXNG for web search, local v5.3 (Qwen3-30B-A3B) for "bigbrain" escalation/verification

CRITICAL — what RouxYou IS NOT (REJECT findings about these — relevance = 0):
- NOT a web frontend / NOT browser-based UI (no CSS, no HTML rendering, no React, no Vue)
- NOT cloud-hosted (no AWS, Azure, GCP, no localstack, no docker-compose deployment patterns)
- NOT a multi-tenant app (no user auth, no sessions, no OAuth, no API keys for users — just DJ)
- NOT a financial/trading/ecommerce system
- NOT a mobile app
- NOT a database product / NOT vector DB internals (uses existing tools, not building them)
- NOT replacing Ollama or vLLM (those are infrastructure choices, not change targets)

For each actionable finding:
1. title: Short title (max 80 chars), prefix with the RouxYou subsystem affected
2. description: What was found and why it matters FOR ROUXYOU SPECIFICALLY (2-3 sentences). If you can't write this without generic language, the finding is not relevant — skip it.
3. proposed_action: Specific concrete action against an actual RouxYou file/service/config
4. relevance: 0.0-1.0 — apply the "is not" list strictly. Default to LOW relevance if you can't connect the finding to a specific RouxYou subsystem.
5. url: Source URL

Hard rules:
- Only include findings with relevance >= 0.80
- Maximum 2 findings per batch (quality over quantity — most batches should return 0)
- Each finding MUST reference at least one specific RouxYou component (file path, service name, alias)
- If the finding could apply to ANY agent system / ANY Python project, it's too generic — reject
- "Improve performance" / "better architecture" / "consider X" — REJECT, not specific
- GitHub repos with working code applicable to OUR exact stack are HIGH relevance
- Articles from 2025-2026 about agent systems specifically are relevant

Default to empty array. The bar is high. An empty result is a SUCCESS — it means you exercised judgment.

Respond with ONLY a JSON array. Empty array [] if nothing meets the bar.
No markdown, no explanation — just the JSON array."""

    user_prompt = (
        f"SYSTEM CONTEXT:\n{topic['context']}\n\n"
        + (f"{recurrence_context}\n\n" if recurrence_context else "")
        + f"FOCUS AREA: {topic['focus']}\n\n"
        + f"SEARCH RESULTS:\n{results_text}\n\n"
        + "Evaluate these results. Which findings could concretely improve our system?"
    )

    # 2026-05-20: re-pointed from vLLM :9101 → Ollama :11434 OpenAI-compat endpoint
    # after the GGUF migration killed the old vLLM serve. Ollama supports the same
    # /v1/chat/completions schema; same sync HTTP path works.
    # Original note (still valid): direct sync HTTP avoids asyncio.run nested-loop issues
    # in both cron thread and FastAPI async context.
    try:
        start = time.time()
        resp = requests.post(
            OLLAMA_CHAT_URL,  # native /api/chat — supports think:false (the openai-compat endpoint does not)
            json={
                "model": MODEL_NAME,  # CONFIG.MODEL_REASON → the v4 backbone (2026-05-27); config-driven, no hardcode
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "think": False,    # qwen3 thinks ~5min & breaks JSON; off → clean JSON in ~1.4s (proven)
                "format": "json",  # constrain output to valid JSON
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 2000},
            },
            timeout=LLM_TIMEOUT,
        )
        elapsed = time.time() - start

        if resp.status_code != 200:
            logger.warning(f"LLM returned {resp.status_code}: {resp.text[:200]}")
            return []

        content = resp.json().get("message", {}).get("content", "")  # native /api/chat shape
        logger.info(f"RESEARCHER: LLM evaluated in {elapsed:.1f}s")

        findings = extract_json(content)  # shared robust parser: strip-think, fences, prose-tolerant
        if isinstance(findings, dict):
            findings = (findings.get("findings") or findings.get("results") or
                        findings.get("proposals") or [findings])
        if not isinstance(findings, list):
            return []

        # 2026-05-17: tightened 0.6 → 0.85 (noisy-proposals fix). 2026-06-05: loosened 0.85 → 0.80
        # — the digest was too conservative (≈2 notes/3 days); 0.80 + broader engines raises yield.
        return [f for f in findings[:MAX_PROPOSALS_PER_RUN]
                if isinstance(f, dict) and f.get("relevance", 0) >= 0.80]

    except requests.Timeout:
        logger.warning(f"LLM timed out after {LLM_TIMEOUT}s")
        return []
    except requests.ConnectionError:
        logger.warning("Ollama not reachable")
        return []
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"Failed to parse LLM response: {e}")
        return []


# === KNOWLEDGE-EMITTING DIGEST (2026-06-03 rework) ===
# web_researcher no longer emits raw [Research] PROPOSALS (they were ungrounded low-signal
# noise: 0/47 ever used). Instead it DISCOVERS (SearXNG) → DIGESTS (v5.3 judges relevance to
# the Roux thesis, the OSCAR test proved v5.3 CAN judge) → EMITS KNOWLEDGE NOTES to the inbox,
# which then flow through the SAME quality pipeline as DJ's curated fuel: knowledge_ingester →
# external_knowledge → reflection seed → grounded drafter → form-validator → pending proposal.
# This makes web_researcher the discovery front-end of the fuel pipeline. No HIL here (gate is
# at execution); no dedup (the reflection topic-diversity gate is the downstream backstop).

_V53_CHAT_URL = "http://127.0.0.1:8090/v1/chat/completions"  # resident Qwen3-30B-A3B (llama-server)
_KNOWLEDGE_INBOX = _BASE / "state" / "knowledge_inbox"
MAX_KNOWLEDGE_NOTES = 6   # 2026-06-18: 3→6 (was starving fuel; digest now scores ALL + we keep top-N)
RELEVANCE_BAR = 0.70      # 2026-06-18: 0.80→0.70 (digest was tossing on-thesis papers, e.g. Self-Harness; verified by raw-results eyeball)

_DIGEST_SYSTEM_PROMPT = """You are the research analyst for RouxYou. You judge web findings for relevance to ADVANCING THE ROUX PROJECT and output KNOWLEDGE NOTES — your job is to SELECT what's worth Roux knowing about, NOT to design the change (a separate grounded drafter does that downstream).

WHAT ROUXYOU IS (judge relevance against this):
- A LOCAL, offline-first, self-modifying + self-healing autonomous agent on ONE RTX 5060 Ti (16GB VRAM), 30GB RAM, Linux.
- Resident model: Qwen3-30B-A3B (MoE) Q4 via Vulkan llama-server; coder = qwen3-coder:30b-a3b (GPU-swapped in); router/embedder/authoring are CPU-pinned so they can't evict the GPU model (a proven pinning trick).
- Memory = LanceDB (vector) + BM25 (lexical) hybrid over ~37K memories, nomic-embed-text CPU embedder. "Memory IS the system."
- Self-modification loop (Pillar 3): reflection proposes → bigbrain (local v5.3) reviews with grounded verification → worker applies → runtime-verifies → trust-ledger graduation gate. FastAPI services + a tier-0 Watchtower supervisor (blue-green deploy, kill-switch, execution budget).
- MISSION (core, not peripheral): RouxYou is becoming the sovereign GUARDIAN of DJ's home + network — it will DRIVE Home Assistant (entities, automations, scripts, templates, the Assist voice pipeline, the HA MCP server), monitor the local network, and handle home security. Self-improvement is in service of being trusted to hold that responsibility.

THEMES THAT MATTER (broad but on-thesis): self-modifying/self-healing agentic architecture; memory-systems architecture + retrieval/reranking; NEW MODEL RELEASES that fit 16GB (directly, or via CPU-pinning/offload/MoE); local quantized/MoE serving + KV-cache tricks; agent tool-calling / structured-output / grounding reliability; trust/safety/verification; and — CENTRAL to the mission — the GUARDIAN track: Home Assistant + the HA MCP server (Roux is becoming the DRIVER of the home), ESPHome/voice, local network, and security monitoring. Treat Guardian/HA findings as on-thesis and HIGH — not peripheral.

HOW TO SCORE — score EVERY result you are given. Do NOT pre-filter, do NOT default to empty; the system applies the bar and logs your scores downstream. A relevant PAPER, repo, model, or technique is worth Roux knowing even if it needs adaptation — ERR TOWARD INCLUDING signal, not rejecting it.
- 0.80-1.0: directly advances a theme AND fits the LOCAL 16GB-GPU offline constraint, OR a concrete tool/model/technique/paper we could realistically adopt. A new model that fits 16GB (directly or via pinning/offload/MoE) is HIGH. A new agent/memory/self-improvement technique or PAPER applicable to our loop is HIGH.
- 0.65-0.79: plausibly on-thesis — a relevant paper/repo/technique that's adjacent or needs adaptation, or a useful reference. When unsure between "keep" and "reject", land HERE — a borderline-relevant paper is worth knowing.
- 0.0-0.5: clearly off-thesis NOISE — cloud-only, multi-tenant/web-frontend/mobile, enterprise press releases, generic "any project" advice, multi-GPU/datacenter, Apple-MLX-only, consumer fluff, or infra we already run.

For EACH result, output a KNOWLEDGE NOTE object (score them ALL — including low ones; the system filters + logs):
- "title": short (max 80 chars), prefix with the theme
- "relevance": 0.0-1.0
- "why_relevant": 1-2 sentences, why this matters FOR ROUX SPECIFICALLY (concrete; for low scores, name why it's off-thesis)
- "transferable_idea": the single concrete idea / tool / model / paper to take from it (or "" if noise)
- "url": source URL

Respond with ONLY a JSON array of note objects — ONE per result you were given. No markdown, no commentary."""


def _digest_findings_v53(topic: Dict, results: List[Dict], recurrence_context: str = "") -> List[Dict]:
    """DIGEST stage: v5.3 judges search results against the Roux thesis and returns
    KNOWLEDGE NOTES (not proposals). Uses the resident llama-server (:8090); extract_json
    tolerates v5.3's reasoning-leak/format quirks (the substance-good/form-bad pattern).
    Returns [] on any failure (skip cycle). bigbrain quality-fallback is a future option if
    v5.3's JUDGMENT proves weak — but the OSCAR test showed v5.3 can do this. 2026-06-03."""
    if not results:
        return []
    results_text = ""
    for i, r in enumerate(results):
        results_text += f"\nResult {i+1}:\n  Title: {r.get('title','')}\n  URL: {r.get('url','')}\n  Snippet: {r.get('snippet','')}\n"
    user_prompt = (
        f"RESEARCH FOCUS: {topic.get('focus','')}\nFOCUS CONTEXT: {topic.get('context','')}\n\n"
        + (f"{recurrence_context}\n\n" if recurrence_context else "")
        + f"SEARCH RESULTS:\n{results_text}\n\n"
        + "Score EVERY result above against the Roux thesis. Output a JSON array with ONE knowledge-note object per result (include the low/off-thesis ones with their score + reason — the system filters and logs). Do NOT pre-filter or return empty."
    )
    # RETRY (2026-06-18): v5.3 occasionally returns empty/unparseable output (the substance-good/
    # form-bad reasoning-leak pattern) — one bad sampling shouldn't drop a whole cycle of fuel.
    # Retry ONLY on empty/unparseable; a parsed-but-all-low result is a valid answer (no retry).
    scored = []
    for attempt in range(1, 3):
        try:
            start = time.time()
            resp = requests.post(
                _V53_CHAT_URL,
                json={
                    "model": "roux-v53",
                    "messages": [
                        {"role": "system", "content": _DIGEST_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 4096,  # 2026-06-18: 2000→4096 — digest now scores ALL results; generous so it isn't truncated mid-JSON
                    "response_format": {"type": "json_object"},
                },
                timeout=LLM_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.warning(f"RESEARCHER digest: v5.3 returned {resp.status_code} (attempt {attempt}): {resp.text[:160]}")
                continue
            content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.info(f"RESEARCHER: v5.3 digested in {time.time()-start:.1f}s" + (f" (attempt {attempt})" if attempt > 1 else ""))
            notes = extract_json(content)  # robust: strips think/fences, prose-tolerant
            if isinstance(notes, dict):
                notes = notes.get("notes") or notes.get("findings") or notes.get("results") or [notes]
            if isinstance(notes, list):
                scored = [n for n in notes if isinstance(n, dict) and n.get("relevance") is not None]
            if scored:
                break  # got a real scored batch
            logger.warning(f"RESEARCHER digest: empty/unparseable v5.3 output (attempt {attempt}) — {'retrying' if attempt < 2 else 'giving up'}")
        except requests.Timeout:
            logger.warning(f"RESEARCHER digest: v5.3 timed out after {LLM_TIMEOUT}s (attempt {attempt})")
        except Exception as e:
            logger.warning(f"RESEARCHER digest: v5.3 failed (attempt {attempt}): {e}")
    if not scored:
        return []
    # OBSERVABILITY (2026-06-18): log EVERY scored finding so rejections are auditable — was a blind spot.
    for n in sorted(scored, key=lambda x: -float(x.get("relevance", 0) or 0)):
        rel = float(n.get("relevance", 0) or 0)
        mark = "KEEP" if rel >= RELEVANCE_BAR else "drop"
        logger.info(f"RESEARCHER digest [{mark} {rel:.2f}] {str(n.get('title',''))[:70]} — {str(n.get('why_relevant',''))[:90]}")
    kept = sorted(
        [n for n in scored if float(n.get("relevance", 0) or 0) >= RELEVANCE_BAR and n.get("why_relevant")],
        key=lambda x: -float(x.get("relevance", 0) or 0),
    )[:MAX_KNOWLEDGE_NOTES]
    return kept


def _emit_knowledge_notes(topic: Dict, notes: List[Dict]) -> int:
    """EMIT stage: write each digested knowledge note as a .md file into state/knowledge_inbox/
    so the existing knowledge_ingester picks it up → external_knowledge → reflection fuel.
    NOT proposals. Returns count written. 2026-06-03."""
    if not notes:
        return 0
    try:
        _KNOWLEDGE_INBOX.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning(f"RESEARCHER emit: can't ensure inbox dir: {e}")
        return 0
    written = 0
    import re as _re_emit
    for n in notes:
        title = (n.get("title") or "research finding")[:80]
        slug = _re_emit.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:50] or "finding"
        fname = f"webfinding_{topic.get('focus','x')}_{slug}.md"
        body = (
            f"# {title}\n\n"
            f"Source (web research — topic: {topic.get('focus','')}): {n.get('url','(no url)')}\n"
            f"Relevance: {n.get('relevance','?')}\n\n"
            f"{n.get('why_relevant','')}\n\n"
            f"Transferable idea: {n.get('transferable_idea','')}\n"
        )
        try:
            (_KNOWLEDGE_INBOX / fname).write_text(body, encoding="utf-8")
            written += 1
            logger.info(f"RESEARCHER: emitted knowledge note → {fname}")
        except Exception as e:
            logger.warning(f"RESEARCHER emit: failed to write {fname}: {e}")
    return written


def run_research(topic_override: Optional[str] = None) -> Dict[str, Any]:
    """
    Main entry point. Runs one research cycle:
      1. Pick next topic (or use override)
      2. Search SearXNG for each query
      3. Feed results to LLM for evaluation
      4. Publish actionable findings as proposals
    """
    logger.info("run_research cycle starting")

    from shared.config import get_web_researcher_enabled

    if not get_web_researcher_enabled():
        logger.info("Researcher disabled by ROUX_WEB_RESEARCHER=0")
        return {"success": False, "error": "Researcher disabled by ROUX_WEB_RESEARCHER=0"}

    if not SEARXNG_URL:
        return {"success": False, "error": "SearXNG URL not configured. Set searxng_url in config.yaml."}

    state = _load_state()
    topics = _load_research_topics()  # live set from state/research_topics.json (self-modifiable)

    # Internal cadence gate (2026-06-03): cron fires hourly but real research runs ~3x/day.
    # topic_override (manual trigger) bypasses. "good, not a lot."
    if not topic_override:
        since = time.time() - state.get("last_run", 0)
        if since < RESEARCH_MIN_INTERVAL_S:
            return {"success": True, "throttled": True,
                    "next_run_in_min": int((RESEARCH_MIN_INTERVAL_S - since) / 60)}

    if topic_override:
        topic = next((t for t in topics if t["focus"] == topic_override), None)
        if not topic:
            available = [t["focus"] for t in topics]
            return {"success": False, "error": f"Unknown topic: {topic_override}. Available: {available}"}
    else:
        idx = state.get("topic_index", 0) % len(topics)
        topic = topics[idx]
        state["topic_index"] = (idx + 1) % len(topics)

    logger.info(f"RESEARCHER: Starting research — focus: {topic['focus']}")

    try:
        requests.get(f"{SEARXNG_URL}/search", params={"q": "test", "format": "json"}, timeout=5)
    except Exception:
        logger.warning("RESEARCHER: SearXNG not reachable, skipping")
        return {"success": False, "error": "SearXNG offline", "focus": topic["focus"]}

    all_results = []
    topic_search = topic.get("search")  # per-topic SearXNG strategy (categories/time_range/engines)
    for query in topic["queries"]:
        logger.info(f"  Searching: {query} (search={topic_search or 'default'})")
        results = _search_searxng(query, search_params=topic_search)
        all_results.extend(results)
        logger.info(f"  → {len(results)} results")

    if not all_results:
        logger.info("RESEARCHER: No search results found")
        _save_state(state)
        return {"success": True, "focus": topic["focus"], "searches": len(topic["queries"]),
                "results_found": 0, "findings": 0, "proposals_published": 0}

    seen_urls = set()
    unique_results = []
    for r in all_results:
        url = r.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_results.append(r)

    logger.info(f"RESEARCHER: {len(unique_results)} unique results, sending to LLM...")

    recurrence_context = _get_recurrence_context()
    # 2026-06-03 rework: DIGEST (v5.3 judges relevance to the Roux thesis) → EMIT KNOWLEDGE
    # NOTES to the inbox, instead of publishing raw [Research] proposals. The notes flow
    # through the curated-fuel pipeline (ingester → reflection → grounded drafter → form-
    # validator), so research findings get grounding + verification like DJ's hand-curated
    # fuel — not dumped as ungrounded proposals. v5.3 digest proven (OSCAR test + live).
    notes = _digest_findings_v53(topic, unique_results, recurrence_context)
    logger.info(f"RESEARCHER: {len(notes)} knowledge note(s) cleared the bar")
    emitted = _emit_knowledge_notes(topic, notes)

    state["last_run"] = time.time()
    state["runs"] = state.get("runs", 0) + 1
    state["findings_total"] = state.get("findings_total", 0) + len(notes)
    _save_state(state)

    stats = {
        "success": True,
        "focus": topic["focus"],
        "searches": len(topic["queries"]),
        "results_found": len(unique_results),
        "knowledge_notes": len(notes),
        "notes_emitted_to_inbox": emitted,
        "notes_detail": notes,
    }

    logger.info(
        f"RESEARCHER: Complete — {stats['searches']} searches, "
        f"{stats['results_found']} results, {stats['knowledge_notes']} knowledge notes, "
        f"{stats['notes_emitted_to_inbox']} emitted to inbox"
    )

    return stats
