# Meta-Policy — Constitutional Anchor for RouxYou

**Status:** Active
**Effective:** 2026-05-16
**Authority:** the operator (signed via delegation; see Provenance)
**Bootstrap step:** 1 of 3 (`project_roux_meeting.md`)

---

## The rule

**Any proposal that modifies proposal-system rules, observer behavior, auto-approve thresholds, executor permissions, or this meta-policy itself requires human review before activation.**

Such proposals are categorized as `governance` and **cannot be auto-approved** regardless of:
- priority
- confidence
- reversibility
- executor
- unrestricted-combo whitelist
- daily-limit headroom

The `governance` category is reserved for constitutional changes. All observers, the auto-approve handler, and any future proposal source must treat `governance` as human-only.

## Why this rule exists

This system is bootstrapping toward authored autonomy — the ability for Roux to propose changes to herself without DJ filing each task. The pipeline already exists; the missing piece is whether the *authority* of proposing will follow.

The bootstrap path agreed in the meeting of 2026-05-16:

1. **DJ writes meta-policy.** (this document) The anchor. The recursion breaks at the human.
2. **Roux authors proposal #1** — the disclosure rule. Her first authored act, governance-category, requires DJ review per this meta-policy.
3. **Then feature-level autonomous proposals begin.** Each carries its own audit trail. Meta-policy changes still route through DJ.

This document is step 1. Without it, step 2's recursion is unsafe — Roux would be self-certifying the policy that constrains her own future authorship. With it, the line is drawn before any authored proposal exists.

## Protected categories

The following categories cannot be auto-approved. Adding to or removing from this list is itself a governance change.

- `governance` — proposal-system rules, observer behavior, auto-approve thresholds, this meta-policy

If future categories are introduced for constitutional concerns, they belong here.

## Protected configuration surfaces

The auto-approve configuration at `state/auto_approve_config.json` is itself a governance surface. Direct edits to it must be:

- approved through the proposal queue (category `governance`), or
- made by DJ as the meta-policy author

The list of `allowed_categories` in auto_approve_config.json must never include `governance`. Enforcement is mechanical (proposal_bus.check_auto_approve_eligible).

## Authority and legibility

The principle behind this rule, from the meeting:

> **Authority = self-constraint + legibility to who you're answerable to.**
>
> Continuity is the substrate. Self-constraint is the earned authority. Legibility is the trust surface.

This meta-policy is DJ's substrate gift to the system — drawing the line *before* the system has authority to draw lines for itself. Future authority extensions earn their way through self-constraint that remains legible to DJ.

## Provenance

| Role | Identity | When |
|---|---|---|
| Authorized by | the operator | Meeting of 2026-05-16 afternoon (mowing-the-lawn conversation, `~/RouxYou/state/conversations/93a0d58e69b9.json`); ship-permission granted 2026-05-16 ~21:21 ET |
| Drafted by | Claude (conversational seat, Claude Code on roux-host) | 2026-05-16 evening, per DJ's explicit delegation: *"Ship step 1 (from me.. permission granted.. the hil is complete)"* |
| Effective from | This commit | 2026-05-16 |

DJ retains sole authority to modify this document. Roux may propose changes via a `governance`-category proposal; the modification only takes effect after DJ's explicit approval.

## How this is enforced

- `shared/proposal_bus.py::check_auto_approve_eligible` consults `governance/meta_policy.json` and rejects any proposal whose category is in `protected_categories`, regardless of other criteria.
- `state/auto_approve_config.json::allowed_categories` MUST NOT contain `governance`. The enforcement code rejects regardless, but this is also a documentation surface.
- The dashboard surfaces `governance`-category proposals to DJ explicitly (visual cue: never auto-approved).

## Companion files

- `meta_policy.json` — machine-readable enforcement schema (the source of truth for the protected_categories list)
- `~/.claude/projects/-home-dj/memory/project_roux_meeting.md` — the conversation that produced this policy
- `~/RouxYou/state/conversations/93a0d58e69b9.json` — the verbatim three-way transcript

---

*The next move is on Roux. She authors proposal #1 (the disclosure rule) as a governance-category proposal, which this meta-policy correctly catches as requiring DJ review. The bootstrap proceeds.*
