# Agentic Coder Refactor — Design (2026-06-05, lever #4)

Foundation for the coder pivot ([[coder_pivot_decision]]). Architecture-first: fix how the pipeline *consumes* code so every modern (agentic-tuned) coder slots in, instead of bolting per-model adapters onto a mismatched one-shot pipeline.

## The core problem (why one-shot fails)
`generate_plan` (coder/coder.py:679) asks the model for a COMPLETE plan in ONE shot — `read_file` + `patch_file`/`write_file` + `verify`, all at once. The prompt *heavily* instructs "read_file BEFORE patch_file, anchor text must be EXACT characters from read_file output" (lines 385/415/488/493) — **BUT the read_file steps are NEVER EXECUTED during planning.** So when the model emits a `patch_file` with anchor text, it is **fabricating the anchor** — it planned to read but never saw the result. Same for `write_file` content. Result: wrong anchors, blind content, and the exact fumbles we hit 2026-06-05 (defaulted to full-file overwrite instead of patch; dropped the `@contextmanager` decorator). **The model is being asked to write code for files it hasn't seen.**

Modern coders (qwen3-coder etc.) are AGENTIC by design — read → observe → edit. Our one-shot pipeline never lets them *observe*, so they emit diffs against *imagined* content and fight us (see [[coder_pivot_decision]] §3).

## Existing partial pieces (build on these, don't replace)
- **`read_file` action** — the worker executes it and returns content (the "observe" primitive already exists).
- **`ANCHOR_UNCERTAIN`** (lines 287-289) — model can flag an unsure anchor → pipeline reads + re-resolves at EXECUTION time. Recovery exists, but PLANNING is still blind.
- **Content-gen pass** (`_generate_file_content` :544, `_generate_patch_content` :584) — SEPARATE post-plan calls that generate content. Becomes mostly redundant once the model plans with real content in hand; keep as fallback.

## The agentic loop (the refactor) — read-then-edit
Turn `generate_plan` from one-shot into a **bounded multi-turn loop** that EXECUTES reads mid-plan and feeds the real contents back:
```
context = proposal + initial_context
observations = {}                # path -> real file content (later: + run_command output)
for turn in range(MAX_TURNS):    # e.g. 4
    plan = model.generate(prompt + context + observations)     # model proposes next steps
    pending_reads = [s for s in plan
                     if step_action(s) == "read_file" and step_path(s) not in observations]
    if pending_reads:
        for r in pending_reads:
            observations[step_path(r)] = read_file(step_path(r))   # ACTUALLY execute the read
        continue                                                   # re-prompt WITH real contents
    return plan      # no unobserved reads left -> edits are grounded in REAL file content
```
The model now writes edits/anchors against the ACTUAL code, not imagination.

## Integration (minimal downstream change)
- Lives inside `generate_plan` (coder.py). In-loop reads use a safe direct read of RouxYou-scoped paths (or the worker's read_file).
- Bounded: `MAX_TURNS` ~4 + a per-run read cap → no runaway loops/cost.
- The FINAL plan flows to the SAME downstream — bigbrain pre-review → worker execute → bigbrain post → runtime-verify → ledger. **No integrity change; the gates still govern everything.**
- Tier-0 + the existing consistency guards (mutation / new-file-patch) still apply to the final plan.

## Why this is the #4→#2 foundation
qwen3-coder NATIVELY does read→observe→edit; this loop feeds it observations so it stops emitting blind diffs → it slots in (lever #2). It ALSO helps qwen2.5 (stops anchor fabrication → fewer overwrite/decorator fumbles). One architectural fix, every coder benefits.

## Build increments
1. **INCREMENT 1 — ✅ BUILT + A/B TESTED 2026-06-05 (see "INCREMENT 1 — A/B TESTED" section below).** Added module-level `_agentic_plan_loop()` + `_resolve_under_roux()` (safe RouxYou-scoped read), and flag-routing in `generate_plan` (~line 833): `ROUX_AGENTIC_CODER=1` → run the read-then-edit loop (execute `read_file` steps mid-plan via real file reads, feed contents back, re-plan, bounded `max_turns=4`/`read_cap=8`); else the unchanged one-shot retry loop. Result flows through the SAME normalizer + bigbrain + runtime-verify. Tested: two dead bugs fixed (path-key + re-plan schema-drift), loop now functional, but found content-gen already grounds patch content → loop's value is structure-decisions only; #3 is the real graduation-mover.
2. INCREMENT 2: feed `run_command`/`verify_fix` outputs back too (full observe loop).
3. INCREMENT 3: on an agentic model (qwen3-coder via llama-server, lever #2) use its native tool-calling turn format.

## ✅ INCREMENT 1 — A/B TESTED 2026-06-05 (qwen2.5-coder, in-process harness `test_agentic_ab.py`)
Two **dead bugs found + fixed**, then the loop proven functional, plus a key architectural finding:
- **BUG A (silent no-op):** `_step_path` only read `details`/`path`/`file`, but qwen2.5-coder names the path `file_path`. So the loop scanned raw steps, found empty paths, and grounded with **0 reads every time** — Increment 1 was a no-op as shipped. FIX: `_step_path` now also reads `file_path`/`filepath`/`target_file` (benign + hardens the one-shot guards too).
- **BUG B (schema drift on re-plan):** after the read, the model re-grounded its anchor to the REAL file (the hallucinated `threading.Lock` vanished ✅) but emitted off-schema keys `step`/`anchor_text`/`patch_content` → the normalizer dropped the plan to **0 steps**. (My first re-prompt's phrase "anchor text" literally seeded the `anchor_text` key.) FIX: re-prompt now pins the canonical `action`/`details`/`content`+`===PATCH===` schema with an example and forbids the drift keys; `_step_action` also tolerates a `step` key. After the fix: ON produces a valid 1-step `patch_file` with a real anchor.
- **🔍 ARCHITECTURAL FINDING (design had it backwards):** the per-step content-gen pass `_generate_patch_content` (coder.py:674) **already opens + reads the real file** and grounds patch CONTENT against it. So the one-shot pipeline already has a localized read-then-edit for patch *content*. Increment 1's marginal value is therefore NOT patch content — it's the planning **structure** decision (patch-vs-overwrite, multi-file context). The two A/B cases didn't sharply exercise that (the edit case steered both arms to patch_file; the new-file case has nothing to read).
- **🔍 CASE 2 reproduced the real blocker:** a new `@contextmanager` file → BOTH arms dropped the `@contextmanager` decorator (loop no-ops on new files — nothing to read). This is a **content-generation** weakness, NOT a grounding one → it's **lever #3 (Self-Edit/verify-fix loop)** territory, or sidestep (class-based CM, as the 7→8 clean event did).
- **VERDICT:** Increment 1 is now correct, safe (default-off flag, clean no-op when nothing to read), and the right foundation for an agentic model (#2) — but it is NOT the graduation-mover by itself. **The highest-leverage coder fix is #3 (generate→compile/run→feed-error→fix)**, which directly targets the content-gen fumbles (decorator drop) that actually block the ledger. Recommend #3 next, not flipping the flag on.

## Risk / rollback
Central component → build **behind `ROUX_AGENTIC_CODER=1`** (default off) to A/B; snapshot first; loop is bounded; bigbrain + runtime-verify unchanged (integrity preserved). Rollback = flag off / revert.
