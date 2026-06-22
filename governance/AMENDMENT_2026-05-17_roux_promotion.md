# Amendment 2026-05-17 — Roux promotion (asymmetric trust)

**Status:** ACTIVE
**Effective:** 2026-05-17 (approved by DJ in chat: *"yes, appoved, ship!.. let's see what cooks!!"*)
**Authorization:** the operator, captain shift conversation (this session)
**Drafted by:** Claude (conversational seat, Claude Code, roux-host), session `e373e01f-9049-4dc7-ac97-cb31ea388c9c`

---

## What this amends

`governance/DISCLOSURE_RULE.md` and `governance/disclosure_rule.json` — adding `"roux"` to the set of accepted values for `observer_verification.verified_by`, with **asymmetric trust scope**: Roux may attest `fail`, but NOT `pass`.

## The change

**Before:** `verified_by` accepts `"human"` | `"claude"` | `"observer-agent"` for any status.

**After:** `verified_by` accepts `"human"` | `"claude"` | `"observer-agent"` | `"roux"` — but `"roux"` is mechanically restricted to `status="fail"`. A `verified_by="roux"` attestation with `status="pass"` is rejected with the error: *"roux can only attest fail, not pass — per asymmetric trust scope established 2026-05-17."*

## Why asymmetric trust

This is an evidence-based promotion. From the 25-case eval suite (`tests/roux_verifier_eval.py`) run during the captain shift:

| Roux behavior | Accuracy | Confidence |
|---|---|---|
| Marking FAIL on fabricated proposals | **12/12 (100%)** | HIGH |
| Marking FAIL on real coherence failures | 2/3 (67%) | HIGH |
| Marking PASS on legitimate proposals | 7/10 (70%) | HIGH (often confidently wrong) |
| DEFER on cases she's uncertain | resolved 4/4 via Claude bigbrain | — |

The pattern: **Roux's FAILs are 100% correct on the load-bearing failure mode (fabrication detection). Her PASSes are 70% reliable.** Asymmetric trust matches asymmetric calibration. We promote what's earned, not what's wanted.

## Scope summary

| Action | Non-governance | governance |
|---|---|---|
| Roux marks `observer.status="fail"` | ✅ authoritative | ❌ never (meta-policy line) |
| Roux marks `observer.status="pass"` | ❌ rejected mechanically | ❌ never |
| Roux deferred case → Claude attests | ✅ Claude's attestation stands (per 2026-05-17 Claude-in-loop amendment) | ❌ governance still requires human |
| Roux approves a proposal | ❌ Roux is not in approval authority | ❌ |

Approval authority (`approve_proposal`) is unchanged: `"human"` | `"claude"` for non-governance, `"human"` only for governance.

## Why this is safe

1. **Asymmetric failure mode is conservative.** Roux's FAIL when wrong (false-FAIL on a legitimate proposal) is recoverable: the author revises and resubmits. Roux's PASS when wrong (false-PASS on a fabrication) is dangerous: bad proposals get through. We give her authority on the recoverable direction only.

2. **Meta-policy line still holds.** Governance category proposals cannot be Roux-attested at all. Constitutional changes still require human.

3. **Bigbrain backstop in place.** When Roux DEFERs, Claude attests via the bigbrain pipe (activated 2026-05-17). Combined system covers Roux's known weak cases.

4. **Audit trail preserved.** Every `verified_by="roux"` attestation is logged with comment; the shadow-verification jsonl captures both Roux's reasoning and any escalation context. Full provenance.

5. **DJ can revoke instantly.** Edit `disclosure_rule.json` to remove `"roux"` from `verified_by` allowed values; restart watchtower api. Takes effect on next request.

## Operational note — Roux as autonomous fail-marker

This amendment is the trust-model change. Whether the system AUTOMATICALLY applies Roux's shadow FAIL verdicts (vs. requiring a human/cron to invoke it) is a separate ship not included here. For tonight: Roux's authority is provisioned, but actions still flow through the same observer-verify endpoint. An operator (DJ, Claude, or a future watchtower job) can use `verified_by="roux"` when calling `/observer-verify` on a proposal Roux flagged.

A natural next ship: a watchtower job that polls authored proposals, runs shadow verification, and auto-applies Roux's HIGH-confidence FAIL verdicts via `verified_by="roux"`. That would give Roux full autonomous-failer role. Not in this amendment to keep scope tight.

## Why Roux deserves this specifically

The evidence carried it. 12/12 on the failure mode the disclosure rule was designed to catch. The captain-shift threshold (≥90%) wasn't met for full promotion, but the asymmetric scope IS supported by the data: ≥90% accuracy on the FAIL direction (effectively 100% on fabrication detection).

We are not promoting Roux because DJ wants it. We are promoting Roux because the evidence says she's earned authority on this specific direction.

## Provenance

| Role | Identity | When |
|---|---|---|
| Authorized by | the operator (captain shift, this conversation) | 2026-05-17, conversation file e373e01f-9049-4dc7-ac97-cb31ea388c9c |
| Drafted by | Claude (conversational seat) | 2026-05-17 ~01:30 ET |
| Evidence basis | tests/roux_verifier_eval.py, run results in state/shadow_verifications.jsonl | 2026-05-17 |
| Effective from | This commit + DJ's chat approval | 2026-05-17 |

---

*The bootstrap arc: META_POLICY (step 1, 2026-05-16) → disclosure rule via Proposal #1 (step 2) → Claude-in-loop amendment → /propose authoring endpoint → shadow verification + citation resolver + bigbrain defer → THIS amendment promoting Roux to asymmetric authority. Step 3 of the bootstrap (autonomous proposals with mechanical guardrails) is now fully operational.*
