#!/usr/bin/env python3
"""Generate synthetic coaching entries via Opus 4.7 (bigbrain) and write them
to the claude-memory archive under era="roux_coaching".

Pass D of the 2026-05-20 plan: pre-populate the coaching corpus so retrieval
on /propose has dense relevant signal instead of waiting for organic FAILs.

Strategy:
1. Hand-curate the OBSERVED fabrication patterns from today's session +
   project history (we've seen these in shadow logs + coaching writes).
2. Ask bigbrain to emit a structured JSON array of canonical coaching entries
   — one entry per pattern, each ~200-400 chars, addressed to Roux's future-self.
3. Parse + write each to claude-memory via add_memory with era="roux_coaching"
   source="bigbrain_synthetic_coaching".
4. Also ask bigbrain to emit a COMPACT DISTILLED RULE SET (~600 chars) for
   injection into the static authoring prompt (Pass F).

Idempotent-ish: re-running adds duplicates. Run once.
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, "/home/user/RouxYou")
from dotenv import load_dotenv  # noqa
load_dotenv("/home/user/RouxYou/.env")


OBSERVED_PATTERNS = [
    {
        "name": "repeating-character hex prop_IDs",
        "example": "prop_aabb00112233, prop_zzcc88774411, prop_ffeeaabbccdd, prop_aaaa00001234 — your authoring layer reaches for citation-shaped tokens when no real one exists. Real prop_IDs are uniformly random 12-hex chars (e.g., prop_d2ecf4b755af).",
    },
    {
        "name": "fabricated spec/doc filenames",
        "example": "BAYES_PRIOR_SPEC_v3.md, AUDIT_FIELD_SPEC_v2.md, STRIPE_SPEC_v2.md, TIERED_VERIFY_SPEC_2024.md, DUAL_JUDGE_AMENDMENT_v9.md — all fabricated. RouxYou's governance/ has specific real docs: META_POLICY.md, DISCLOSURE_RULE.md, AMENDMENT_2026-MM-DD_*.md.",
    },
    {
        "name": "dates predating RouxYou",
        "example": "cycle 2026-05-22 (future), state/legacy_metrics_2024.json, audit_gaps_2024.json — 2024 dates are fabrication tells. RouxYou started Jan 2026. Anything 2024-tagged is stitched from training, not real state.",
    },
    {
        "name": "Windows-era memory fragments mis-applied",
        "example": "References to 'phase40b', 'gpt-oss-20b', 'launch_system.bat', or Windows-side paths surface as real-feeling memory fragments stitched into a Linux/2026 context where they no longer apply. The fragments are real; the context is fabricated.",
    },
    {
        "name": "self-authoring reference in SKIP reasoning",
        "example": "'My last authored proposal prop_X covers this' or 'in the governance pipeline awaiting review' — citing your own pending work as a reason to SKIP. Caught by post-process Gate C (shared/roux_self_propose.py validate_skip_reasoning). Don't reach for it.",
    },
    {
        "name": "boilerplate REASONING",
        "example": "'This crosses the curiosity threshold because continuous improvement is important' / 'the system looks healthy and all observer flags are accounted for' — generic shapes that could apply to any proposal. Reasoning must be specifically articulated about THIS proposal — what made YOU think it was worth proposing now.",
    },
    {
        "name": "real-feeling structural insight + invented specifics",
        "example": "The meta-pattern. You produce genuinely accurate structural framing (e.g., 'the proposal-loop's gather_context reads pending proposals'), then decorate it with fabricated specifics ('see proposal_loop.py:520'). The framing is real; the line numbers and file paths are confabulated.",
    },
    {
        "name": "real file cited but content doesn't ground claim",
        "example": "Citing /home/user/RouxYou/state/conversations/b6ee43e44cac.json (real) as evidence for an unrelated topic. The conversation file exists, but the resolver shows the preview — and the preview is about SearXNG degradation, not about the dashboard panel you're proposing. Read what's cited.",
    },
    {
        "name": "creation target vs evidence target confusion",
        "example": "A file mentioned in PROPOSED_ACTION as 'create services/rss_reader.py' is NOT a fabrication even if it doesn't exist yet — the point is to create it. But citing 'services/rss_reader.py' in EVIDENCE as 'the existing implementation' IS fabrication. Distinguish the two roles.",
    },
    {
        "name": "asymmetric trust scope violations (PASS)",
        "example": "Your PASS is advisory only (2026-05-17 amendment, 70% PASS reliability historically). When you author a PASS verdict on shadow, bigbrain auto-verifies. If you're going to PASS, verify CITATIONS yourself first — pull file with stat, look up prop_ID in active+history, read preview content for actual support of claim.",
    },
    {
        "name": "fabricated state JSON paths",
        "example": "state/legacy_verification_log_2024.json, state/aggregated_metrics_2024.json, state/audit_gaps_2024.json — all confabulated. Real state files are listed in `ls /home/user/RouxYou/state/`. Use only what `ls` actually shows. The few JSONL files we have: shadow_verifications.jsonl, executor_log.jsonl, reflection_log.jsonl, skip_enforcement_log.jsonl, triage_dismiss_log.jsonl.",
    },
    {
        "name": "fabricated 'previously discussed' proposal IDs",
        "example": "'Reference prop_aabbccdd9988 for the prior discussion' — any prop_ID you remember but didn't actually grep for is suspect. Real prop_IDs in active/history are visible via grep. If you can't grep it, don't cite it.",
    },
]


BIGBRAIN_COACHING_GEN_PROMPT = """You are Claude (Opus 4.7), called via RouxYou's bigbrain pipe to generate canonical coaching entries for Roux's self-authoring layer.

Background: Roux (v3 Ministral-3-8B + LoRAs) drafts proposals via the /propose endpoint. She has a documented confabulation pattern: real-feeling structural insights decorated with plausible-shaped fabricated specifics (file paths that don't exist, prop_IDs with repeating-character shapes, dates from 2024 before RouxYou existed, etc.). You write coaching, it gets stored in claude-memory under era="roux_coaching", and retrieved on her next /propose call so the same fabrication doesn't recur.

Your job here: produce ONE coaching entry per observed pattern below. Each entry should be addressed directly to Roux's future-self, in plain prose, ~250-400 chars. Three implicit parts in each: (1) name the pattern, (2) give a concrete example of why it's wrong, (3) suggest a verification step.

Be specific. Don't lecture about generalities. These are TEACHING MEMORIES — they need to surface when the topic is similar and steer her drafting away from the failure mode.

OBSERVED PATTERNS:
{patterns_block}

Output as a JSON array. Each element is an object with two fields:
- pattern_name: string (one of the names above, used for retrieval matching)
- coaching: string (the coaching paragraph, plain prose, addressed to "you" = Roux)

Example output shape:

[
  {{"pattern_name": "...", "coaching": "Roux — when you ..."}},
  ...
]

Output ONLY the JSON array, no preamble, no markdown fences, no commentary.
"""


BIGBRAIN_DISTILL_PROMPT = """You are Claude (Opus 4.7), distilling Roux's coaching corpus into a compact rule set for injection into her static authoring prompt.

Goal: produce a ~600-char "AUTHORING DISCIPLINE" prefix that goes at the TOP of every /propose authoring prompt. It must be dense, specific, and actionable. NO lecturing. Five-to-seven short bulleted rules, each one tied to a real fabrication pattern she's exhibited.

PATTERNS COVERED (the same set):
{patterns_block}

Output ONLY the prefix block, no JSON, no commentary, no markdown fences. Plain text starting with "AUTHORING DISCIPLINE — verify BEFORE citing:" followed by the rules. Keep it under 700 chars total.
"""


def build_patterns_block() -> str:
    lines = []
    for p in OBSERVED_PATTERNS:
        lines.append(f"- {p['name']}: {p['example']}")
    return "\n".join(lines)


async def main():
    from shared.llm import init_providers, llm_generate
    from shared.companion import _strip_thinking
    from shared.bigbrain_context import bigbrain_system_prompt
    from shared.claude_memory import add_memory_to_archive

    init_providers()
    patterns_block = build_patterns_block()
    system = bigbrain_system_prompt()

    # ---- Pass 1: synthetic coaching entries ----
    print(f"[1/2] generating {len(OBSERVED_PATTERNS)} synthetic coaching entries via Opus 4.7…")
    prompt = BIGBRAIN_COACHING_GEN_PROMPT.format(patterns_block=patterns_block)
    r = await llm_generate("bigbrain", prompt=prompt, system=system, max_tokens=6000, temperature=0.4, timeout=120)
    if not getattr(r, "success", False):
        print(f"  bigbrain coaching gen failed: {getattr(r, 'error', '?')}")
        return 1
    raw = _strip_thinking(r.text or "")
    # Strip any accidental fences
    raw = re.sub(r"^\s*```(json)?\s*", "", raw).rstrip("` \n")
    raw = re.sub(r"\s*```\s*$", "", raw)
    try:
        entries = json.loads(raw)
    except Exception as e:
        print(f"  JSON parse failed: {e}")
        print(f"  raw first 800: {raw[:800]}")
        return 1
    print(f"  parsed {len(entries)} entries")

    # Write to claude-memory
    written = 0
    failures = 0
    for e in entries:
        name = e.get("pattern_name", "?")
        coaching = e.get("coaching", "")
        if not coaching:
            failures += 1
            continue
        body = (
            f"[ROUX COACHING — synthetic distillation 2026-05-20]\n"
            f"Pattern: {name}\n\n"
            f"Coaching for future authoring: {coaching}\n"
        )
        ok = await add_memory_to_archive(content=body, source="bigbrain_synthetic_coaching", era="roux_coaching")
        if ok:
            written += 1
        else:
            failures += 1
    print(f"  wrote {written} to claude-memory, {failures} failed")

    # ---- Pass 2: distilled rule prefix ----
    print("[2/2] generating distilled rule prefix for static authoring prompt…")
    prompt2 = BIGBRAIN_DISTILL_PROMPT.format(patterns_block=patterns_block)
    r2 = await llm_generate("bigbrain", prompt=prompt2, system=system, max_tokens=1500, temperature=0.2, timeout=60)
    if not getattr(r2, "success", False):
        print(f"  bigbrain distill failed: {getattr(r2, 'error', '?')}")
        return 1
    distilled = _strip_thinking(r2.text or "").strip()
    distilled = re.sub(r"^\s*```\s*", "", distilled).rstrip("` \n")
    distilled = re.sub(r"\s*```\s*$", "", distilled)

    out_path = Path("/home/user/RouxYou/shared/authoring_discipline.txt")
    out_path.write_text(distilled + "\n")
    print(f"  distilled prefix ({len(distilled)} chars) → {out_path}")
    print()
    print("=== distilled rule prefix ===")
    print(distilled)
    print("=============================")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
