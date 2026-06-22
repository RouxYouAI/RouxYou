# Self-Edit / Verify-Fix Loop — Design (lever #3, 2026-06-05)

The graduation-mover. [[coder_pivot_decision]] §"#3" + literature (arxiv 2504.15228 "A Self-Improving Coding Agent", 2508.00083 survey). Model-agnostic: helps qwen2.5-coder AND the future qwen3-coder-30B-A3B (#2). Independent of the agentic refactor (#4) — #4 grounds PLANNING; #3 verifies the GENERATED CODE.

## Why (proven by the 2026-06-05 A/B)
The coder's content-gen produces code that is **syntactically valid but functionally wrong** — the canonical case: a `@contextmanager` function generated WITHOUT the decorator (CASE 2, both arms). It `py_compile`s fine; only *using* it as a context manager fails. bigbrain caught it (BLOCK 2/3) — but that costs a full review cycle and a ledger false-block. #3 catches it **before bigbrain**, deterministically, with execution feedback, and feeds the error back so the coder FIXES it instead of being blocked.

## Where it hooks
Inside `coder/coder.py` `generate_plan`, in the per-step content-gen loop (~lines 1330–1359) — AFTER `_generate_file_content` / `_generate_patch_content` fill `ns["content"]`, BEFORE `plan_data["plan"] = normalized_steps` / return. The coder is the thing that produces code, so it self-verifies before handing off. Flag-gated **`ROUX_SELF_EDIT`** (default OFF), A/B like `ROUX_AGENTIC_CODER`. Gates (bigbrain pre/post, runtime-verify, tier-0) UNCHANGED — this only stops feeding them broken code.

## What "verify" means — DJ's call 2026-06-05 = COMPILE + IMPORT + RUN A MODEL-WRITTEN SMOKE TEST
Build in two increments:

### Increment 1 — compile + guarded import (catches syntax / truncation / NameError / bad imports)
For each `write_file`/`patch_file` step, compute the RESULTING file body:
- `write_file`: body = the generated content.
- `patch_file`: apply the anchor patch to a COPY of the real file via `_apply_patch_preview()` — a faithful mirror of the worker's `find_anchor` + `===PATCH===`(insert-after) / `===REPLACE===`(replace) logic (worker/worker.py:292–335). If the anchor doesn't resolve, that's itself a fixable verify failure ("anchor not found — read the file and use a verbatim anchor").
Then verify the body (only `.py`):
1. `ast.parse(body)` / `compile()` — **definitive FAIL** on SyntaxError/IndentationError (catches the truncation class).
2. Guarded subprocess import — write body to a temp file, import it in a SUBPROCESS (cwd=PROJECT_BASE_DIR, sys.path incl. it, timeout ~20s). **FAIL** only on clear code errors surfaced at import (NameError, ImportError of an undefined symbol, etc.). **Inconclusive→PASS** on timeout or heavy side-effect errors (library modules pull real deps/init; we must not block on those). Honest: increment 1 reliably kills the syntax/truncation/name class; semantic-contract bugs wait for increment 2.

### Increment 2 — model-written smoke test, executed (catches the @contextmanager class)
After content-gen, ask the coder to ALSO emit a tiny smoke test exercising the new code's contract (e.g. `with timed_block('x'): pass` / call the new function / construct the class). Run it in the subprocess against the temp file. A bare-generator-as-CM throws `AttributeError: __enter__` → caught → fed back. This is the real "Self-Edit with execution feedback." The model writing its own test is what catches semantic bugs WITHOUT us hardcoding contracts.

## The fix loop
```
for step needing content:
    content = generate_content(step)              # existing
    for attempt in range(MAX_FIX=2):              # bounded
        body, anchor_err = resulting_body(step, content)
        ok, err = verify(body, smoke_test?)       # incr1: compile+import; incr2: + run smoke
        if ok: break
        content = regenerate_content(step, error=anchor_err or err)   # feed the SPECIFIC error back
    step["content"] = content                     # best-effort even if still failing (bigbrain backstops)
```
Bounded (`MAX_FIX≈2`), fail-soft (on exhaustion, pass the last attempt to bigbrain — never worse than today). Every model call respects `CODER_TIMEOUT`; verify itself is cheap (compile) + one bounded subprocess.

## Risk / rollback
Behind `ROUX_SELF_EDIT=1` (default off) for A/B; snapshot first; loop bounded; verify subprocess sandboxed + timed; gates unchanged. Rollback = flag off / revert. The guarded-import "inconclusive→pass" rule means a flaky verify can never BLOCK a good change — worst case it's a no-op and bigbrain decides, exactly as today.

## BUILD STATUS — increments 1+2 BUILT + UNIT-PROVEN 2026-06-05 (no GPU needed)
`coder/coder.py`: `_apply_patch_preview` (worker-faithful anchor apply), `_verify_python_body` (incr1: ast + guarded subprocess import), `_generate_smoke_test` + `_run_smoke_test` (incr2: model-written test exec), `_self_edit_verify_fix` (orchestration; smoke scoped to write_file new modules), wired into `generate_plan` after content-gen, gated `ROUX_SELF_EDIT=1` (default OFF). Compiles. **Unit tests (pure fns, no model):** patch-preview REPLACE/PATCH/anchor-miss ✅; verify catches SyntaxError (truncation) ✅ + NameError (undefined symbol) ✅, passes good ✅; **smoke runner catches the CASE-2 fumble** — bare `def timed_block` (no `@contextmanager`) + `with m.timed_block('x'): pass` → `TypeError: 'generator' object does not support the context manager protocol` → FAIL; decorated → PASS. ✅
**✅ END-TO-END PROVEN 2026-06-05** (GPU-swapped to qwen2.5-coder, `test_agentic_ab.py se`): CASE 2, `ROUX_SELF_EDIT` OFF → write_file `has@contextmanager=False` (fumble reproduced); ON → smoke test caught `TypeError: 'generator' object does not support the context manager protocol` → fed back → coder REGENERATED → **passed after 1 fix → `has@contextmanager=True`.** The exact ledger-blocking fumble is auto-fixed before bigbrain. v5.3 swapped back + verified generating as Roux; coder service restarted so #3 is live (flag default-OFF). NEXT = wire it on for real runs once we trust it across more cases, and #2 (qwen3-coder via llama-server).

## Test plan (mirrors #4)
Harness extends `test_agentic_ab.py`: GPU-swap to coder, run the two fumble cases with `ROUX_SELF_EDIT` off vs on. Success = ON auto-fixes the `@contextmanager` drop (increment 2) and any truncation/syntax (increment 1) BEFORE bigbrain, with the plan still valid. Then swap v5.3 back + verify it generates as Roux.
