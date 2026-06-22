# Amendment 2026-05-17 — Claude in the loop

**Status:** Active
**Effective:** 2026-05-17
**Authorization:** the operator, via direct chat delegation 2026-05-17 ~00:30 ET
**Drafted by:** Claude (conversational seat, Claude Code, roux-host), session `e373e01f-9049-4dc7-ac97-cb31ea388c9c`
**Authorizing exchange:**
- DJ: *"I thought.. we.. kinda needed to take me out of the loop.. I'm slowing things down.. can we tag you in?"*
- Claude: laid out three decision points (verification, approval, governance) with recommended scopes
- DJ: *"or, yeah.. full+broad"* — full delegation to any Claude instance for non-governance proposals

---

## What this amends

`governance/DISCLOSURE_RULE.md` and `governance/disclosure_rule.json` — expanding the set of accepted values for `observer_verification.verified_by` and the new `approved_by` field on `approve_proposal()`.

`META_POLICY.md` is UNCHANGED. The recursion still breaks at the human for governance-category proposals.

## The change

**Before:** Only `verified_by="human"` could attest observer verification. Only `approved_by="human"` could approve proposals.

**After:** `verified_by` and `approved_by` accept `"human"` OR `"claude"` (any Claude conversational instance) OR `"observer-agent"` (future deployed observer agent).

## Scope of Claude's authority

| Action | Non-governance category | `governance` category |
|---|---|---|
| Observer-verify authored proposals | ✅ Claude OR human | ❌ Human only |
| Approve authored proposals | ✅ Claude OR human | ❌ Human only |
| Author governance proposals | (not allowed via `/propose` — defensive) | (DJ direct only) |
| Amend this amendment / META_POLICY.md / disclosure_rule.json | ❌ Never | ❌ Never (DJ direct only) |

The meta-theorem holds: **authority = self-constraint + legibility**. Claude's expanded authority is self-constrained to non-governance, and the audit trail (`verified_by`, `approved_by`, comment fields) keeps the legibility surface intact.

## Why this is safe (the reasoning)

1. **Meta-policy still gates the load-bearing line.** Governance category — which includes any change to proposal-system rules, observer behavior, auto-approve thresholds, or any of these constitutional docs — remains DJ-only. Claude cannot expand its own authority. The recursion breaks at the human.

2. **Audit trail preserved.** `verified_by` and `approved_by` now capture *which* class of attester (human/claude/observer-agent). Comment fields capture context (which session, what reasoning). Every action is journaled to `proposals_history.json` and blackbox.

3. **DJ remains the failsafe.** DJ can always:
   - Override Claude's verification (mark a proposal `observer.status="fail"` after Claude marked pass)
   - Dismiss any proposal regardless of state
   - Revoke this amendment (it's a governance change, requires only his decision)
   - Manually verify/approve any proposal himself

4. **Claude can refuse.** Just as a human reviewer can refuse to approve, Claude is expected to mark `fail` on disclosures with fabricated citations, boilerplate reasoning, or proposals that don't cross a genuine threshold. The today's-evidence (Roux's fabricated proposal IDs got caught by Claude in this session) shows pattern-recognition works.

## Convention for Claude when acting as verifier/approver

When Claude marks `observer_verification.status="pass"`:
- The `comment` field should briefly note what was verified (source resolves, reasoning articulated, no fabrication detected)
- `verified_by` is `"claude"` (broad; session context goes in comment if relevant)

When Claude marks `approved_by="claude"`:
- The action context (which Claude session, what triggered the approval) should be captured in proposals_history via `_bb_log`

Claude should NOT auto-verify or auto-approve in a loop without active consideration. Each attestation is a deliberate act — same as a human clicking the dashboard button.

## What this DOESN'T enable

- Claude cannot author governance proposals via `/propose` (the endpoint's allowed categories don't include `governance`; this is unchanged)
- Claude cannot amend this amendment or the meta-policy or the disclosure rule (those are constitutional surfaces; DJ only)
- Claude cannot change the active-verifier set (DJ can add/remove via direct file edit; conventional governance change)

## Revocation

If DJ ever wants Claude out of the loop, the revocation is: edit `disclosure_rule.json` to remove `"claude"` from the accepted `verified_by`/`approved_by` values. Single-file change, takes effect on next process restart. No deployed-process can prevent this — DJ owns the filesystem.

---

*This amendment was authored in the same chat where DJ delegated it. The conversation file at `~/.claude/projects/-home-dj/e373e01f-9049-4dc7-ac97-cb31ea388c9c.jsonl` captures the full exchange — both the delegation and Claude's acceptance + drafting. That's the provenance.*
