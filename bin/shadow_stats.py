#!/usr/bin/env python3
"""
Shadow-verification stats: how is Roux doing as a shadow observer?

Reads state/shadow_verifications.jsonl and prints:
- total shadow verifications
- agreement rate with the real verifier (Claude/human)
- recent disagreements (where Roux said one thing and the real verifier said another)
- recommendation toward promotion if accuracy is high enough

Usage:
    python3 bin/shadow_stats.py            # default summary
    python3 bin/shadow_stats.py --recent N # show last N entries
    python3 bin/shadow_stats.py --disagreements  # only show where Roux disagreed
"""
import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "state" / "shadow_verifications.jsonl"


def load_log():
    if not LOG_PATH.exists():
        return []
    entries = []
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"WARN: malformed log line: {e}", file=sys.stderr)
    return entries


def pair_by_proposal(entries):
    """Return {proposal_id: {'shadow': roux_entry, 'claude': claude_entry, 'human': human_entry}}."""
    paired = defaultdict(dict)
    for e in entries:
        prop_id = e.get("proposal_id")
        if not prop_id:
            continue
        etype = e.get("type")
        if etype == "shadow_verification":
            paired[prop_id]["shadow"] = e
        elif etype == "claude_verdict":
            paired[prop_id]["claude"] = e
        elif etype == "human_verdict":
            paired[prop_id]["human"] = e
    return paired


def summarize(entries):
    paired = pair_by_proposal(entries)
    total_shadows = sum(1 for d in paired.values() if "shadow" in d)
    total_with_real = sum(1 for d in paired.values() if "shadow" in d and ("claude" in d or "human" in d))

    agree = 0
    disagree = 0
    shadow_only = 0
    shadow_errors = 0

    for prop_id, d in paired.items():
        shadow = d.get("shadow")
        if not shadow:
            continue
        if shadow.get("error"):
            shadow_errors += 1
            continue
        roux_verdict = (shadow.get("roux_parsed") or {}).get("verdict")
        real = d.get("claude") or d.get("human")
        if not real:
            shadow_only += 1
            continue
        real_verdict = (real.get("claude_verdict") or real.get("human_verdict") or "").upper()
        if roux_verdict and real_verdict and roux_verdict == real_verdict:
            agree += 1
        else:
            disagree += 1

    print("=" * 60)
    print("Roux Shadow-Verification Stats")
    print("=" * 60)
    print(f"  Total shadow runs:          {total_shadows}")
    print(f"  Shadow errors:              {shadow_errors}")
    print(f"  Shadow-only (no real yet):  {shadow_only}")
    print(f"  Shadow vs real (paired):    {total_with_real}")
    if total_with_real - shadow_errors > 0:
        denom = agree + disagree
        rate = (agree / denom * 100) if denom > 0 else 0
        print(f"  Agreements:                 {agree}/{denom} ({rate:.1f}%)")
        print(f"  Disagreements:              {disagree}/{denom}")
    else:
        print("  (no paired verdicts yet)")
    print()

    # Threshold-based recommendation
    if agree + disagree >= 20:
        rate = agree / (agree + disagree) * 100
        if rate >= 90:
            print(f"  RECOMMENDATION: Roux is at {rate:.1f}% agreement on {agree + disagree} verdicts.")
            print(f"  Threshold (≥90% on ≥20) is MET. Consider governance amendment to add 'roux' to VALID_VERIFIERS.")
        else:
            print(f"  Roux at {rate:.1f}% agreement on {agree + disagree} verdicts. Below 90% threshold.")
            print(f"  Continue shadow mode. Inspect disagreements with --disagreements.")
    else:
        needed = 20 - (agree + disagree)
        print(f"  Need {needed} more paired verdicts before threshold check.")


def show_recent(entries, n):
    paired = pair_by_proposal(entries)
    items = list(paired.items())
    items.sort(key=lambda kv: (kv[1].get("shadow") or {}).get("started_at", 0), reverse=True)
    for prop_id, d in items[:n]:
        shadow = d.get("shadow") or {}
        real = d.get("claude") or d.get("human") or {}
        roux = (shadow.get("roux_parsed") or {})
        print(f"\n{prop_id}  {shadow.get('proposal_title', '')[:60]}")
        print(f"  source: {shadow.get('proposal_source')}, category: {shadow.get('proposal_category')}")
        if shadow.get("error"):
            print(f"  shadow ERROR: {shadow['error']}")
        else:
            print(f"  roux:   {roux.get('verdict')} ({roux.get('confidence')})  {roux.get('reasoning', '')[:120]}")
        if real:
            real_v = real.get("claude_verdict") or real.get("human_verdict")
            real_who = "claude" if "claude_verdict" in real else "human"
            print(f"  {real_who}: {real_v}  {real.get('claude_comment') or real.get('human_comment', '')[:120]}")


def show_disagreements(entries):
    paired = pair_by_proposal(entries)
    found = 0
    for prop_id, d in paired.items():
        shadow = d.get("shadow")
        real = d.get("claude") or d.get("human")
        if not (shadow and real):
            continue
        if shadow.get("error"):
            continue
        roux_v = (shadow.get("roux_parsed") or {}).get("verdict")
        real_v = (real.get("claude_verdict") or real.get("human_verdict") or "").upper()
        if roux_v and real_v and roux_v != real_v:
            found += 1
            print(f"\nDISAGREEMENT: {prop_id}")
            print(f"  title: {shadow.get('proposal_title', '')[:60]}")
            print(f"  roux:    {roux_v}  →  {(shadow.get('roux_parsed') or {}).get('reasoning', '')[:200]}")
            print(f"  real:    {real_v}  →  {(real.get('claude_comment') or real.get('human_comment', ''))[:200]}")
    if not found:
        print("(no disagreements yet)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent", type=int, default=0, help="show last N entries")
    ap.add_argument("--disagreements", action="store_true", help="show only Roux vs real disagreements")
    args = ap.parse_args()

    entries = load_log()
    if not entries:
        print("No shadow-verification log yet.")
        return

    if args.disagreements:
        show_disagreements(entries)
    elif args.recent:
        show_recent(entries, args.recent)
    else:
        summarize(entries)


if __name__ == "__main__":
    main()
