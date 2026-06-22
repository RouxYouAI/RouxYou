# Design Doc: Automated Fuel Pipeline ("self-feeding coach")

Status: PROPOSED (2026-06-02). Author: Claude (Opus 4.8) + DJ.
Related: [[project_research_derived_backlog]], the retired `web_researcher`, the
knowledge-ingest pipe (`shared/knowledge_ingest.py`), reflection_coach.

## 1. The goal

Close the loop that is currently MANUAL. Today, fuel for Roux's reflection loop is made by hand:
1. **Discover** — DJ hunts article/paper links across a span of topics (web search).
2. **Digest → fuel** — Claude reads each, judges relevance to RouxYou, and authors *grounded*
   work-items (referencing real files/symbols) into `state/knowledge_inbox/`.
3. **Consume** — `knowledge_ingester` → `external_knowledge` memory → reflection_coach surfaces
   it → drafts a grounded proposal → Pillar-3 executes. **(Already automated.)**

This doc specs automating stages 1 and 2 so the system feeds itself.

## 2. The hard lesson (why naive automation fails)

The OLD `web_researcher` already did "web-search → auto-generate proposals" and was **retired**
for producing low-signal garbage (confabulated relevance, browser-bleed, wrong-port confab,
unbounded spend). The manual curation pipe exists *because that failed*. So this is not a new
idea — it is a **re-attempt of a known-failed approach**, and the design must answer: *what is
different now that makes the quality hold?*

**The crux: who does the DIGEST (judgment) step?**

LIVE TEST 2026-06-02 (n=1, OSCAR article — a judgment trap whose correct answer is "D: skip,
SGLang/Triton/H100-only, not our stack"): v5.3 got the JUDGMENT RIGHT — it caught the real
disqualifier ("RouxYou uses Vulkan which has no support for Triton kernels... incompatible with
their current stack") and did NOT confabulate an enthusiastic A. But it FAILED the FORMAT (junk
token, prose instead of the requested fields, truncated verdict). This is the well-documented
v5.3 "substance vs form" pattern (cf. project_self_propose_substance_finding + the coder saga):
v5.3 REASONS well but can't reliably emit clean STRUCTURED output.

Implication: the digest's hard part (judgment) is within v5.3's reach; the easy part (format +
grounded-work-item authoring) is exactly what the content-gen extractor + codebase-index grounding
+ symbol-backstop already solve. So the digest step can likely run on v5.3 OFFLINE (zero bigbrain
$) IF wrapped with that structured-output scaffolding. **bigbrain becomes the FALLBACK** (high-
stakes / guaranteed-clean-structure), not the default — better aligned with offline-first
(bigbrain = checked exception). Still validate at scale before trusting it unsupervised, and keep
a bigbrain spot-check on a sample of v5.3's verdicts.

## 3. Architecture (staged)

```
[DISCOVER]  DJ curates TOPICS (not links) in a config file (e.g. state/fuel_topics.json)
            → scheduled SearXNG search per topic via shared/search.py web_search()
            → collect candidate URLs
                ↓
[DEDUP]     drop URLs already ingested (content/URL hash vs state/knowledge_inbox/processed/
            and a seen-URL ledger). Cheap, local. (knowledge_ingest already dedups by content hash.)
                ↓
[TRIAGE]    cheap local-model OR heuristic pass drops obvious junk (off-topic, marketing,
            dead links, paywalled-with-no-preview). NOT judgment — just a coarse filter to
            cut the bigbrain bill.
                ↓
[DIGEST]    BIGBRAIN reads the N survivors (BOUNDED + cost-capped per run):
            - applies the SELECTIVE relevance bar (maps to a real RouxYou need? else drop)
            - queries the codebase index → authors a GROUNDED work-item that references REAL
              files/symbols (reuse the grounding mechanism from shared/authoring.py)
            - writes it as a .md in a STAGING dir, not the live inbox
                ↓
[STAGE]     candidate fuel lands in state/fuel_staging/ (NOT state/knowledge_inbox/)
                ↓
[GATE]      Phase 1 (HIL): DJ/Claude reviews staged fuel, moves approved files into
            state/knowledge_inbox/ (then the existing knowledge_ingester takes over).
            Phase 2 (earned): once the pipeline's fuel proves high-signal over time, auto-promote
            staging → inbox. Mirrors HIL-is-provisional / the trust-ledger graduation pattern.
```

## 4. Key design choices

- **DJ curates TOPICS, the machine hunts LINKS.** Keeps DJ's taste/priorities in the loop (the
  real value of hand-hunting) while automating the legwork. Topic list in `state/fuel_topics.json`.
- **bigbrain digests, local model never judges.** The lesson from the retired web_researcher.
- **Bounded + cost-capped.** Max articles/run, max bigbrain calls/day. The old one was unbounded.
- **Staged human gate, then graduate.** Start with DJ approving staged fuel; remove the human
  once proven. Same earn-trust philosophy as the coder.
- **Reuse existing plumbing.** `shared/search.py` (search), `shared/knowledge_ingest.py`
  (ingest_content / the inbox), the codebase-index grounding from `shared/authoring.py`, the
  CronJob registration pattern in `services/watchtower/api.py`.

## 5. Why this beats the retired web_researcher

| Failure of old web_researcher | This design |
|---|---|
| weak local model judged relevance → confab | v5.3 judges (proven to catch the OSCAR trap, no confab) wrapped in structured-output scaffolding; bigbrain spot-checks/fallback |
| no grounding → phantom references | reuses codebase-index symbol grounding |
| unbounded → spend-leak + queue noise | bounded/cost-capped per run |
| published straight to queue | staged + human gate, graduate later |

## 6. Cost

DISCOVER/DEDUP/TRIAGE/STAGE = free local plumbing. DIGEST = real bigbrain $ per article (small;
a few articles/run, cents each, capped). That ongoing cost is the price of quality and is the
reason for the bounded/triage design.

## 7. Suggested build order (increments)

1. **DISCOVER + DEDUP + STAGE** (free local plumbing): topic config → SearXNG search → dedup →
   write raw candidate list to `state/fuel_staging/`. Lets DJ SEE the candidate firehose and tune
   topics before paying for digestion. **← recommended first increment.**
2. **TRIAGE**: cheap junk filter.
3. **DIGEST (bigbrain)**: relevance bar + grounded work-item authoring into staging.
4. **GATE**: human-approve staging→inbox; later auto-promote.
5. **CRON**: register as an autonomy-gated CronJob (low cadence, e.g. daily).

## 8. Open questions

- Topic list: static config, or can reflection itself nominate new topics from observed gaps?
- Triage model: local v5.3, a tiny classifier, or pure heuristics?
- Graduation criteria for staging→auto-inbox (signal-rate threshold, like the trust ledger)?
- Paywalled sources: fetch-what's-accessible vs skip?
