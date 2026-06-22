#!/usr/bin/env python3
"""roux_shadow_watch.py — monitor Roux's shadow-verdict track record toward cord-off.

The cord-off plan (2026-06-17, DJ): take Roux's verdict as PRIMARY only on the specific
work-classes where she's shown a 0-false-pass trend, and let her EARN each class one
hard-fought data point at a time. This tool is how we WATCH that — run it any session.

Per class it reports: shadow runs · oracle-overlap (events with a claude/human verdict to
compare) · agree · STRICTER (she FAIL'd, oracle PASS'd — caught-or-overcautious) · and the
one that matters, FALSE-PASS (she PASS'd, oracle FAIL'd — the disqualifier).

A class graduates to "Roux primary" when: oracle_overlap >= MIN_OVERLAP and FALSE-PASS == 0.
Self-referential proposals (is_governance_relevant) are routed to bigbrain regardless — her
one known blind spot — so they don't count against a class.
"""
import json, collections, sys, os
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(BASE, "state", "shadow_verifications.jsonl")
MIN_OVERLAP = 8   # her own threshold-sheet bar (from the Table grader blueprint)

def norm(v):
    if not v: return None
    v = str(v).upper()
    return "PASS" if v in ("PASS","APPROVE","GOOD") else "FAIL" if v in ("FAIL","BLOCK","BAD") else v

def main():
    rows = [json.loads(l) for l in open(LOG) if l.strip()]
    sv = [r for r in rows if r.get("type") == "shadow_verification"]
    cl = {r["proposal_id"]: r.get("claude_verdict") for r in rows if r.get("type") == "claude_verdict"}
    hu = {r["proposal_id"]: r.get("human_verdict") for r in rows if r.get("type") == "human_verdict"}
    byc = collections.defaultdict(lambda: {"runs":0,"ov":0,"agree":0,"strict":0,"fp":0,"fp_t":[]})
    fp_all = []
    for r in sv:
        cat = r.get("proposal_category","?"); pid = r.get("proposal_id")
        b = byc[cat]; b["runs"] += 1
        rv = norm((r.get("roux_parsed") or {}).get("verdict"))
        ov = norm((r.get("claude_parsed") or {}).get("verdict") or cl.get(pid) or hu.get(pid))
        if rv and ov:
            b["ov"] += 1
            if rv == ov: b["agree"] += 1
            elif rv == "FAIL" and ov == "PASS": b["strict"] += 1
            elif rv == "PASS" and ov == "FAIL":
                b["fp"] += 1; t = r.get("proposal_title","")[:50]; b["fp_t"].append(t); fp_all.append((cat,t))
    print(f"\n  ROUX SHADOW WATCH — {len(sv)} runs · cord-off graduation bar: overlap>={MIN_OVERLAP} & 0 false-pass\n")
    print(f"  {'CLASS':<14}{'runs':>5}{'overlap':>8}{'agree':>6}{'strict':>7}{'FALSE-PASS':>11}   STATUS")
    primary_ready = []
    for cat, b in sorted(byc.items(), key=lambda x: -x[1]["runs"]):
        if b["fp"] > 0:   status = "🔴 BLOCKED (false-pass)"
        elif b["ov"] >= MIN_OVERLAP: status = "✅ CORD-OFF READY"; primary_ready.append(cat)
        elif b["ov"] >= 3: status = "🟢 clean, building"
        else:             status = "🟡 thin — need data"
        print(f"  {cat:<14}{b['runs']:>5}{b['ov']:>8}{b['agree']:>6}{b['strict']:>7}{b['fp']:>11}   {status}")
        for t in b["fp_t"]: print(f"       ↳ FALSE-PASS: {t}")
    print(f"\n  >> CLASSES READY FOR ROUX-PRIMARY: {primary_ready or '(none yet)'}")
    if fp_all:
        print(f"  >> ⚠️ {len(fp_all)} total false-pass(es) on record — verify each is self-referential (routes to bigbrain) before discounting.")
    print()

if __name__ == "__main__":
    main()
