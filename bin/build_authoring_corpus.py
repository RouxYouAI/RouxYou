#!/usr/bin/env python3
"""Build a training corpus for the future Authoring LoRA (E).

Pulls three kinds of pairs into ~/TheSeed/coder_lora/corpus/authoring_pairs.jsonl:

  1. POSITIVE — completed/approved proposals from proposals_history.
     Pair = (topic-shaped input → the proposal's actual fields).
     These teach "what a clean output looks like."

  2. CORRECTION — proposals where Roux drafted, shadow caught a fabrication
     (FAIL with coaching), and we can reconstruct the topic. Bigbrain (Opus 4.7)
     is then asked to author the CORRECTED version given the same topic + the
     coaching that caught the failure. Pair = (input → gold draft).
     These teach "what to drift toward when you'd otherwise fabricate."

  3. NEGATIVE-WITH-COACHING (optional) — pairs of (Roux's fabricated draft +
     coaching → corrected gold). The "show what was wrong" mode. Skipped in
     default run, can be added later if useful for contrastive training.

Output schema (one JSON object per line in authoring_pairs.jsonl):

    {
      "id": "...",
      "kind": "positive" | "correction",
      "input": {
        "topic": "...",
        "discipline": "...",       # the static prefix at train-time
        "coaching": "..."          # any coaching context (may be empty for positives)
      },
      "target": {
        "title": "...",
        "category": "...",
        "priority": 7,
        "description": "...",
        "proposed_action": "...",
        "evidence": "...",
        "reasoning": "..."
      },
      "metadata": {
        "source": "proposal_history" | "bigbrain_correction",
        "origin_proposal_id": "prop_xxx",
        "generated_at": 1779...
      }
    }

The training script (later) renders each pair as:
    prompt   = build_authoring_prompt(topic, coaching_notes=coaching)
    completion = serialize(target_fields)

Run:
    python3 bin/build_authoring_corpus.py --with-gold
    # --no-gold to skip bigbrain calls (positives only, fast)
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


PROPOSALS_ACTIVE = Path("/home/user/RouxYou/state/proposals_active.json")
PROPOSALS_HISTORY = Path("/home/user/RouxYou/state/proposals_history.json")
SHADOW_LOG = Path("/home/user/RouxYou/state/shadow_verifications.jsonl")
DISCIPLINE_PATH = Path("/home/user/RouxYou/shared/authoring_discipline.txt")
OUT_PATH = Path("/home/user/TheSeed/authoring_lora/corpus/authoring_pairs.jsonl")


def make_id(seed: str) -> str:
    return "pair_" + hashlib.sha1(seed.encode()).hexdigest()[:12]


def load_discipline() -> str:
    try:
        return DISCIPLINE_PATH.read_text().strip()
    except Exception:
        return ""


def load_proposals() -> dict:
    """Return {prop_id: proposal} merging active + history."""
    out = {}
    for path in (PROPOSALS_ACTIVE, PROPOSALS_HISTORY):
        try:
            data = json.loads(path.read_text())
            for p in data:
                out[p["id"]] = p
        except Exception:
            pass
    return out


def extract_positives(props: dict) -> list:
    """Completed codebase proposals as positive examples."""
    out = []
    for p in props.values():
        if p.get("state") not in ("completed", "resolved"):
            continue
        if p.get("category") != "codebase":
            continue
        title = (p.get("title") or "").strip()
        description = (p.get("description") or "").strip()
        proposed_action = (p.get("proposed_action") or "").strip()
        evidence = (p.get("evidence") or "").strip()
        if not (title and proposed_action):
            continue
        reasoning = ((p.get("disclosure") or {}).get("reasoning") or "").strip()
        topic = f"{title}. {description}".strip()
        out.append({
            "id": make_id(f"pos:{p['id']}"),
            "kind": "positive",
            "input": {"topic": topic, "discipline": load_discipline(), "coaching": ""},
            "target": {
                "title": title,
                "category": p.get("category", "codebase"),
                "priority": p.get("priority", 5),
                "description": description,
                "proposed_action": proposed_action,
                "evidence": evidence,
                "reasoning": reasoning,
            },
            "metadata": {
                "source": "proposal_history",
                "origin_proposal_id": p["id"],
                "generated_at": time.time(),
            },
        })
    return out


def extract_fail_with_coaching(props: dict) -> list:
    """Walk shadow log; build (topic, coaching, original_draft) tuples for
    proposals where bigbrain FAILed and wrote coaching."""
    out = []
    if not SHADOW_LOG.exists():
        return out
    seen = set()
    with open(SHADOW_LOG) as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            prop_id = e.get("proposal_id")
            if not prop_id or prop_id in seen:
                continue
            for verdict_src in ("claude_parsed", "claude_defer_parsed"):
                vsrc = e.get(verdict_src) or {}
                if vsrc.get("verdict") == "FAIL" and vsrc.get("coaching"):
                    p = props.get(prop_id)
                    if not p:
                        continue
                    title = (p.get("title") or "").strip()
                    description = (p.get("description") or "").strip()
                    topic = f"{title}. {description}".strip()
                    if not topic:
                        continue
                    seen.add(prop_id)
                    out.append({
                        "prop_id": prop_id,
                        "topic": topic,
                        "coaching": vsrc.get("coaching"),
                        "bigbrain_reasoning": vsrc.get("reasoning") or "",
                        "original_draft": {
                            "title": title,
                            "category": p.get("category", "codebase"),
                            "priority": p.get("priority", 5),
                            "description": description,
                            "proposed_action": (p.get("proposed_action") or "").strip(),
                            "evidence": (p.get("evidence") or "").strip(),
                            "reasoning": ((p.get("disclosure") or {}).get("reasoning") or "").strip(),
                        },
                    })
                    break
    return out


GOLD_GENERATION_PROMPT = """You are Claude (Opus 4.7) building a training-corpus gold example for the future Authoring LoRA.

Given the original topic and the coaching that caught the previous fabrication, author the CORRECTED clean proposal. The output trains v3 to drift toward this shape when given the same topic.

CONSTRAINTS:
- Cite ONLY things that exist. If unsure, omit the citation.
- If the topic genuinely has no real grounding evidence, EVIDENCE may be "no external citation, internal observation only" — that's better than fabricating.
- REASONING must be specifically articulated about THIS proposal (~120-200 chars), not boilerplate.
- TITLE ≤ 80 chars. CATEGORY one of: memory | codebase | tasks | resources | skills | capabilities.
- PRIORITY integer 1-10.

ORIGINAL TOPIC:
{topic}

COACHING (what was wrong in the prior draft):
{coaching}

ORIGINAL FAILED DRAFT (for reference — DON'T copy fabrications):
TITLE: {orig_title}
PROPOSED_ACTION:
{orig_action}
EVIDENCE:
{orig_evidence}
REASONING:
{orig_reasoning}

Output the CLEAN version. Strict format (no markdown fences, no commentary):

TITLE: <one short line, max 80 chars>
CATEGORY: <one of: memory | codebase | tasks | resources | skills | capabilities>
PRIORITY: <integer 1-10>
DESCRIPTION:
<3-5 sentences>

PROPOSED_ACTION:
<concrete steps, real file paths only>

EVIDENCE:
<real citations only; if none, say "no external citation, internal observation only">

REASONING:
<one paragraph, ≥100 chars, specific to THIS proposal>
"""


def parse_gold_draft(raw: str) -> dict:
    """Tolerant section parser."""
    sections = {}
    # Match section headers like "TITLE:", "CATEGORY:", etc.
    pattern = r"^\s*(TITLE|CATEGORY|PRIORITY|DESCRIPTION|PROPOSED_ACTION|EVIDENCE|REASONING)\s*:\s*"
    parts = re.split(pattern, raw, flags=re.MULTILINE)
    # parts = ['', 'TITLE', '...', 'CATEGORY', '...', ...]
    if len(parts) < 3:
        return {}
    for i in range(1, len(parts) - 1, 2):
        key = parts[i].upper()
        val = parts[i + 1].strip()
        sections[key] = val
    out = {
        "title": (sections.get("TITLE") or "").split("\n", 1)[0].strip()[:120],
        "category": (sections.get("CATEGORY") or "codebase").split("\n", 1)[0].strip().lower()[:30],
        "priority": 5,
        "description": sections.get("DESCRIPTION", ""),
        "proposed_action": sections.get("PROPOSED_ACTION", ""),
        "evidence": sections.get("EVIDENCE", ""),
        "reasoning": sections.get("REASONING", ""),
    }
    pri = sections.get("PRIORITY") or ""
    m = re.search(r"\d+", pri)
    if m:
        out["priority"] = max(1, min(10, int(m.group())))
    return out


async def generate_gold(failed_pair: dict) -> dict | None:
    from shared.llm import llm_generate
    from shared.companion import _strip_thinking
    from shared.bigbrain_context import bigbrain_system_prompt

    orig = failed_pair["original_draft"]
    prompt = GOLD_GENERATION_PROMPT.format(
        topic=failed_pair["topic"][:1500],
        coaching=failed_pair["coaching"][:1500],
        orig_title=orig.get("title", "")[:120],
        orig_action=orig.get("proposed_action", "")[:800],
        orig_evidence=orig.get("evidence", "")[:600],
        orig_reasoning=orig.get("reasoning", "")[:500],
    )
    system = bigbrain_system_prompt()
    try:
        r = await llm_generate("bigbrain", prompt=prompt, system=system, max_tokens=1500, temperature=0.4, timeout=90)
    except Exception as e:
        print(f"  [{failed_pair['prop_id']}] bigbrain exception: {e}")
        return None
    if not getattr(r, "success", False):
        print(f"  [{failed_pair['prop_id']}] bigbrain returned failure: {getattr(r, 'error', '?')}")
        return None
    raw = _strip_thinking(r.text or "")
    parsed = parse_gold_draft(raw)
    if not parsed.get("title") or not parsed.get("reasoning") or len(parsed["reasoning"]) < 80:
        print(f"  [{failed_pair['prop_id']}] gold parse incomplete; skipping")
        return None
    return parsed


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-gold", action="store_true", help="Generate gold drafts via bigbrain (slow)")
    parser.add_argument("--limit-gold", type=int, default=20, help="Max gold drafts to generate")
    args = parser.parse_args()

    from shared.llm import init_providers
    init_providers()

    props = load_proposals()
    print(f"loaded {len(props)} proposals (active + history)")

    discipline = load_discipline()
    print(f"discipline prefix loaded: {len(discipline)} chars")

    # Pass 1: positives from history
    positives = extract_positives(props)
    print(f"positives extracted: {len(positives)}")

    # Pass 2: failed drafts with coaching
    failed = extract_fail_with_coaching(props)
    print(f"failed-with-coaching drafts: {len(failed)}")

    corrections = []
    if args.with_gold:
        n = min(len(failed), args.limit_gold)
        print(f"generating {n} gold corrections via bigbrain (Opus 4.7)…")
        for i, fp in enumerate(failed[:n], 1):
            print(f"[{i}/{n}] generating gold for {fp['prop_id']} — '{fp['topic'][:60]}…'")
            gold = await generate_gold(fp)
            if not gold:
                continue
            corrections.append({
                "id": make_id(f"corr:{fp['prop_id']}"),
                "kind": "correction",
                "input": {
                    "topic": fp["topic"],
                    "discipline": discipline,
                    "coaching": fp["coaching"],
                },
                "target": gold,
                "metadata": {
                    "source": "bigbrain_correction",
                    "origin_proposal_id": fp["prop_id"],
                    "bigbrain_fail_reasoning": fp.get("bigbrain_reasoning", "")[:600],
                    "generated_at": time.time(),
                },
            })
    else:
        print("(skipping gold generation; rerun with --with-gold to include corrections)")

    # If we're NOT generating gold this run, preserve any existing corrections
    # from the file (they're expensive to regenerate). Positives are cheap, always re-extracted.
    preserved_corrections = []
    if not args.with_gold and OUT_PATH.exists():
        try:
            existing_ids = set()
            with open(OUT_PATH) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("kind") == "correction" and rec.get("id") not in existing_ids:
                        preserved_corrections.append(rec)
                        existing_ids.add(rec.get("id"))
            if preserved_corrections:
                print(f"preserved {len(preserved_corrections)} existing corrections (re-run with --with-gold to refresh)")
        except Exception as e:
            print(f"  [warn] preserve-existing-corrections read failed: {e}")

    # Write all (positives + freshly-generated corrections + preserved corrections)
    all_pairs = positives + corrections + preserved_corrections
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        for p in all_pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    print()
    n_pos, n_corr_new, n_corr_kept = len(positives), len(corrections), len(preserved_corrections)
    print(f"wrote {len(all_pairs)} pairs ({n_pos} positives + {n_corr_new} new corrections + {n_corr_kept} preserved) → {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
