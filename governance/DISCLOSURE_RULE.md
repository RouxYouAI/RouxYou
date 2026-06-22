# Disclosure Rule — Traceability Required for Authored Proposals

**Status:** Active
**Effective:** 2026-05-16 (approved by DJ via dashboard following Proposal #1)
**Source proposal:** `prop_f732126bbdc0` — *Proposal #1: Enforce Proposal Traceability via Observer Verification*
**Authority:** Approved through the proposal queue under the meta-policy (`META_POLICY.md`)

---

## The rule

**Every proposal whose `source` field starts with `authored_by_` must include traceability fields that an observer can verify before the proposal can be approved.**

Heuristic-source proposals (`source="heuristic"`, the existing watchtower/proposer pipeline) are unaffected. The rule applies only to proposals authored by an autonomous agent (Roux, Claude-as-Roux, future authored sources).

## Required fields

Authored proposals must include a `disclosure` dict with:

| Field | Type | Validation |
|---|---|---|
| `source_url` | str | non-empty; if path-style, file must exist on disk |
| `authored_at` | float (unix timestamp) | non-future, non-prehistoric (after 2025-01-01) |
| `reasoning` | str | non-empty, ≥100 characters — articulated rationale, not boilerplate |
| `observer_verification` | dict | see below |

`observer_verification` must contain:

| Subfield | Type | Initial value |
|---|---|---|
| `status` | str | `"pending"` at publish; `"pass"` or `"fail"` after observer review |
| `comment` | str | observer's attestation text; empty allowed at publish |
| `verified_at` | float\|null | unix timestamp when observer marked status; null at publish |
| `verified_by` | str\|null | `"human"` or `"observer-agent"`; null at publish |

## What is enforced mechanically (at publish time)

`shared/proposal_bus.py::publish_proposal()` validates:

- presence of all four disclosure fields on `authored_by_*` proposals
- `source_url` is a non-empty string; if it looks like a filesystem path (starts with `/` or `~`), the path resolves
- `reasoning` is ≥100 chars
- `authored_at` is a sane timestamp (after 2025-01-01, not in the future)
- `observer_verification` has the four subfields with correct types

If any check fails, `publish_proposal()` returns `None` and the proposal is NOT written to the queue. The caller can fix and retry.

## What is enforced at approval time

`shared/proposal_bus.py::approve_proposal()` and `auto_approve_if_eligible()` both refuse to approve an `authored_by_*` proposal whose `observer_verification.status != "pass"`. The proposal stays `pending` until the observer marks it passed (or it gets dismissed).

This means an authored proposal can be in the queue, structurally valid, but un-approvable until the observer attests.

## Observer role

The mechanical checks verify *form*. The human observer (or future observer-agent) verifies *substance*:

- Does the `source_url` actually ground this proposal, or is it a plausible-shape citation?
- Is the `reasoning` genuinely the agent's rationale, or pattern-completed boilerplate?
- Does the proposal cross the curiosity threshold or is it generated to fill the queue?

This is the trust-surface piece. Mechanical checks catch sloppy proposals; observer attestation catches the failure mode the proposal #1 EVIDENCE section demonstrated — fabricated citations that look correctly-shaped.

## Forward-only

Proposal #1 itself, which established this rule, predates it. The disclosure rule applies to proposals authored *after* its approval. The proposal that established the rule is preserved at `prop_f732126bbdc0` in `state/proposals_history.json` with its full provenance disclosure baked into the EVIDENCE section — which is the spirit of the rule, even if it doesn't carry the formal `disclosure` field schema.

## Companion files

- `disclosure_rule.json` — machine-readable enforcement schema
- `META_POLICY.md` — the meta-policy that made this rule's approval require human review (and still does, for any future amendments)
- `~/.claude/projects/-home-dj/memory/project_bootstrap_steps_1_2.md` — full origin story

## Amendment process

Per the meta-policy, any change to this rule is itself a `governance`-category proposal requiring human review. DJ can also edit `disclosure_rule.json` directly (he owns the file), but per the meta-policy, that's a governance surface and should be journaled in proposal history if done.

---

*Step 2 of the bootstrap is shipped: queue approval + code enactment. Step 3 (feature-level autonomous proposals) is now unblocked — but every such proposal must carry its disclosure or it won't reach the queue.*
