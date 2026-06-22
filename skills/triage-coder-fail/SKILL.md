---
name: triage-coder-fail
description: How to triage a Coder plan that bigbrain BLOCKed during Pillar 3 pre-execution review. Use when a code-kind proposal fails the plan-review gate.
---

# Triage Coder Fail

## When to use

A code-kind proposal made it through /propose + shadow + resolver, but bigbrain returned `BLOCK` on the Coder's `/plan` output. Worker never executed. Need to figure out what the Coder LoRA got wrong and whether to redraft or escalate.

## Steps

1. **Read bigbrain's BLOCK reason.** Stored in `state/proposal_executor_state.jsonl` under the proposal's `bigbrain_plan_review` field. Most common: empty `deploy_patch` content, missing test step, plan references nonexistent files.
2. **Check the original proposal.** Was the EVIDENCE solid? If the proposal cited fabricated paths, the Coder built a plan on top of fiction — redraft the proposal, don't redraft the plan.
3. **Inspect the plan itself.** `POST /coder/plan` is logged in `state/coder_log.jsonl`. Look at what the Coder actually generated. Common Coder LoRA failure modes: copies prompt structure into the plan, hallucinates file refs, leaves placeholder TODOs.
4. **Decide route:**
   - **Redraft proposal** if EVIDENCE was bad → human re-curates context.
   - **Retry Coder** if plan was just noisy → /companion ask Coder to plan again with explicit constraints.
   - **Escalate to bigbrain** if Coder consistently fails on this category → bigbrain drafts the plan instead.
5. **Log the pattern.** If you see a Coder failure mode recurring, drop a note in `roux_notes_work.md` so the next session inherits the signal.

## Common BLOCK reasons (2026-05 observed)

- **Empty `deploy_patch.content`** — Coder LoRA generated metadata without actual patch text. Most common failure.
- **References to nonexistent test files** — Coder invents `tests/test_X.py` paths that don't exist.
- **Missing rollback step** — plan ships forward but no backout.

## Files involved

- `state/proposal_executor_state.jsonl` — bigbrain review outcomes
- `state/coder_log.jsonl` — raw Coder plan outputs
- `shared/proposal_executor.py::_handle_code` — 4-phase pipeline (plan → review → execute → review)
- `shared/bigbrain_review.py::bigbrain_review_planned_action(kind="code-plan")`
