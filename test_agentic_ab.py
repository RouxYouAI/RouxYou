"""Agentic-coder A/B harness (Increment 1, lever #4). 2026-06-05.
Calls coder.generate_plan with ROUX_AGENTIC_CODER off vs on, in-process, on the
two fumble cases. Inspects the PLAN: does an EDIT become a patch_file with a
VERBATIM-real anchor (not a full-file overwrite)? does a new @contextmanager file
keep the decorator? Throwaway test file."""
import asyncio, os, json, sys

os.environ.setdefault("ROUX_AGENTIC_CODER", "0")
import coder.coder as C
from coder.coder import PlanRequest
from shared.llm import init_providers

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_LOCK = os.path.join(ROOT, "shared", "state_lock.py")
REAL = open(STATE_LOCK).read()

def anchor_of(step):
    """patch content is 'ANCHOR===PATCH===NEWCODE' — return the anchor part."""
    c = (step.get("content") or "")
    return c.split("===PATCH===")[0].strip() if "===PATCH===" in c else c.strip()

def summarize(plan, label):
    print(f"\n===== {label} =====")
    if not isinstance(plan, dict):
        print(f"  !! non-dict plan: {type(plan).__name__} -> {str(plan)[:200]}")
        return
    steps = C._plan_steps(plan)
    print(f"  steps: {len(steps)}")
    for i, s in enumerate(steps):
        act = C._step_action(s)
        path = C._step_path(s)
        line = f"  [{i}] {act}  {path}"
        if act == "patch_file":
            a = anchor_of(s)
            real = a and a in REAL
            line += f"   anchor_real={real}  anchor={a[:60]!r}"
        elif act == "write_file":
            cont = s.get("content") or ""
            line += f"   content_len={len(cont)}"
            if "state_lock" in (path or ""):
                line += "  <<< OVERWRITE of existing file!"
            if "@contextmanager" in cont:
                line += "  has@contextmanager=True"
            elif "timed" in (path or "").lower() or "context" in (path or "").lower():
                line += "  has@contextmanager=False"
        print(line)
    return steps

async def run(query, context, agentic=False, self_edit=False):
    os.environ["ROUX_AGENTIC_CODER"] = "1" if agentic else "0"
    os.environ["ROUX_SELF_EDIT"] = "1" if self_edit else "0"
    req = PlanRequest(query=query, context=context)
    try:
        return await C.generate_plan(req)
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}

CASE1 = ("In shared/state_lock.py, modify the FileLock class so its __init__ "
         "accepts an optional timeout=None argument and stores it as self._timeout. "
         "Make a surgical edit to the existing file; do not rewrite the whole file.",
         "Existing file shared/state_lock.py defines class FileLock with __init__(self, path).")

CASE2 = ("Create a new file shared/timed_block.py that defines a context manager "
         "`timed_block(label)` using the @contextmanager decorator from contextlib; "
         "it logs the elapsed wall-clock time for the block when it exits.",
         "New file. Use contextlib.contextmanager.")

async def main():
    init_providers()
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which in ("1", "both"):
        print("\n######## CASE 1 — EDIT existing state_lock.py (expect patch_file + REAL anchor) ########")
        off = await run(*CASE1, agentic=False); summarize(off, "CASE1 flag OFF (one-shot)")
        on  = await run(*CASE1, agentic=True);  summarize(on,  "CASE1 flag ON  (agentic)")
    if which in ("2", "both"):
        print("\n######## CASE 2 — NEW @contextmanager file (expect decorator kept) ########")
        off = await run(*CASE2, agentic=False); summarize(off, "CASE2 flag OFF (one-shot)")
        on  = await run(*CASE2, agentic=True);  summarize(on,  "CASE2 flag ON  (agentic)")
    if which == "q3":
        print("\n######## #2 qwen3-coder via llama-server — full pipeline ########")
        for label, case in [("CASE1-edit", CASE1), ("CASE2-decorator", CASE2)]:
            off = await run(*case, agentic=False, self_edit=False)
            summarize(off, f"{label}  flags OFF (pure one-shot)")
            on  = await run(*case, agentic=True,  self_edit=True)
            summarize(on,  f"{label}  flags ON  (#4 agentic + #3 self-edit)")
    if which == "se":
        print("\n######## #3 SELF-EDIT — NEW @contextmanager file (expect decorator AUTO-FIXED when ON) ########")
        off = await run(*CASE2, self_edit=False); summarize(off, "CASE2 ROUX_SELF_EDIT OFF")
        on  = await run(*CASE2, self_edit=True);  summarize(on,  "CASE2 ROUX_SELF_EDIT ON")

if __name__ == "__main__":
    asyncio.run(main())
