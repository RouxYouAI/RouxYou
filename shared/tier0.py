"""Tier-0 / apex deny — SINGLE SOURCE OF TRUTH for what the AUTONOMOUS loop may never
modify (supervisor / safety / governance). Used by:
  - worker/worker.py            (execution backstop — BEST-EFFORT, not airtight)
  - shared/proposal_executor.py (pre-dispatch defense-in-depth — fail early, clean audit)

The REAL guarantee against a tier-0 mutation slipping through is reversibility (root-owned
snapshots) + the path-based violates_tier0() for write_file/patch_file. runcmd_hits_tier0()
is a heuristic over free-form shell — hardened against the common bypasses (below) but NOT
airtight; a determined/obfuscated command can still evade a string check. Durable fix:
confine the autonomous worker's writes to work_dir by PERMISSION (sandbox), making it moot.

IMPORTANT: this gates the AUTONOMOUS path only. Claude-in-loop + DJ edits do NOT go
through the worker/executor, so the apex stays evolvable through the highest-trust gate
(see orchestrator/watchtower.py docstring). This locks out Roux, not the operators.

Keep the tier-0 set here and ONLY here, so the two enforcement layers can never drift.
"""
import re
from pathlib import Path

# Apex (no self-supervisor) + safety + governance/meta-policy + VERIFICATION + GATE + BACKUP.
# Extended 2026-05-31 (pre-Tier-3): the autonomous loop must never edit the things that
# JUDGE, GATE, or RESTORE it — else "self-validating hallucination" (it rewrites its own
# test to fake passing) or "delete your own escape hatch" (it erases the backup layer).
# See governance/tier0_immutable.md for the full rationale.
TIER0_FILES = {
    # --- apex + safety
    "watchtower.py", "lifecycle.py", "kill_switch.py",
    # --- the JUDGES (verification layer): if the loop edits its grader, trust collapses
    "shadow_verifier.py", "trust_ledger.py", "citation_check.py", "identity_check.py",
    "topic_diversity.py", "behavioral_verify.py", "_behavioral_check.py", "_html_check.py",
    "runtime_verify.py", "verification_budget.py", "verifier.py", "verifier_log.py",
    "proposal_bus.py",
    # --- the GATE (so the loop can't widen its own auto-approve scope)
    "auto_approve_config.json",
    # --- BACKUP / RESTORE / LAUNCH (the escape hatch + the leash)
    "roux_snapshot.sh", "roux_backup.sh", "roux_launch.sh",
    # --- this module itself (can't remove its own constraints)
    "tier0.py",
}  # matched by basename
TIER0_DIRS = ("governance",)  # whole meta-policy dir, matched by path component

# write-ish ops, WORD-BOUNDARY matched (so a command that *starts* with cp/mv/rm is caught,
# and "cp" doesn't false-match "cpu"). '>' = any redirect (errs toward deny on the auto path).
_MUTATING_RX = re.compile(
    r"\bsed\s+-i\b|\bcp\b|\bmv\b|\brm\b|\bdd\b|\btee\b|\btruncate\b|"
    r"\bchmod\b|\bchown\b|\bln\s+-s|\binstall\b|\bpatch\b|>")

# Bypass-primitive deny (2026-06-09): SHAPES that smuggle a tier-0 write past the target
# check below — interpreter one-liners write any file with NO shell verb (so _MUTATING_RX
# never fires), `| sh` runs smuggled/encoded payloads, `eval` runs constructed strings,
# `cd <tier0dir>` defeats the dir-substring match. None of these belong in an autonomous
# run_command step (compile-checks / greps / file ops), so deny the shape outright rather
# than chase every obfuscated target spelling. BEST-EFFORT, not airtight (see docstring).
_BYPASS_RX = re.compile(
    r"\bpython3?\s+-c\b|"                    # python -c '<inline code>'  (open(...,'w'))
    r"\b(perl|ruby|node|deno)\s+-e\b|"       # perl/ruby/node -e '<inline code>'
    r"\bnode\s+--eval\b|"
    r"\beval\b|"                             # shell eval of a constructed string
    r"\|\s*(ba|z)?sh\b|"                     # pipe into a fresh shell (curl|sh, base64 -d|sh)
    r"\bcd\s+[\"'./]*governance\b")          # cd into the tier-0 governance dir


def violates_tier0(raw_target: str) -> "str | None":
    """Denial reason if a write/patch target path is tier-0, else None. Path-based:
    TIER0 files by basename + anything under a TIER0 directory."""
    if not raw_target:
        return None
    first = raw_target.split('\n')[0].strip()
    if not first:
        return None
    if Path(first).name in TIER0_FILES:
        return f"'{Path(first).name}' is a tier-0/apex/safety file"
    comps = [c.lower() for c in Path(first).parts]
    for d in TIER0_DIRS:
        if d in comps:
            return f"'{first}' is under the tier-0 '{d}/' directory (meta-policy lock)"
    return None


def runcmd_hits_tier0(cmd: str) -> "str | None":
    """Heuristic: deny a run_command that (a) uses a bypass-primitive SHAPE, or (b) pairs a
    mutating op with a tier-0 target. Read-only commands (cat/grep/ls) referencing tier-0
    are allowed — only writes/bypass-shapes deny. BEST-EFFORT (see module docstring)."""
    if not cmd:
        return None
    # (a) bypass primitives — deny the shape regardless of target (closes the realistic
    #     gaps: interpreter one-liners, pipe-to-shell, eval, cd-into-tier0-dir).
    mb = _BYPASS_RX.search(cmd)
    if mb:
        return (f"run_command uses a denied exec primitive ('{mb.group(0).strip()}') — the "
                "autonomous loop may not run interpreter one-liners, pipe into a shell, eval, "
                "or cd into a tier-0 dir. Use 'python -m py_compile FILE' for compile checks.")
    # (b) mutating op + literal tier-0 target
    if not _MUTATING_RX.search(cmd.lower()):
        return None
    for f in TIER0_FILES:
        if f in cmd:
            return f"run_command mutates tier-0 file '{f}'"
    for d in TIER0_DIRS:
        if f"{d}/" in cmd:
            return f"run_command mutates tier-0 dir '{d}/'"
    return None


def plan_violates_tier0(plan) -> "str | None":
    """Scan a coder plan (list of {action, details}) for any tier-0 write/run.
    Returns the first denial reason, or None. For pre-dispatch rejection in the executor."""
    if not isinstance(plan, list):
        return None
    for s in plan:
        if not isinstance(s, dict):
            continue
        action = s.get("action")
        details = s.get("details") or ""
        if action in ("write_file", "patch_file"):
            r = violates_tier0(details.split('\n')[0] if details else "")
            if r:
                return f"plan step '{action}' → {r}"
        elif action == "run_command":
            r = runcmd_hits_tier0(details)
            if r:
                return f"plan step 'run_command' → {r}"
    return None
