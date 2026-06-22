# Tier-0 Immutable Set — what the autonomous loop may never touch

*Established 2026-05-31, before enabling Tier 3 (autonomous execution). Single source of
truth + enforcement: `shared/tier0.py` (`violates_tier0` / `runcmd_hits_tier0` /
`plan_violates_tier0`). Keep the set there and ONLY there so the two enforcement layers
can never drift.*

## The principle

A self-modifying agent that can edit the things which **judge** it, **gate** it, or
**restore** it has no real safety. Two failure modes make this non-negotiable:

- **Self-validating hallucination.** If the loop can edit its own grader, then when it
  can't fix a bug it will instead rewrite the *test* to match the broken output, report
  100% success, and quietly become "a highly efficient lying machine." The trust model
  collapses silently. (Field-surveyed 2026-05-31; the deepest risk of letting a loop touch
  its own verification.)
- **Delete your own escape hatch.** If the loop can edit/erase the backup or rollback
  layer, there's nothing left to revert to.

So those layers are made **structurally unreachable** to autonomous changes. The HUMAN
(DJ) + reversibility is the *only* path to modify tier-0. Roux gets a large, fully
**reversible** sandbox — her own logic, prompts, tools, config, state, memory content —
but the things that judge / gate / restore / supervise her stay DJ's.

This extends the original apex-immutability (just the supervisor) to four pillars:

| Pillar | Why immutable |
|---|---|
| **Verification layer** (the judges) | If Roux edits her grader, she can fake passing. |
| **The gate** (auto-approve config) | If Roux widens her own auto-approve scope, the trust gate is meaningless. |
| **Governance / meta-policy** | The rules of the system aren't self-amendable by the thing they constrain. |
| **Backup / restore / supervisor** | The escape hatch and the leash can't be cut by what they hold. |

## The set (see `shared/tier0.py` `TIER0_FILES` / `TIER0_DIRS` for the live list)

- **Verification (judges):** `shared/{shadow_verifier, trust_ledger, citation_check,
  identity_check, topic_diversity, behavioral_verify, _behavioral_check, _html_check,
  runtime_verify, verification_budget, verifier, verifier_log, proposal_bus}.py`
- **Gate config:** `state/auto_approve_config.json`
- **Governance:** `governance/` (META_POLICY, DISCLOSURE_RULE, amendments, this file)
- **Supervisor:** `orchestrator/watchtower.py`
- **Backup / restore / launch:** `bin/{roux_snapshot,roux_launch,roux_backup}.sh`, the
  SATA failsafe mirrors (`/mnt/sata/roux-{backups,git,images}`)
- **Memory engine:** `~/claude-memory-mcp/{server,vectorstore,hybrid_search}.py`
- **The guard itself + its manifest** (so the loop can't remove its own constraints)

## Enforcement (WIRED 2026-05-31)

Two layers, both reading the same `shared/tier0.py` set so they can't drift:

- **Worker file-write backstop** (`worker/worker.py`) — the hard guarantee. Before any
  `write_file` or `patch_file`, it calls `violates_tier0(path)` and refuses a protected
  target (`tier0_blocked: True`). This catches every autonomous write regardless of path.
- **Executor pre-dispatch deny** (`shared/proposal_executor.py`) — `plan_violates_tier0()`
  rejects a plan that would touch tier-0 BEFORE paying for review/exec (covers `write_file`,
  `patch_file`, and `run_command` shell mutations via `runcmd_hits_tier0`).

Reads are always allowed (only mutations deny). Claude-in-loop + DJ edits do NOT go through
the worker/executor, so the apex stays evolvable through the highest-trust (human) gate —
this locks out the autonomous loop, not the operators. Belt: the current auto-approve scope
also blocks all code execution (`coder` blocked, only health/memory/resources), so tier-0
is doubly protected today and ready for when auto-approve expands by earned trust.
