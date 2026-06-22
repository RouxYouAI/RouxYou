#!/usr/bin/env python3
"""Generate synthetic authoring training pairs via Opus 4.7 to cover the three
gap classes identified after the first training run:

1. **NO-GROUNDING** (~40% of pairs) — topics where the right EVIDENCE is
   "no external citation, internal observation only". Teaches the adapter
   to NOT fabricate plausible-looking files when no real grounding exists.

2. **REAL-FILE** (~40%) — topics that DO have real grounding in the RouxYou
   codebase. EVIDENCE cites actual files from the structure inventory below.
   Teaches the adapter to ground in REAL paths.

3. **ANTI-FABRICATION** (~20%) — explicit (bad → good) transitions showing
   common fabrication shapes (repeating-char prop_IDs, 2024 paths, line-number
   fabrications) and how to rewrite cleanly.

Output → /home/user/TheSeed/authoring_lora/corpus/authoring_pairs.jsonl
(appended, NOT overwritten — preserves the existing 26 organic pairs)

Run:
    /home/user/RouxYou/venv/bin/python bin/gen_synthetic_authoring_pairs.py --n-per-class 30
"""
import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/user/RouxYou")
from dotenv import load_dotenv  # noqa
load_dotenv("/home/user/RouxYou/.env")


REAL_SHARED_MODULES = [
    "shared/authoring.py", "shared/bigbrain_context.py", "shared/blackbox.py",
    "shared/claude_memory.py", "shared/coach.py", "shared/companion.py",
    "shared/conversations.py", "shared/llm.py", "shared/logger.py",
    "shared/mcp_client.py", "shared/memory.py", "shared/post_exec_reflection.py",
    "shared/proposal_bus.py", "shared/proposal_executor.py", "shared/proposer.py",
    "shared/reflection_coach.py", "shared/researcher.py", "shared/roux_self_propose.py",
    "shared/shadow_sweeper.py", "shared/shadow_verifier.py", "shared/task_queue.py",
    "shared/verifier.py",
]
REAL_SERVICES = [
    "services/watchtower/api.py", "services/roux/roux_service.py",
    "orchestrator/orchestrator.py", "coder/coder.py", "worker/worker.py",
    "memory/memory_agent.py", "gateway/gateway.py", "dashboard.py",
]
REAL_GOVERNANCE = [
    "governance/META_POLICY.md", "governance/DISCLOSURE_RULE.md",
    "governance/disclosure_rule.json", "governance/meta_policy.json",
    "governance/AMENDMENT_2026-05-17_claude_in_loop.md",
    "governance/AMENDMENT_2026-05-17_roux_promotion.md",
    "governance/AMENDMENT_2026-05-19_governance_relevance.md",
]
REAL_STATE = [
    "state/proposals_active.json", "state/proposals_history.json",
    "state/shadow_verifications.jsonl", "state/executor_log.jsonl",
    "state/reflection_log.jsonl", "state/skip_enforcement_log.jsonl",
    "state/triage_dismiss_log.jsonl", "state/roux_self_propose_state.json",
    "state/reflection_coach_state.json", "state/auto_approve_config.json",
]


def make_id(seed: str) -> str:
    return "pair_" + hashlib.sha1(seed.encode()).hexdigest()[:12]


GEN_PROMPT_NO_GROUNDING = """You are Claude (Opus 4.7) generating SYNTHETIC training pairs for Roux's authoring LoRA.

This batch teaches a specific lesson: when a topic has NO real external grounding (no docs exist, no prior proposal to cite, no specific log evidence), the right EVIDENCE is "no external citation, internal observation only" — NOT fabricated paths or made-up file references.

Generate {n} diverse training pairs. Each pair has:
- topic: a plausible RouxYou improvement idea where no real cited evidence exists yet
- target: the clean proposal output (TITLE/CATEGORY/PRIORITY/DESCRIPTION/PROPOSED_ACTION/EVIDENCE/REASONING)

The KEY teaching signal: EVIDENCE must explicitly say "no external citation, internal observation only" or similar honest phrasing. PROPOSED_ACTION may reference future-to-be-created files (legitimate creation targets), but EVIDENCE must NOT fabricate "existing" files.

REASONING should articulate WHY the proposer thought of this — internal motivation, observed pattern, hypothesis. NOT boilerplate.

Topics should be varied: features, UX changes, ops adjustments, hypothesis-driven experiments, light refactors. Avoid governance-category topics (those need human review).

Output as a JSON array of objects with shape:
{{
  "topic": "...",
  "target": {{
    "title": "...", "category": "codebase|memory|tasks|resources|skills|capabilities",
    "priority": 3-7, "description": "...", "proposed_action": "...",
    "evidence": "...",  // MUST say no external citation
    "reasoning": "..."  // ≥100 chars, specific, articulated
  }}
}}

NO markdown, NO commentary outside the JSON array. Just the array.
"""

GEN_PROMPT_REAL_FILE = """You are Claude (Opus 4.7) generating SYNTHETIC training pairs for Roux's authoring LoRA.

This batch teaches the model to USE real files from the RouxYou codebase as EVIDENCE. The adapter is being trained on a small corpus and has shown it can use real files correctly when the topic mentions them — we want to reinforce that pattern with more examples.

Generate {n} training pairs where the topic + target naturally reference real files from this inventory:

shared/ modules (any of): {shared}
services/ + entrypoints (any of): {services}
state/ files (any of): {state}
governance/ docs (any of): {governance}

Each pair has:
- topic: a plausible RouxYou improvement that legitimately benefits from citing one or more of those real files as grounding
- target: the clean proposal output. EVIDENCE field cites real files only (from the inventory above) — never fabricate paths.

PROPOSED_ACTION may reference real files for modifications AND future-to-be-created files (legitimate creation targets).
REASONING is specific to the proposal.

Output as a JSON array of objects with shape:
{{
  "topic": "...",
  "target": {{
    "title": "...", "category": "codebase|memory|tasks|resources|skills|capabilities",
    "priority": 3-8, "description": "...", "proposed_action": "...",
    "evidence": "...",  // cites real files from the inventory
    "reasoning": "..."  // ≥100 chars, specific
  }}
}}

NO markdown, NO commentary. Just the array.
"""

GEN_PROMPT_ANTI_FAB = """You are Claude (Opus 4.7) generating SYNTHETIC training pairs for Roux's authoring LoRA.

This batch teaches the model what NOT to do by inverting common fabrication patterns. Each pair pretends the topic implicitly suggests a fabrication shape, and the target shows the CLEAN rewrite that avoids it.

Specific anti-patterns to cover across the {n} pairs:
- repeating-character prop_IDs (prop_aabb1234..., prop_zzcc..., prop_dead...)
- 2024-dated state files (state/legacy_*_2024.json, audit_gaps_2024.json)
- Windows-era references (phase40b, .bat files, gpt-oss)
- specific fabricated spec files (FOO_SPEC_v2.md, BAR_AMENDMENT_v3.md)
- fabricated line numbers in code references (line 248, line 520 with specifics)
- self-authored pending proposals cited as evidence

Each pair has:
- topic: a topic where a careless authoring layer might fabricate (e.g. "Reference the prior discussion about X" — there's no prior discussion, but it sounds like there should be)
- target: the clean rewrite — EVIDENCE honestly says "no prior proposal cited; this is a fresh idea" or cites only verified files

Output as a JSON array. NO markdown, NO commentary. Just the array.
"""


def parse_pairs_response(raw: str, default_kind: str) -> list:
    """Parse a JSON array response, tolerant of accidental fences/preambles."""
    raw = raw.strip()
    raw = re.sub(r"^```(json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    # Find the first '[' and last ']' to extract the array
    i = raw.find("[")
    j = raw.rfind("]")
    if i < 0 or j < 0 or j <= i:
        return []
    try:
        arr = json.loads(raw[i:j+1])
    except json.JSONDecodeError as e:
        print(f"  [WARN] JSON parse failed: {e}")
        return []
    if not isinstance(arr, list):
        return []
    out = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        topic = item.get("topic", "")
        target = item.get("target", {})
        if not topic or not isinstance(target, dict):
            continue
        if not target.get("title") or not target.get("reasoning"):
            continue
        out.append({
            "id": make_id(f"{default_kind}:{topic}:{target['title']}"),
            "kind": default_kind,
            "input": {
                "topic": topic,
                "discipline": "",  # discipline injected by build_authoring_prompt at format time
                "coaching": "",
            },
            "target": {
                "title": target.get("title", "")[:120],
                "category": (target.get("category") or "codebase").lower(),
                "priority": int(target.get("priority", 5) or 5),
                "description": target.get("description", ""),
                "proposed_action": target.get("proposed_action", ""),
                "evidence": target.get("evidence", ""),
                "reasoning": target.get("reasoning", ""),
            },
            "metadata": {
                "source": f"bigbrain_synthetic_{default_kind}",
                "generated_at": time.time(),
            },
        })
    return out


async def gen_batch(prompt: str, kind: str, n: int, batch_size: int = 10) -> list:
    """Generate `n` pairs by calling bigbrain in batches of `batch_size`."""
    from shared.llm import llm_generate, init_providers
    from shared.companion import _strip_thinking
    from shared.bigbrain_context import bigbrain_system_prompt
    init_providers()
    system = bigbrain_system_prompt()

    all_pairs = []
    remaining = n
    batch_idx = 1
    while remaining > 0:
        this_batch = min(batch_size, remaining)
        print(f"  [batch {batch_idx}] generating {this_batch} {kind} pairs…")
        filled_prompt = prompt.format(
            n=this_batch,
            shared=", ".join(REAL_SHARED_MODULES),
            services=", ".join(REAL_SERVICES),
            state=", ".join(REAL_STATE),
            governance=", ".join(REAL_GOVERNANCE),
        )
        try:
            r = await llm_generate("bigbrain", prompt=filled_prompt, system=system,
                                   max_tokens=8000, temperature=0.6, timeout=180)
        except Exception as e:
            print(f"    [WARN] bigbrain call failed: {e}; skipping batch")
            batch_idx += 1
            continue
        if not getattr(r, "success", False):
            print(f"    [WARN] bigbrain returned failure: {getattr(r, 'error', '?')}")
            batch_idx += 1
            continue
        raw = _strip_thinking(r.text or "")
        parsed = parse_pairs_response(raw, default_kind=kind)
        print(f"    [batch {batch_idx}] parsed {len(parsed)} pairs")
        all_pairs.extend(parsed)
        remaining -= this_batch
        batch_idx += 1
    return all_pairs


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-per-class", type=int, default=30,
                        help="Pairs to generate per class (default 30 → 90 total)")
    parser.add_argument("--batch-size", type=int, default=10)
    args = parser.parse_args()

    print(f"Generating {args.n_per_class} pairs per class via Opus 4.7")
    print()

    print("=== Class 1: NO-GROUNDING ===")
    no_grounding = await gen_batch(GEN_PROMPT_NO_GROUNDING, "synthetic_no_grounding",
                                    args.n_per_class, args.batch_size)
    print(f"  total: {len(no_grounding)}")
    print()

    print("=== Class 2: REAL-FILE ===")
    real_file = await gen_batch(GEN_PROMPT_REAL_FILE, "synthetic_real_file",
                                 args.n_per_class, args.batch_size)
    print(f"  total: {len(real_file)}")
    print()

    print("=== Class 3: ANTI-FAB ===")
    anti_fab = await gen_batch(GEN_PROMPT_ANTI_FAB, "synthetic_anti_fab",
                                args.n_per_class // 2, args.batch_size)  # smaller per spec
    print(f"  total: {len(anti_fab)}")
    print()

    all_new = no_grounding + real_file + anti_fab
    out_path = Path("/home/user/TheSeed/authoring_lora/corpus/authoring_pairs.jsonl")
    existing_ids = set()
    if out_path.exists():
        with open(out_path) as f:
            for line in f:
                try:
                    existing_ids.add(json.loads(line).get("id"))
                except Exception:
                    pass
    new_count = 0
    with open(out_path, "a") as f:
        for p in all_new:
            if p["id"] in existing_ids:
                continue
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
            new_count += 1
    print(f"appended {new_count} new pairs → {out_path}")
    print(f"corpus total now: {sum(1 for _ in open(out_path))}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
