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

import json
import time
import requests
from typing import List, Dict, Any, Optional
from pathlib import Path

import sys
_BASE = Path(__file__).parent.parent
sys.path.insert(0, str(_BASE))

from shared.logger import get_logger
from config import CONFIG

logger = get_logger("researcher")

SEARXNG_URL = CONFIG.SEARXNG_URL
OLLAMA_CHAT_URL = f"{CONFIG.OLLAMA_HOST}/api/chat"
MODEL_NAME = CONFIG.MODEL_REASON
SEARCH_TIMEOUT = 15
LLM_TIMEOUT = 90
MAX_RESULTS_PER_QUERY = 5
MAX_PROPOSALS_PER_RUN = 3

STATE_FILE = _BASE / "state" / "researcher_state.json"


# === RESEARCH TOPICS ===
# Rotated through — one batch per run, cycling over ~1 week

RESEARCH_TOPICS = [
    {
        "focus": "agent_orchestration",
        "queries": [
            "FastAPI multi-agent orchestration patterns 2025 2026",
            "Python autonomous agent task queue best practices",
        ],
        "context": "RouxYou uses FastAPI for all services, with an Orchestrator routing tasks to Coder and Worker agents.",
    },
    {
        "focus": "memory_retrieval",
        "queries": [
            "episodic memory agent system decay pruning strategies",
            "vector search optimization embedding models local inference",
        ],
        "context": "System uses JSON-based episodic memory with utility scoring, decay, and deduplication. RAG uses ChromaDB.",
    },
    {
        "focus": "local_llm_inference",
        "queries": [
            "Ollama local LLM optimization inference speed 2025 2026",
            "quantized model inference quality tradeoffs agent systems",
        ],
        "context": "Coder uses a 14B quantized model via Ollama for task planning. Local inference on consumer GPU.",
    },
    {
        "focus": "deployment_reliability",
        "queries": [
            "blue green deployment Python service hot swap patterns",
            "self-healing agent system service restart strategies",
        ],
        "context": "System has blue-green deploy with health checks, Watchtower supervisor for restarts, and proposal-based self-healing.",
    },
    {
        "focus": "code_intelligence",
        "queries": [
            "automated code review AI agent codebase analysis",
            "Python AST analysis code quality automation",
        ],
        "context": "Coder has a codebase index (AST-based) tracking modules, classes, functions for architectural awareness.",
    },
    {
        "focus": "agent_safety",
        "queries": [
            "AI agent safety kill switch human oversight patterns",
            "autonomous agent execution budget rate limiting",
        ],
        "context": "System has an immutable Watchtower, kill switch, execution budget, and human approval gates on all self-modifications.",
    },
    {
        "focus": "emerging_patterns",
        "queries": [
            "MCP model context protocol agent tools 2025 2026",
            "autonomous coding agent self-improvement techniques",
        ],
        "context": "RouxYou is a self-modifying agent system. Looking for new patterns in the agent/AI engineering space.",
    },
]


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


def _search_searxng(query: str) -> List[Dict]:
    if not SEARXNG_URL:
        logger.warning("RESEARCHER: SearXNG URL not configured in config.yaml")
        return []
    try:
        resp = requests.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "format": "json", "categories": "general,it"},
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

    system_prompt = """You are a research analyst for RouxYou, an autonomous agent system.
You've been given web search results about a specific technology area.
Identify findings that could CONCRETELY improve the system.

For each actionable finding:
1. title: Short title (max 80 chars)
2. description: What was found and why it matters (2-3 sentences)
3. proposed_action: Specific action to take
4. relevance: 0.0-1.0
5. url: Source URL

Rules:
- Only include findings with relevance > 0.6
- Maximum 3 findings per batch
- Be SPECIFIC — no vague suggestions like "improve performance"
- GitHub repos with working code are HIGH relevance
- Articles from 2025-2026 are more relevant than older ones

Respond with ONLY a JSON array. Empty array [] if nothing is relevant.
No markdown, no explanation — just the JSON array."""

    user_prompt = (
        f"SYSTEM CONTEXT:\n{topic['context']}\n\n"
        + (f"{recurrence_context}\n\n" if recurrence_context else "")
        + f"FOCUS AREA: {topic['focus']}\n\n"
        + f"SEARCH RESULTS:\n{results_text}\n\n"
        + "Evaluate these results. Which findings could concretely improve our system?"
    )

    try:
        start = time.time()
        resp = requests.post(
            OLLAMA_CHAT_URL,
            json={
                "model": MODEL_NAME,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
                "stream": False,
                "format": "json",
            },
            timeout=LLM_TIMEOUT,
        )
        elapsed = time.time() - start

        if resp.status_code != 200:
            logger.warning(f"Ollama returned {resp.status_code}")
            return []

        content = resp.json().get("message", {}).get("content", "")
        logger.info(f"RESEARCHER: LLM evaluated in {elapsed:.1f}s")

        if "<think>" in content:
            content = content.split("</think>")[-1].strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        findings = json.loads(content.strip())
        if isinstance(findings, dict):
            findings = (findings.get("findings") or findings.get("results") or
                        findings.get("proposals") or [findings])
        if not isinstance(findings, list):
            return []

        return [f for f in findings[:MAX_PROPOSALS_PER_RUN]
                if isinstance(f, dict) and f.get("relevance", 0) > 0.6]

    except requests.Timeout:
        logger.warning(f"LLM timed out after {LLM_TIMEOUT}s")
        return []
    except requests.ConnectionError:
        logger.warning("Ollama not reachable")
        return []
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"Failed to parse LLM response: {e}")
        return []


def run_research(topic_override: Optional[str] = None) -> Dict[str, Any]:
    """
    Main entry point. Runs one research cycle:
      1. Pick next topic (or use override)
      2. Search SearXNG for each query
      3. Feed results to LLM for evaluation
      4. Publish actionable findings as proposals
    """
    if not SEARXNG_URL:
        return {"success": False, "error": "SearXNG URL not configured. Set searxng_url in config.yaml."}

    state = _load_state()

    if topic_override:
        topic = next((t for t in RESEARCH_TOPICS if t["focus"] == topic_override), None)
        if not topic:
            available = [t["focus"] for t in RESEARCH_TOPICS]
            return {"success": False, "error": f"Unknown topic: {topic_override}. Available: {available}"}
    else:
        idx = state.get("topic_index", 0) % len(RESEARCH_TOPICS)
        topic = RESEARCH_TOPICS[idx]
        state["topic_index"] = (idx + 1) % len(RESEARCH_TOPICS)

    logger.info(f"RESEARCHER: Starting research — focus: {topic['focus']}")

    try:
        requests.get(f"{SEARXNG_URL}/search", params={"q": "test", "format": "json"}, timeout=5)
    except Exception:
        logger.warning("RESEARCHER: SearXNG not reachable, skipping")
        return {"success": False, "error": "SearXNG offline", "focus": topic["focus"]}

    all_results = []
    for query in topic["queries"]:
        logger.info(f"  Searching: {query}")
        results = _search_searxng(query)
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
    findings = _evaluate_with_llm(topic, unique_results, recurrence_context)
    logger.info(f"RESEARCHER: {len(findings)} actionable finding(s)")

    published = 0
    if findings:
        from shared.proposal_bus import publish_proposal
        for f in findings:
            title = f.get("title", "Research finding")[:80]
            result = publish_proposal(
                title=f"[Research] {title}",
                description=f.get("description", ""),
                category="optimization",
                priority=3,
                proposed_action=f.get("proposed_action", "Investigate finding"),
                evidence=f"Source: {f.get('url', 'N/A')} | Relevance: {f.get('relevance', 0):.0%} | Focus: {topic['focus']}",
                reversible=True,
                source="research",
                confidence=f.get("relevance", 0.7),
                executor="coder",
                coach_reasoning=f"Web research finding from {topic['focus']} scan",
            )
            if result and result.get("state") == "pending":
                published += 1
                logger.info(f"  Published: {title}")

    state["last_run"] = time.time()
    state["runs"] = state.get("runs", 0) + 1
    state["findings_total"] = state.get("findings_total", 0) + len(findings)
    _save_state(state)

    stats = {
        "success": True,
        "focus": topic["focus"],
        "searches": len(topic["queries"]),
        "results_found": len(unique_results),
        "findings": len(findings),
        "proposals_published": published,
        "findings_detail": findings,
    }

    logger.info(
        f"RESEARCHER: Complete — {stats['searches']} searches, "
        f"{stats['results_found']} results, {stats['findings']} findings, "
        f"{stats['proposals_published']} proposals published"
    )

    return stats
