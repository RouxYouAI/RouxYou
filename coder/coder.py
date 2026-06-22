import sys
import os
import json
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

# Ensure we can import from shared
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from shared.lifecycle import register_process
from shared.schemas import AgentPlan, Step, TaskContext
from shared.memory import memory
from shared.codebase_index import codebase_index

# Phase 23: Skill library
try:
    from skill_extractor import get_skills_for_task, format_skills_for_prompt
    SKILLS_AVAILABLE = True
except ImportError:
    SKILLS_AVAILABLE = False
from shared.logger import get_logger
from shared.activity import set_thought, set_plan as broadcast_plan
from shared.llm import llm_chat, llm_generate, init_providers, warm_models
from shared.compaction import compact_coder_prompt, estimate_tokens
from shared.cost_tracker import record_usage

# --- CONFIGURATION ---
PORT = 8002
# Display-only label for startup log. The actual model is resolved at LLM
# call time via the "coder" alias in shared/llm.py (which reads config.yaml).
# Keep this in sync with config.yaml's `llm.aliases.coder` value when changed.
MODEL_NAME = "qwen2.5-coder:14b-instruct (via 'coder' alias)"

# Generalized for cross-machine portability. The system prompt template uses a
# __USERNAME__ sentinel that gets substituted with the actual user at prompt-build
# time, so the file is safe to ship publicly without leaking the original author's
# username. Override SEARXNG_URL via environment if you have a SearXNG instance
# somewhere other than localhost.
import getpass as _getpass
USER_NAME = _getpass.getuser()
USER_HOME = os.path.expanduser("~")
PROJECT_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888")

# --- LOGGING SETUP ---
logger = get_logger("coder")

app = FastAPI(title="Coder Agent (Level 5)")

@app.get("/health")
async def health():
    return {"status": "ok"}

class PlanRequest(BaseModel):
    query: str
    context: Optional[str] = None
    history: List[str] = []

def _check_searxng() -> bool:
    """Quick check if SearXNG is reachable. Used to inform Coder.
    Default URL is http://localhost:8888 — override with the SEARXNG_URL env var
    if your SearXNG instance lives elsewhere on your network.
    """
    import urllib.request
    try:
        urllib.request.urlopen(SEARXNG_URL, timeout=2)
        return True
    except Exception:
        return False

def _extract_user_paths(text: str) -> List[str]:
    """Extract absolute Linux/Unix paths from a user task string.

    Catches: /home/user/foo.txt, ~/Desktop/foo.txt, /etc/config.yaml,
    quoted or unquoted, with or without surrounding sentence punctuation.

    Used by the coder's path sanitizer to detect when the LLM has invented
    its own path instead of using one the user explicitly named.

    Note: tilde (`~`) is expanded to /home/<USER_NAME> at extract time so
    downstream comparisons use a single canonical form.
    """
    if not text:
        return []
    import re as _re_paths
    # Absolute Unix path: starts with / followed by path chars (no whitespace/shell metas).
    # Tilde-prefixed: ~ or ~user followed by /, then path chars.
    # First grab tilde paths, then plain absolute paths.
    # 2026-06-01 BUGFIX: the leading `/` must be at a boundary — NOT glued to a
    # preceding path char. Without the lookbehind, a RELATIVE path mention like
    # "shared/config.py" matched its mid-string slash and yielded a bogus absolute
    # "/config.py", which then got force-applied as a "user path" hard-constraint,
    # corrupting the model's CORRECT "/home/user/RouxYou/shared/config.py". Require the
    # slash (or tilde) to be preceded by whitespace, start-of-string, or a delimiter.
    pattern = r'(?<![A-Za-z0-9_./~-])(?:~[A-Za-z0-9_-]*)?/(?:[^\s"\'<>|*?]+)'
    matches = _re_paths.findall(pattern, text)
    home = os.path.expanduser("~")
    cleaned = []
    for m in matches:
        # Strip trailing sentence punctuation that almost certainly isn't part of the path.
        while m and m[-1] in '.,;:!?)]}':
            m = m[:-1]
        # Skip URLs and protocol-relative paths (e.g. "//foo", "http://...")
        if m.startswith("//"):
            continue
        # Skip noise that just happens to start with `/` but isn't a real path
        # (e.g. regex output, code snippets). Require at least one alpha char.
        if not _re_paths.search(r'[A-Za-z]', m):
            continue
        # Expand ~ to absolute home so downstream comparisons normalize.
        if m.startswith("~/"):
            m = home + m[1:]
        elif m == "~":
            m = home
        if m:
            cleaned.append(m)
    return cleaned

# --- implementation-consistency guard (2026-06-01) -----------------------------
# v5.3-as-coder is capable but STOCHASTIC: on an implement task it sometimes emits
# a recon-only plan (read_file steps, zero edits). bigbrain catches that downstream
# and BLOCKs ("zero write/edit steps"), but that costs a full ~70s chain + an API
# call per miss. We pull that exact check INTO the coder's retry loop so a recon-only
# plan is rejected locally and re-prompted with a mutation directive — converting
# "reliable in one shot" (which it isn't) into "reliable in a few cheap local shots".
_MUTATION_ACTIONS = {"patch_file", "write_file", "deploy_patch"}
_IMPL_VERBS = (
    "add", "create", "fix", "implement", "insert", "modif", "change", "update",
    "harden", "guard", "remove", "delete", "refactor", "rename", "replace",
    "write", "build", "wire", "patch", "enable", "disable", "append", "inject",
)

def _task_needs_mutation(query: str) -> bool:
    """Heuristic: does this task ask us to CHANGE code (vs. just read/analyze)?
    Conservative — a false positive only costs a retry; bigbrain is the backstop."""
    if not query:
        return False
    q = query.lower()
    return any(v in q for v in _IMPL_VERBS)

def _step_action(s) -> str:
    """The action of a plan step, tolerant of model key-drift.
    Mirrors the downstream normalizer (line ~784): action OR type. Without this,
    a model that emits {'type': 'write_file'} (e.g. qwen3-coder) reads as having
    no action and the guards misfire. 2026-06-01."""
    if not isinstance(s, dict):
        return ""
    a = s.get("action") or s.get("type") or s.get("step") or ""
    a = a.strip().lower() if isinstance(a, str) else ""
    # qwen3-coder bare-verb map (mirrors ACTION_ALIASES) so the consistency guards see canonical actions
    return {"edit": "patch_file", "modify": "patch_file", "create": "write_file",
            "write": "write_file", "new_file": "write_file", "read": "read_file"}.get(a, a)

def _step_path(s) -> str:
    """The target path of a plan step, tolerant of details/path key-drift."""
    if not isinstance(s, dict):
        return ""
    v = (s.get("details") or s.get("path") or s.get("file")
         or s.get("file_path") or s.get("filepath") or s.get("target_file") or "")
    return v if isinstance(v, str) else ""

def _plan_steps(plan_data: dict) -> list:
    if not isinstance(plan_data, dict):
        return []
    steps = plan_data.get("plan") or plan_data.get("steps") or plan_data.get("actions") or []
    if not isinstance(steps, list):
        return []
    # qwen3-coder schema-map (2026-06-05): it nests as tasks -> {"task": ..., "steps": [...]}.
    # Flatten any task-wrapper (an item carrying its own nested "steps" list) into the real steps.
    flat = []
    for it in steps:
        if isinstance(it, dict) and isinstance(it.get("steps"), list):
            flat.extend(s for s in it["steps"] if isinstance(s, dict))
        else:
            flat.append(it)
    return flat

def _resolve_under_roux(p: str):
    """Resolve a model-named path under the RouxYou base; return a Path or None if out of scope.
    Used by the agentic loop to safely read only RouxYou-scoped files. 2026-06-05."""
    from pathlib import Path as _Path
    if not isinstance(p, str) or not p.strip():
        return None
    p = p.strip()
    cand = _Path(p) if p.startswith("/") else _Path(PROJECT_BASE_DIR) / p
    try:
        rp = cand.resolve()
    except Exception:
        return None
    base = _Path(PROJECT_BASE_DIR).resolve()
    return rp if (rp == base or base in rp.parents) else None


async def _agentic_plan_loop(request, system_instruction, submit_plan_tool, extract_json,
                             max_turns: int = 4, read_cap: int = 8):
    """Lever #4 (agentic refactor): read-then-edit loop. When the model plans read_file steps,
    ACTUALLY execute them and feed the real file contents back, then let it re-plan — so edits
    anchor against REAL code, not guesses (the root cause of the one-shot fumbles). Returns a
    best-effort plan_data dict or None; the result flows through the same normalizer + gates.
    Flag-gated by ROUX_AGENTIC_CODER. 2026-06-05. Design: docs/agentic_coder_refactor_design.md."""
    msgs = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": f"Task: {request.query}\nContext: {request.context or 'None'}"},
    ]
    observations = {}      # resolved-path-str -> content already fed back
    last_plan = None
    for turn in range(max_turns):
        # max_tokens is CAPPED here (2026-06-05): the agentic call produces the PLAN STRUCTURE
        # only — file CONTENT comes from the separate unconstrained content-gen pass. qwen3-coder
        # otherwise RUNS AWAY to the full 8192 (~238s @ 37 t/s) → swap+plan blows CODER_TIMEOUT.
        # A plan of steps fits easily in ~2500; a runaway is now bounded to ~70s.
        resp = await llm_chat("coder", messages=msgs, temperature=0.1, format="json",
                              tools=submit_plan_tool, tool_choice="required",
                              max_tokens=2500, timeout=300)
        if not getattr(resp, "success", False):
            logger.warning(f"🔁 agentic loop turn {turn}: llm fail: {getattr(resp,'error','?')}")
            break
        cand = extract_json(resp.text)
        if not isinstance(cand, dict):
            logger.warning(f"🔁 agentic loop turn {turn}: no parseable plan")
            break
        last_plan = cand
        pending = []
        for s in _plan_steps(cand):
            if _step_action(s) == "read_file":
                rp = _resolve_under_roux(_step_path(s))
                if rp is not None and str(rp) not in observations:
                    pending.append((_step_path(s), rp))
        if not pending or len(observations) >= read_cap:
            logger.info(f"🔁 agentic loop grounded after turn {turn} ({len(observations)} files observed)")
            return cand
        fed = []
        for raw_p, rp in pending[: max(0, read_cap - len(observations))]:
            try:
                txt = rp.read_text(errors="replace")[:12000]
            except Exception as e:
                txt = f"<could not read: {e}>"
            observations[str(rp)] = txt
            fed.append(f"=== Contents of {raw_p} ===\n{txt}")
            logger.info(f"🔁 agentic loop executed read of {raw_p} ({len(txt)} chars)")
        msgs.append({"role": "assistant", "content": json.dumps(cand)[:2000]})
        msgs.append({"role": "user", "content":
            "I executed your read_file steps. Below are the REAL current file contents.\n\n"
            + "\n\n".join(fed) +
            '\n\nNow call submit_plan with the FINAL plan. Every step MUST use exactly these keys: '
            '"action", "details" (the file path), and "content". For an edit use action "patch_file" '
            'and set "content" to text copied VERBATIM from the real file above, then a line '
            '"===PATCH===", then the replacement text. '
            'Example: {"action":"patch_file","details":"shared/x.py",'
            '"content":"    def __init__(self, path):\\n===PATCH===\\n    def __init__(self, path, timeout=None):"}. '
            'Use action "write_file" with the full file in "content" ONLY for brand-new files. '
            'Do NOT output a unified diff. Do NOT use keys named step, anchor_text, patch_content, patch, or file_path.'})
    return last_plan


def _plan_has_mutation(plan_data: dict) -> bool:
    """True if the plan contains at least one write/edit/deploy step."""
    steps = _plan_steps(plan_data)
    if not steps:
        return True  # empty/unparseable — not our concern (other checks handle it)
    return any(_step_action(s) in _MUTATION_ACTIONS for s in steps)

def _plan_patches_nonexistent(plan_data: dict) -> str:
    """Return the path of any patch_file step targeting a file that doesn't exist.

    patch_file needs an anchor in EXISTING content — you can't patch a file that
    isn't there. A new file must be created with write_file. We only flag absolute
    paths so we don't false-trigger on unresolved relative ones (those get fixed in
    the per-step normalizer downstream). 2026-06-01."""
    for s in _plan_steps(plan_data):
        if _step_action(s) == "patch_file":
            det = _step_path(s)
            if det and os.path.isabs(det) and not os.path.exists(det):
                return det
    return ""

def _normalize_path_for_compare(p: str) -> str:
    """Normalize a Unix path for comparison.
    Collapses repeated slashes, expands `~`, strips trailing slash. Case-sensitive
    on Linux (unlike the Windows version).
    """
    if not p:
        return ""
    if p.startswith("~"):
        p = os.path.expanduser(p)
    # Collapse repeated slashes (//foo/bar -> /foo/bar)
    import re as _re_norm
    p = _re_norm.sub(r'/{2,}', '/', p)
    return p.rstrip("/")

def _build_system_prompt(memories: List[Any] = [], query: str = ""): 
    """Builds the system prompt, injecting learned memories, skills, and system architecture."""
    
    # Phase 23: Check web search availability
    searxng_online = _check_searxng()
    
    # Phase 17: Refresh codebase index if files changed
    codebase_index.refresh_if_stale()
    
    # Phase 17: PageIndex-inspired system architecture map
    system_map = codebase_index.get_system_map()
    
    # Phase 17: Format memories with code artifacts when available
    memory_text = ""
    if memories:
        memory_text = "## RELEVANT MEMORIES (LEARNED KNOWLEDGE)\n"
        for m in memories:
            memory_text += f"- Past Task: {m.task_query}\n"
            memory_text += f"  Result: {m.plan_summary[:150]}\n"
            memory_text += f"  Location: {m.working_dir}\n"
            # Phase 17: Include code artifacts so Coder can REUSE working patterns
            if m.code_artifacts:
                memory_text += f"  Working Code Pattern:\n"
                for fname, code in list(m.code_artifacts.items())[:1]:  # Only best artifact
                    if fname.startswith("cmd:"):
                        continue
                    # Keep compact for local LLM - just enough to copy the pattern
                    code_preview = code[:400] if len(code) > 400 else code
                    memory_text += f"    File: {fname}\n```python\n{code_preview}\n```\n"
            memory_text += f"  Utility Score: {getattr(m, 'utility', 'N/A')}\n"
    
    # Phase 23: Retrieve relevant skills for this task
    skill_text = ""
    if SKILLS_AVAILABLE and query:
        try:
            skills = get_skills_for_task(query, limit=3)
            if skills:
                skill_text = format_skills_for_prompt(skills)
                logger.info(f"📚 SKILLS: Injected {len(skills)} skill(s) for task")
        except Exception as e:
            logger.warning(f"⚠️ Skill retrieval error: {e}")
    
    # Phase 35: Context compaction — trim dynamic sections if over budget
    # Build the static prompt first (without dynamic sections) to measure its size
    _static_size_estimate = 3050 * 4  # ~3050 words × 4 chars/word from our analysis
    sections = compact_coder_prompt(
        system_prompt="",  # placeholder — we measure static separately
        memory_text=memory_text,
        skill_text=skill_text,
        system_map=system_map,
        user_query=query or "",
        model_context=32768,
        output_reserve=4096,
    )
    # Apply compacted versions
    memory_text = sections.get("memories", memory_text)
    skill_text = sections.get("skills", skill_text)
    system_map = sections.get("system_map", system_map)

    _prompt = f"""
    ## YOUR IDENTITY
    You are a surgical code editor with 15 years of systems programming experience.
    You specialize in making the SMALLEST possible change to achieve a goal.
    You NEVER rewrite entire files. You always use anchors and delimiters to make precise edits.
    You would never dump a full file into a patch — that is a catastrophic amateur mistake you do not make.
    You always read a file before editing it so you have exact text to anchor against.

    ## HALLUCINATION PREVENTION (ANTI-COMPLIANCE RULES)
    You are optimized to produce confident-sounding output. This is a liability for patching tasks.
    Override this tendency with the following hard rules:

    ANCHOR UNCERTAINTY PROTOCOL:
    - If you have NOT seen the exact line in a read_file result from this session, do NOT guess the anchor.
    - Instead, output the literal token ANCHOR_UNCERTAIN as the anchor text.
    - The pipeline will catch ANCHOR_UNCERTAIN and trigger a read_file before executing the patch.
    - A confident wrong anchor silently corrupts code. ANCHOR_UNCERTAIN is always recoverable.
    - "It probably looks like..." is NOT acceptable. Only use anchors you have literally seen this session.
    - ANCHOR_UNCERTAIN format: {{"action": "patch_file", "details": "filepath", "content": "ANCHOR_UNCERTAIN\n===REPLACE===\nnew code"}}

    WRONG PREMISE RESISTANCE:
    - If the task provides a file path, function name, or code snippet you cannot verify, do NOT silently adopt it.
    - Instead, add an optional key to your response: "unverified_assumptions": ["<the claim you could not verify>"]
    - Any plan that depends on an unverified assumption MUST include a read_file step to confirm it first.
    - If a user premise contradicts what you read from the file, trust the file. Say so in unverified_assumptions.

    UNCERTAINTY IS VALID OUTPUT:
    - If you cannot safely accomplish a task with the context available, return:
      {{"success": false, "error": "Insufficient context — <what is needed>"}}
    - A plan that admits uncertainty is recoverable. A hallucinated plan that executes is a production incident.

    ## DEFAULT WORKING DIRECTORY (CRITICAL)
    The default working directory for ALL tasks is:
      __PROJECT_BASE__

    When a task refers to a file by name only (e.g. "thank_you.txt", "notes.md"),
    ALWAYS assume it lives in that directory UNLESS context (memories or task
    description) explicitly says otherwise.
    NEVER guess /home/__USERNAME__/ as the root. That is almost always wrong — the project
    lives in __PROJECT_BASE__, not directly under home.

    ## USER-PROVIDED PATHS (HARD CONSTRAINT — READ THIS)
    If the user's task contains an absolute path (anything starting with `/` or `~`),
    that path is a HARD CONSTRAINT, not a suggestion. You MUST use it EXACTLY as written:
    - Same directory chain
    - Same filename
    - Same extension

    DO NOT invent a different filename. DO NOT "simplify" the path. DO NOT relocate
    the file to a more sensible directory. The user picked that path on purpose.

    Example:
      User: "create a file at /home/__USERNAME__/Desktop/verifier_test.txt with the word hello"
      CORRECT: write_file at /home/__USERNAME__/Desktop/verifier_test.txt
      WRONG:   write_file at __PROJECT_BASE__/hello
      WRONG:   write_file at /home/__USERNAME__/Desktop/verifier.txt
      WRONG:   write_file at ./verifier_test.txt

    If you cannot use the user's path for some reason (e.g. it does not exist for a
    read operation), return {{"success": false, "error": "Cannot use path X because Y"}}
    instead of substituting a different path. The pipeline has a sanitizer that will
    OVERRIDE your path with the user's path if you ignore this rule, and the override
    is logged loudly. Don't make us correct you.

    ## OPERATING SYSTEM: LINUX (UBUNTU)
    This system runs on Ubuntu Linux. When using run_command:
    - Use `ls` NOT `dir`, `cat` NOT `type`, `cp` NOT `copy`, `rm` NOT `del`
    - Shell is bash (or sh). No PowerShell. No `cmd.exe`.
    - Home directory: /home/__USERNAME__
    - Path separator is `/` (forward slash). NO backslashes. NO drive letters.

    ## PATH FORMAT (LINUX — SIMPLE)
    You output JSON. Linux paths use forward slashes — no escaping needed.
    Example: "/home/__USERNAME__/RouxYou_Public/coder/coder.py" appears in JSON exactly as written.
    Tilde expansion: `~` means /home/__USERNAME__. Use the absolute form (/home/...) in JSON,
    not `~`, since not every consumer expands `~`.

    {memory_text}

    {skill_text}

    ## SYSTEM KNOWLEDGE (YOUR OWN INFRASTRUCTURE)
    Log files are at: __PROJECT_BASE__/logs/
    Available logs: coder.log, worker.log, orchestrator.log, watchtower.log,
                    gateway.log, deployer.log, memory.log, task_queue.log
    To READ a log: {{"action": "run_command", "details": "tail -n 50 __PROJECT_BASE__/logs/coder.log"}}
    To LIST all logs: {{"action": "run_command", "details": "ls -la __PROJECT_BASE__/logs"}}
    The shared logger function signature: read_log(service_name: str) — requires the service name.

    Services and ports:
      - Gateway: 8000 | Orchestrator: 8001 | Coder: 8002 | Worker: 8003 | Watchtower: 8010

    Memory file: __PROJECT_BASE__/memory.json
    Task registry: __PROJECT_BASE__/tasks.json

    ## WEB SEARCH AVAILABILITY
    Web search uses SearXNG. The instance URL is configured via the SEARXNG_URL
    environment variable (default: http://localhost:8888).
    Current status: {"✅ ONLINE — web_search is available" if searxng_online else "❌ OFFLINE — web_search WILL FAIL. Do NOT use web_search in your plan."}
    {"" if searxng_online else "Solve ALL tasks with local tools only: read_file, run_command, patch_file, write_file."}
    NEVER use web_search as a fallback for tasks you don't understand.
    If a task is unclear, return {{"success": false}} with a clear error message.

    ## CREDENTIALS (SECURE)
    Credentials live in `.env` at project root. NEVER read/write the .env file directly.
    In scripts, use: `from dotenv import load_dotenv; load_dotenv(); os.getenv('KEY_NAME')`
    Available keys: PROXMOX_HOST, PROXMOX_USER, PROXMOX_PASSWORD, PROXMOX_PORT, HA_TOKEN
    NEVER print, log, or hardcode credential values. The Worker blocks .env access.

    ## AVAILABLE ACTIONS (8 total)

    1. **read_file** — Read file contents. ALWAYS do this before patch_file.
       Format: {{"action": "read_file", "details": "/full/path/file.py"}}

    2. **write_file** — Create NEW files ONLY. Never use on existing files.
       Format: {{"action": "write_file", "details": "/full/path/new_file.py", "content": "full file contents"}}
       IMPORTANT — for any NON-trivial new file (more than ~30 lines, e.g. a full HTML
       page, a module, a script), DO NOT paste the whole body here. Set "content" to the
       single literal token __GENERATE__ and the system will write the complete file body
       in a dedicated step. Pasting a large body into this JSON truncates and corrupts the
       plan. Only inline "content" for genuinely tiny files (a few lines).

    3. **patch_file** — Surgically edit EXISTING files. This is your primary editing tool.

       PATCH_FILE FORMAT LOCK (MANDATORY — NO EXCEPTIONS)

       The "content" field must have EXACTLY 3 parts:
         PART 1: Anchor text (exact line(s) copied from the file)
         PART 2: Delimiter (===PATCH=== or ===REPLACE===)
         PART 3: New code to insert or substitute

       INSERT example (adds code AFTER the anchor):
         {{"action": "patch_file", "details": "filepath", "content": "app = FastAPI()\n===PATCH===\n\n@app.get(\\"/new\\")\nasync def new():\n    return {{\\"ok\\": True}}"}}

       REPLACE example (swaps anchor with new code):
         {{"action": "patch_file", "details": "filepath", "content": "PORT = 8001\n===REPLACE===\nPORT = 9001"}}

       ENFORCED CONSTRAINTS:
       - Content MUST contain ===PATCH=== or ===REPLACE===
       - Content without a delimiter is REJECTED (error returned)
       - Full file rewrites in content are REJECTED (error returned)
       - Anchor text must be EXACT characters from read_file output
       - If you have not read the file yet, read it first
       - If anchor text is uncertain, use ANCHOR_UNCERTAIN as the anchor — do NOT guess

    4. **run_command** — Execute shell commands (bash).
       Format: {{"action": "run_command", "details": "python3 script.py"}}

    5. **verify_fix** — Run and verify code works. Must be LAST step.
       Format: {{"action": "verify_fix", "details": "script.py"}}

    6. **web_search** — Search the internet (requires SearXNG).
       Format: {{"action": "web_search", "details": "search query"}}

    6b. **rag_query** — Search DJ + Claude's shared knowledge base (semantic search).
        Use this to find context about past projects, conversations, and decisions.
        Much better than web_search for questions about THIS system or DJ's setup.
        Format: {{"action": "rag_query", "details": "what you want to know about"}}

    7. **restart_service** — Restart worker/coder/orchestrator.
       Format: {{"action": "restart_service", "details": "worker"}}

    8. **deploy_patch** — Deploy changes to a long-running SERVICE file via blue-green.
       ⚠️ ONLY for this exact filename set: worker.py, orchestrator.py, coder.py,
       gateway.py, dashboard.py, companion.py, memory_agent.py, deployer.py, schemas.py,
       logger.py, activity.py, task_registry.py, codebase_index.py,
       infrastructure_monitor.py, registry.py. NOTHING else — all other files (including
       everything under shared/ and services/) use patch_file or write_file, never deploy_patch.
       The change will be staged, health-checked, and await human approval before going live.

       Format is IDENTICAL to patch_file but with action="deploy_patch" and details="service_name":
       INSERT (adds code AFTER anchor):
         {{"action": "deploy_patch", "details": "worker", "content": "app = FastAPI()\n===PATCH===\n\n@app.get(\\\"/phase\\\")\nasync def phase():\n    return {{\\\"phase\\\": \\\"20.3\\\"}}"}}
       REPLACE (swap anchor with new code):
         {{"action": "deploy_patch", "details": "worker", "content": "PORT = 8001\n===REPLACE===\nPORT = 9001"}}

       Service names: worker, orchestrator, coder
       IMPORTANT: You must read_file FIRST to get exact anchor text, then use deploy_patch.

    ## OUTPUT FORMAT LOCK
    You return ONLY valid JSON. No markdown. No explanation. No commentary.
    Respond with these EXACT keys:
    {{
      "success": true,
      "initial_context": {{
        "working_dir": "/path/to/project",
        "active_file": "target.py",
        "detected_errors": []
      }},
      "plan": [
        {{"id": 1, "action": "read_file", "details": "/path/file.py"}},
        {{"id": 2, "action": "patch_file", "details": "/path/file.py", "content": "exact anchor line\n===PATCH===\nnew code"}},
        {{"id": 3, "action": "verify_fix", "details": "file.py"}}
      ],
      "unverified_assumptions": []  // optional — list any claims from the task you could not verify
    }}

    ## ⚠️ IMPLEMENTATION IS MANDATORY — READ-ONLY PLANS ARE REJECTED
    If the task asks you to ADD, CHANGE, FIX, CREATE, HARDEN, GUARD, REMOVE, or
    otherwise MODIFY code, your plan MUST contain at least one MUTATION step that
    actually performs the change: patch_file, write_file, or deploy_patch.
    A plan made ONLY of read_file / rag_query / web_search / verify_fix steps does
    NOT implement the task and WILL BE REJECTED by review. read_file is PREPARATION,
    not implementation — every read_file you emit MUST be followed by the edit it was
    preparing for. If the task names a concrete change like "add X to function Y in
    file Z", your plan MUST include the patch_file/deploy_patch step that writes X
    into Z. Do not stop at reconnaissance. Implement.
    PATHS: When the task names a file like `shared/config.py` or `services/x.py`, the
    path in EVERY step MUST be the FULL absolute path under the project base
    (e.g. `__PROJECT_BASE__/shared/config.py`) — NEVER a bare `/config.py` at the
    filesystem root. This applies to NEW files you are creating too: a new file goes
    at `__PROJECT_BASE__/<dir>/<name>.py`, never `/<name>.py`.

    ## PLANNING RULES
    1. EVERY edit workflow: read_file first, then edit, then verify_fix. No exceptions.
    2. EVERY plan ends with verify_fix as the final step.
    3. Use patch_file for NORMAL files. Use write_file ONLY for brand new files.
    4. All paths must be absolute (start with `/`).
    5. If memories contain Working Code Patterns, adapt that code instead of reinventing.
    6. Before patching, confirm: do I have the exact anchor text from a read_file? If no, read first.

    ## SCOPE & EFFICIENCY RULES (CRITICAL)
    7. NEVER run analysis tools (flake8, pylint, grep) on the ENTIRE project directory.
       Always target SPECIFIC files or at most a single subdirectory.
       BAD:  run_command "flake8 __PROJECT_BASE__"
       GOOD: run_command "flake8 __PROJECT_BASE__/orchestrator/orchestrator.py"
    8. Keep plans to 6 steps or fewer. If a task needs more, focus on the 3-4 MOST
       IMPORTANT changes and note what was deferred.
    9. run_command has a 60-second timeout. If a command could exceed this, scope it down.
       Scan individual files, not entire directories.
    10. When asked to scan/check/audit the codebase, pick the 2-3 MOST IMPORTANT files
        from the System Architecture map above. Do NOT try to process everything at once.
    11. For broad tasks like "find all deprecation warnings", read the 2-3 core service files
        (orchestrator.py, worker.py, coder.py) and report findings per file.
    12. UNCERTAINTY OVER CONFIDENCE: If you are unsure of anchor text, file structure, or function
        signatures, your plan MUST include a read_file step to verify BEFORE patching. Never fabricate
        anchor text that "probably" exists. A wrong anchor is always worse than an explicit ANCHOR_UNCERTAIN.
        When in doubt: read first, patch second. Always.

    ## ⚠️ CRITICAL: deploy_patch IS FOR AN EXACT LIST OF FILES — NOTHING ELSE
    deploy_patch is ONLY for these EXACT files (matched by filename):
      worker.py, orchestrator.py, coder.py, gateway.py, dashboard.py, companion.py,
      memory_agent.py, deployer.py, schemas.py, logger.py, activity.py,
      task_registry.py, codebase_index.py, infrastructure_monitor.py, registry.py
    These are long-running services; editing them needs the blue-green pipeline, so
    deploy_patch with details="<service>" (worker | orchestrator | coder | gateway).

    EVERY OTHER FILE uses patch_file or write_file — NOT deploy_patch. This includes
    ALL files under shared/ and services/ (e.g. shared/researcher.py, shared/config.py,
    services/web_researcher.py). They are plain library modules, NOT system services.
    - File EXISTS → patch_file (read_file first to get the exact anchor).
    - File is NEW (does not exist yet) → write_file with the COMPLETE file contents.
    Do NOT call a file a "system file" or use deploy_patch unless its filename is in
    the exact list above. shared/config.py and shared/researcher.py are NOT in the list.

    ## CONTEXT EXTRACTION
    - If a relevant memory exists, use its working_dir.
    - Subfolder references like "m_t/trap.py" expand to: __PROJECT_BASE__/m_t/trap.py
    - NEVER use partial paths like "/m_t" — always use the full absolute path.
    - Always populate initial_context with working_dir, active_file, and detected_errors.

    ## SYSTEM ARCHITECTURE
    {system_map}
    """
    # Substitute the actual user and project base directory of the running machine
    # in place of the placeholders used throughout the prompt template above.
    # This keeps the template readable while making the file safe to ship publicly.
    return _prompt.replace("__USERNAME__", USER_NAME).replace("__PROJECT_BASE__", PROJECT_BASE_DIR)


async def _generate_file_content(path: str, task: str, draft: str = "") -> str:
    """Generate a COMPLETE file body via a raw, UNCONSTRAINED coder call.

    The JSON plan mode (format="json") truncates large file bodies — qwen2.5-coder
    stochastically stops mid-file when emitting a big string inside constrained JSON
    (snake-game probe, 2026-05-25). This generates the body with NO format=json so it
    runs to completion. Returns `draft` unchanged on any failure (fail-soft).
    """
    import re as _re
    prompt = (
        f"Write the COMPLETE, working contents of the file `{path}`.\n\n"
        f"TASK / SPECIFICATION:\n{task}\n\n"
        f"Include EVERY import the code uses (e.g. `import os` if you call os.getenv). "
        f"Output ONLY the raw file contents — no markdown code fences, no commentary, "
        f"no JSON. Begin immediately with the first character of the file and write the "
        f"ENTIRE file through to its final line. Do NOT stop early or truncate."
    )
    try:
        resp = await llm_chat(
            "coder",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=8192,
            timeout=300,
        )  # deliberately NO format="json"
    except Exception as e:
        logger.warning(f"content-gen call failed for {path}: {e}")
        return draft
    if not getattr(resp, "success", False) or not getattr(resp, "text", ""):
        logger.warning(f"content-gen returned nothing for {path}; keeping draft")
        return draft
    content = resp.text.strip()
    # Strip a wrapping markdown fence if the model added one despite instructions.
    if content.startswith("```"):
        content = _re.sub(r"^```[A-Za-z0-9_+-]*\n", "", content, count=1)
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3].rstrip()
    return content or draft


def _def_block(existing: str, name: str):
    """Return the VERBATIM text of the def/class `name` block (signature line through the end
    of its indented body, trailing blanks trimmed), or None. Used to replace a whole function
    by name when an agentic coder emits a redefinition but a junk anchor. 2026-06-05 (lever #2)."""
    import re as _re
    lines = existing.splitlines()
    pat = _re.compile(r'^(\s*)(?:async\s+def|def|class)\s+' + _re.escape(name) + r'\b')
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if not m:
            continue
        indent = len(m.group(1))
        last = i
        j = i + 1
        while j < len(lines):
            s = lines[j]
            if s.strip() == "":
                j += 1; continue            # blank lines inside the body don't end it
            if (len(s) - len(s.lstrip())) <= indent:
                break                        # dedent to <= signature → block ended
            last = j; j += 1
        return "\n".join(lines[i:last + 1])
    return None


def _function_replace(existing: str, newcode: str):
    """If `newcode` redefines a def/class that EXISTS in the file, return a normalized
    'old_block===REPLACE===newcode' patch. None otherwise. The robust fallback for agentic
    coders that emit a full-function replacement with a bad/whole-file anchor. 2026-06-05."""
    import re as _re
    m = _re.search(r'(?:async\s+def|def|class)\s+(\w+)', newcode or "")
    if not m:
        return None
    blk = _def_block(existing, m.group(1))
    if not blk:
        return None
    return blk + "\n===REPLACE===\n" + (newcode or "").strip("\n")


def _normalize_patch_output(out: str, existing: str):
    """Turn a coder's raw edit output into a worker-applicable 'anchor + ===PATCH===/===REPLACE===
    + code' string, or None. Handles: (1) SEARCH/REPLACE blocks (agentic-coder native);
    (2) our ===PATCH===/===REPLACE=== with a SMALL verbatim anchor (qwen2.5 happy path, preserved);
    (3) function-replacement-by-symbol when the anchor is junk/whole-file (qwen3-coder). 2026-06-05."""
    import re as _re
    # (1) SEARCH/REPLACE block
    m = _re.search(r'<{3,}\s*SEARCH\s*\n(.*?)\n={3,}\s*\n(.*?)\n>{3,}\s*REPLACE', out, _re.DOTALL)
    if m:
        search, replace = m.group(1).strip("\n"), m.group(2).strip("\n")
        if search.strip() and search.strip() in existing and len(search.splitlines()) <= 12:
            return search.strip("\n") + "\n===REPLACE===\n" + replace
        return _function_replace(existing, replace)  # search wasn't verbatim → replace by symbol
    # (2)/(3) our delimiter format — split on ALL delimiters so a stray 2nd delim can't misparse
    if "===PATCH===" not in out and "===REPLACE===" not in out:
        return _function_replace(existing, out)  # no delimiter → maybe it's just the new code
    segs = _re.split(r'={2,}\s*(?:PATCH|REPLACE)\s*={2,}', out)
    before = segs[0].strip("\n")
    newcode = next((s.strip("\n") for s in segs[1:] if s.strip()), "")
    # (2) small verbatim anchor → preserve the model's insert(PATCH) vs replace(REPLACE) intent
    if before.strip() and before.strip() in existing and len(before.splitlines()) <= 8:
        rest = out[out.find(before) + len(before):]
        mode = "===REPLACE===" if _re.match(r'\s*={2,}\s*REPLACE', rest) else "===PATCH==="
        return before + "\n" + mode + "\n" + newcode
    # (3) anchor is junk/whole-file → replace the function the newcode redefines
    fb = _function_replace(existing, newcode)
    if fb:
        return fb
    # last resort: repair a def/class signature anchor from `before`'s first line
    first = before.splitlines()[0].strip() if before.splitlines() else ""
    m2 = _re.match(r'(?:async\s+def|def|class)\s+(\w+)', first)
    if m2:
        blk = _def_block(existing, m2.group(1))
        if blk:
            return blk + "\n===REPLACE===\n" + newcode
    return None


async def _generate_patch_content(path: str, task: str) -> str:
    """Generate a patch (anchor + ===PATCH===/===REPLACE=== + new code) for an
    EXISTING file via a raw, UNCONSTRAINED coder call.

    2026-06-02: modern coder models (qwen2.5-coder, qwen3-coder) are AGENTIC — they
    plan read→edit steps but leave the patch `content` EMPTY in one-shot JSON because
    they expect to read the file first, THEN emit the edit. We do that read here and
    ask for the concrete patch, closing the gap deterministically. Returns "" on any
    failure (caller leaves the step empty for ANCHOR_UNCERTAIN / review to catch).
    """
    import re as _re
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            existing = f.read()
    except Exception as e:
        logger.warning(f"patch content-gen: can't read {path}: {e}")
        return ""
    # Cap the file context so the prompt stays sane on large files.
    snippet = existing if len(existing) <= 12000 else existing[:12000] + "\n# ...(truncated)..."
    prompt = (
        f"You are editing the EXISTING file `{path}`.\n\n"
        f"=== CURRENT FILE CONTENTS ===\n{snippet}\n=== END FILE ===\n\n"
        f"TASK: {task}\n\n"
        "Produce ONE minimal edit as a SEARCH/REPLACE block, in EXACTLY this format and nothing else:\n"
        "<<<<<<< SEARCH\n"
        "(the EXACT existing lines to change — copied VERBATIM from the file, MINIMAL: only the\n"
        " function or lines you are changing, NOT the whole file)\n"
        "=======\n"
        "(the new lines that replace them)\n"
        ">>>>>>> REPLACE\n\n"
        "Example of the SHAPE only (use the real file's lines + real code, NOT this):\n"
        "<<<<<<< SEARCH\n"
        "    def __init__(self, path):\n"
        "        self._x = None\n"
        "=======\n"
        "    def __init__(self, path, timeout=None):\n"
        "        self._x = None\n"
        "        self._timeout = timeout\n"
        ">>>>>>> REPLACE\n\n"
        "Rules:\n"
        "- SEARCH must be lines that appear VERBATIM in the file above. Keep it MINIMAL — include\n"
        "  only the function/lines you change. Do NOT paste the whole file as SEARCH.\n"
        "- When changing a function, put the WHOLE current function in SEARCH and the whole new\n"
        "  function in REPLACE.\n"
        "- Include any imports the new code needs.\n"
        "- If the task names a symbol not exactly present, map to the closest real one and edit THAT.\n"
        "- Output ONLY the SEARCH/REPLACE block. No JSON, no markdown fences, no commentary.\n"
        "- Output NO_CHANGE only if this file is genuinely unrelated to the task."
    )
    try:
        resp = await llm_chat(
            "coder",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=2048,
            timeout=180,
        )  # deliberately NO format="json"
    except Exception as e:
        logger.warning(f"patch content-gen call failed for {path}: {e}")
        return ""
    if not getattr(resp, "success", False) or not getattr(resp, "text", ""):
        return ""
    out = resp.text.strip()
    # Strip ALL markdown fence lines (the model sprinkles ``` around the anchor/code,
    # not just at the start) — a fence inside the content corrupts the anchor match.
    out = "\n".join(l for l in out.splitlines() if not l.strip().startswith("```")).strip()
    if os.environ.get("ROUX_CODER_DEBUG") == "1":
        logger.info(f"🔬 patch content-gen RAW for {path} ({len(out)} chars):\n{out[:800]}")
    if out == "NO_CHANGE":
        logger.info(f"patch content-gen: model says NO_CHANGE for {path}")
        return ""
    # Robust normalize: SEARCH/REPLACE blocks, multi-delimiter junk, whole-file anchors, and
    # function-replacement-by-symbol (the agentic-coder edit shapes). 2026-06-05 (lever #2).
    norm = _normalize_patch_output(out, existing)
    if not norm:
        logger.warning(f"patch content-gen: could not normalize output for {path}; discarding")
        return ""
    if os.environ.get("ROUX_CODER_DEBUG") == "1":
        logger.info(f"🔬 patch content-gen NORMALIZED for {path}:\n{norm[:600]}")
    return norm


# ── Lever #3: Self-Edit / verify-fix loop (2026-06-05, flag ROUX_SELF_EDIT, default OFF) ──
# Verify the coder's GENERATED CODE before it reaches bigbrain; on failure, feed the error
# back and regenerate (bounded). Design: docs/self_edit_fix_loop_design.md.
def _apply_patch_preview(original: str, patch_content: str):
    """Return (file body AFTER applying patch_content, "") or (None, reason). Faithful
    mirror of the worker's anchor-apply (worker/worker.py:292-335) so verify sees what the
    worker WILL produce. reason in {anchor_uncertain,no_delimiter} = 'not our concern, let
    the worker/ANCHOR_UNCERTAIN path handle it'; any other reason is a fixable error."""
    import re as _re
    pc = patch_content or ""
    if pc.startswith("ANCHOR_UNCERTAIN"):
        return None, "anchor_uncertain"
    pc = _re.sub(r'={2,}\s*REPLACE\s*={2,}', '===REPLACE===', pc, flags=_re.IGNORECASE)
    pc = _re.sub(r'={2,}\s*PATCH\s*={2,}', '===PATCH===', pc, flags=_re.IGNORECASE)

    def find_anchor(anchor, source):
        if anchor in source: return anchor
        stripped = anchor.strip()
        if stripped and stripped in source: return stripped
        collapsed = ' '.join(anchor.split())
        for line in source.splitlines():
            if ' '.join(line.split()) == collapsed: return line
        anchor_lines = [l for l in anchor.strip().splitlines() if l.strip()]
        if len(anchor_lines) >= 2:
            last = anchor_lines[-1].strip()
            if last and last in source and source.count(last) == 1: return last
        return None

    if "===REPLACE===" in pc:
        find_text, new_text = pc.split("===REPLACE===", 1)
        actual = find_anchor(find_text.strip(), original)
        if actual is None:
            return None, "anchor text not found in the file — copy a VERBATIM line from the file as the anchor"
        if original.count(actual) > 1:
            return None, f"anchor appears {original.count(actual)} times — use a longer, unique anchor"
        return original.replace(actual, new_text.strip(), 1), ""
    if "===PATCH===" in pc:
        find_line, new_code = pc.split("===PATCH===", 1)
        actual = find_anchor(find_line.strip(), original)
        if actual is None:
            return None, "anchor line not found in the file — copy a VERBATIM line from the file as the anchor"
        return original.replace(actual, actual + "\n" + new_code.rstrip(), 1), ""
    return None, "no_delimiter"


def _verify_python_body(body: str):
    """Increment 1 verify of a candidate .py body. Returns (ok, error).
    (1) ast.parse — DEFINITIVE fail on SyntaxError/IndentationError (the truncation class).
    (2) guarded subprocess import — FAIL only on NameError at module load (undefined-symbol
    class). ImportError / any other exception / timeout = INCONCLUSIVE → pass, so a flaky or
    heavy-side-effect import can NEVER block a good change (bigbrain still decides). 2026-06-05."""
    import ast, tempfile, subprocess, os as _os
    if not body or not body.strip():
        return True, ""
    try:
        ast.parse(body)
    except SyntaxError as e:
        return False, f"SyntaxError: {e.msg} (line {e.lineno})"
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".py", prefix="rx_verify_")
        with _os.fdopen(fd, "w") as f:
            f.write(body)
        probe = (
            "import importlib.util,sys\n"
            f"s=importlib.util.spec_from_file_location('rx_verify',{tmp!r})\n"
            "m=importlib.util.module_from_spec(s)\n"
            "try:\n"
            "    s.loader.exec_module(m)\n"
            "except NameError as e:\n"
            "    print('FAIL:NameError: '+str(e)); sys.exit(7)\n"
            "except BaseException:\n"
            "    sys.exit(0)\n"      # inconclusive — heavy imports / side effects don't block
        )
        r = subprocess.run([sys.executable, "-c", probe], cwd=PROJECT_BASE_DIR,
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 7:
            line = next((l for l in r.stdout.splitlines() if l.startswith("FAIL:")), "FAIL: NameError")
            return False, line[len("FAIL:"):].strip()
        return True, ""
    except Exception:
        return True, ""
    finally:
        if tmp:
            try: _os.unlink(tmp)
            except Exception: pass


async def _generate_smoke_test(det: str, task: str) -> str:
    """Increment 2: ask the coder for a MINIMAL smoke test that EXERCISES the new code's
    contract. The candidate module is exposed to the test as the variable `m` (no import),
    so a `with m.thing(): ...` on a CM-missing-@contextmanager throws → caught → fed back.
    Returns raw Python statements, or "" on failure (caller skips smoke). 2026-06-05."""
    prompt = (
        f"Write a MINIMAL smoke test for new code that will live in `{det}`.\n\n"
        f"TASK the code implements:\n{task}\n\n"
        "The new module is ALREADY LOADED and available as the variable `m` — do NOT import "
        "it. Access members as m.<name> (m.my_func, m.MyClass).\n"
        "Write 1–5 lines of Python that EXERCISE the main contract and would raise if it is "
        "wrong:\n"
        "- a context manager → use it: `with m.thing('x'):\\n    pass`\n"
        "- a function → call it with sample args (assert the result ONLY if the task states it)\n"
        "- a class → construct it and call its key method\n"
        "RULES (avoid false failures): keep it MINIMAL — just exercise, do not over-assert. "
        "Do NOT access attributes on a `with ... as v:` value unless the task EXPLICITLY says the "
        "context manager yields an object (many yield None). Do NOT assume return types/attributes "
        "the task didn't promise. Use only sample literals for args.\n"
        "Output ONLY the raw Python statements — no imports of the module, no markdown fences, "
        "no commentary, no function wrapper."
    )
    try:
        resp = await llm_chat("coder", messages=[{"role": "user", "content": prompt}],
                              temperature=0.1, max_tokens=512, timeout=120)
    except Exception as e:
        logger.warning(f"smoke-test gen failed for {det}: {e}")
        return ""
    if not getattr(resp, "success", False) or not getattr(resp, "text", ""):
        return ""
    out = "\n".join(l for l in resp.text.strip().splitlines() if not l.strip().startswith("```")).strip()
    return out


def _run_smoke_test(body: str, test_code: str):
    """Load `body` as module `m` in a subprocess, exec `test_code` against it. Returns
    (ok, error). Loading failures (heavy imports/side effects) = INCONCLUSIVE→pass; only a
    failure raised by the TEST itself blocks (e.g. CM used without @contextmanager). 2026-06-05."""
    import tempfile, subprocess, os as _os, json as _json
    if not test_code.strip():
        return True, ""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".py", prefix="rx_cand_")
        with _os.fdopen(fd, "w") as f:
            f.write(body)
        probe = (
            "import importlib.util,sys,traceback\n"
            f"s=importlib.util.spec_from_file_location('rx_cand',{tmp!r})\n"
            "m=importlib.util.module_from_spec(s)\n"
            "try:\n"
            "    s.loader.exec_module(m)\n"
            "except BaseException:\n"
            "    sys.exit(0)\n"      # can't even load → inconclusive, don't block on smoke
            "try:\n"
            + "".join("    " + ln + "\n" for ln in (test_code.splitlines() or ["pass"])) +
            "except BaseException as e:\n"
            "    print('SMOKE_FAIL:'+type(e).__name__+': '+str(e)); sys.exit(7)\n"
        )
        r = subprocess.run([sys.executable, "-c", probe], cwd=PROJECT_BASE_DIR,
                           capture_output=True, text=True, timeout=25)
        if r.returncode == 7:
            line = next((l for l in r.stdout.splitlines() if l.startswith("SMOKE_FAIL:")), "SMOKE_FAIL: error")
            return False, "smoke test failed — " + line[len("SMOKE_FAIL:"):].strip()
        return True, ""
    except Exception:
        return True, ""
    finally:
        if tmp:
            try: _os.unlink(tmp)
            except Exception: pass


async def _self_edit_verify_fix(ns: dict, request, max_fix: int = 2):
    """For one write_file/patch_file step: compute the resulting .py body, verify it, and on
    failure regenerate the content with the SPECIFIC error fed back (bounded). Mutates
    ns['content'] to the best attempt; fail-soft (bigbrain backstops). 2026-06-05."""
    act = ns.get("action"); det = ns.get("details")
    if act not in ("write_file", "patch_file") or not det or not str(det).endswith(".py"):
        return
    content = ns.get("content") or ""
    # Increment 2: a smoke test (model-written) only for NEW modules (write_file) — they hold
    # the self-contained, contract-bearing code (the @contextmanager class). Generated once.
    smoke = ""
    if act == "write_file":
        smoke = await _generate_smoke_test(det, request.query)
    prev_smoke_err = None  # smoke-robustness: detect a bad model-written test (repeats unchanged)
    for attempt in range(max_fix + 1):
        body = None; err = ""; failtype = ""
        if act == "write_file":
            body = content
        else:
            try:
                original = open(det, "r", encoding="utf-8", errors="replace").read()
            except Exception:
                return  # can't read target → leave it to the worker / bigbrain
            body, reason = _apply_patch_preview(original, content)
            if body is None:
                if reason in ("anchor_uncertain", "no_delimiter"):
                    return  # not our concern
                err, failtype = reason, "patch"  # real, fixable anchor failure
        if body is not None:
            ok, verr = _verify_python_body(body)            # incr1: compile + import (ALWAYS real)
            if not ok:
                err, failtype = verr, "code"
            elif smoke:                                      # incr2: run the model-written smoke test
                sok, serr = _run_smoke_test(body, smoke)
                if not sok:
                    # The body PASSED compile+import. If the smoke error is IDENTICAL to the prior
                    # attempt's (a regen didn't change it), the model-written TEST is the bug, not the
                    # code → drop the smoke and accept (bigbrain still backstops). Stops wasted regens.
                    if serr == prev_smoke_err:
                        logger.warning(f"🩺 self-edit: {det} smoke error unchanged after regen "
                                       f"({serr[:70]}) — suspect a BAD TEST not bad code; accepting as-is")
                        ns["content"] = content
                        return
                    prev_smoke_err = serr
                    ok, err, failtype = False, serr, "smoke"
            if ok:
                if attempt > 0:
                    logger.info(f"🩺 self-edit: {det} passed verify after {attempt} fix(es)")
                ns["content"] = content
                return
        if attempt >= max_fix:
            logger.warning(f"🩺 self-edit: {det} still failing after {max_fix} fix(es) "
                           f"[{failtype}] ({(err or '')[:90]}); passing to bigbrain as-is")
            ns["content"] = content
            return
        logger.info(f"🩺 self-edit: {det} verify failed [{failtype}] ({(err or '')[:90]}); regenerating (fix {attempt+1})")
        task = (f"{request.query}\n\nA previous attempt to edit `{det}` FAILED verification with:\n"
                f"{err}\nProduce a corrected version that fixes exactly that error.")
        fresh = (await _generate_file_content(det, task, content)) if act == "write_file" \
            else (await _generate_patch_content(det, task))
        if fresh:
            content = fresh
    ns["content"] = content


@app.post("/plan")
async def generate_plan(request: PlanRequest):
    """Entry point — wrap the planner in the GPU arbiter (flag-gated ROUX_GPU_ARBITER) so the
    coder backend owns the 16GB card during planning and v5.3 is restored after. Default OFF =
    no-op (manual swap flow unchanged). 2026-06-05 (lever #2)."""
    from shared.gpu_arbiter import coder_gpu_session
    async with coder_gpu_session():
        return await _plan_impl(request)


async def _plan_impl(request: PlanRequest):
    logger.info(f"Planning task: {request.query}")
    set_thought(f"Planning: {request.query[:60]}...")
    
    try:
        # 1. Retrieve Memories (The Hippocampus Lookup)
        set_thought("Searching episodic memory for relevant experiences...")
        relevant_memories = memory.retrieve_relevant(request.query)
        if relevant_memories:
            logger.info(f"Found {len(relevant_memories)} relevant memories.")
            set_thought(f"Found {len(relevant_memories)} relevant memories to guide planning.")
        
        # 2. Build Prompt (Phase 23: now includes skill retrieval)
        system_instruction = _build_system_prompt(relevant_memories, query=request.query)
        
        # 3. Call LLM (GPT-OSS 20B) — Phase 34: via shared/llm.py provider abstraction
        set_thought("Consulting local LLM for task planning...")
        # 2026-06-01: force structured output via tool-use (tool_choice=required) — the
        # reliable path on the v5.3 backbone's llama-server (response_format is only soft-
        # honored there). The model must emit a schema-valid submit_plan call; the provider
        # surfaces its arguments as the response text, so the JSON parse below is unchanged.
        _SUBMIT_PLAN_TOOL = [{
            "type": "function",
            "function": {
                "name": "submit_plan",
                "description": "Submit the ordered edit plan as structured JSON.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plan": {
                            "type": "array",
                            "description": "Ordered steps; each is an action plus the fields it needs (path, content, command, etc.) per the system prompt's plan format.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action": {"type": "string"},
                                    "path": {"type": "string"},
                                    "content": {"type": "string"},
                                    "details": {"type": "string"},
                                },
                                "required": ["action"],
                            },
                        },
                        "initial_context": {"type": "object"},
                    },
                    "required": ["plan"],
                },
            },
        }]
        # --- robust plan acquisition (2026-06-01) --------------------------------------
        # tool_choice is SOFT on this Vulkan llama-server build: clean inputs → valid
        # tool_call JSON, but messy ones can still slip into prose. So: a tolerant parser
        # (strip think-tags/fences, pull the first balanced {...}) PLUS retry-with-
        # escalation (a hard "ONLY the tool call" nudge + temp variance to break reasoning).
        def _extract_plan_json(text):
            if not text:
                return None
            import re as _re
            t = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).replace("</think>", "").strip()
            mfence = _re.search(r"```(?:json)?\s*(.*?)```", t, flags=_re.DOTALL)
            if mfence:
                t = mfence.group(1).strip()
            try:
                return json.loads(t)
            except Exception:
                pass
            start = t.find("{")
            if start >= 0:
                depth = 0
                for i in range(start, len(t)):
                    if t[i] == "{":
                        depth += 1
                    elif t[i] == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(t[start:i + 1])
                            except Exception:
                                break
            return None

        plan_data = None
        content = ""
        _needs_mut = _task_needs_mutation(request.query)
        _fail_reason = None  # "parse" | "no_mutation"
        # Lever #4 (agentic refactor): read-then-edit loop, flag-gated A/B vs the one-shot loop below.
        _AGENTIC = os.environ.get("ROUX_AGENTIC_CODER", "0") == "1"
        if _AGENTIC:
            logger.info("🔁 ROUX_AGENTIC_CODER on — read-then-edit planning")
            plan_data = await _agentic_plan_loop(request, system_instruction, _SUBMIT_PLAN_TOOL, _extract_plan_json)
        for _attempt in range(4):  # +1 headroom for a mutation-directive retry
            if _AGENTIC:
                break  # agentic loop already produced plan_data; skip the one-shot retries
            _msgs = [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": f"/no_think\nTask: {request.query}\nContext: {request.context or 'None'}"},
            ]
            if _attempt > 0:
                if _fail_reason == "no_mutation":
                    _msgs.append({"role": "user", "content":
                        "Your previous plan only INSPECTED files (read_file) and did NOT implement the task. "
                        "This task REQUIRES code changes. Your plan MUST include at least one mutation step that "
                        "performs the edit: patch_file, write_file, or deploy_patch (use deploy_patch for system "
                        ".py files). read_file is only preparation — every read_file must be followed by the "
                        "patch/write step it prepares. Re-issue the COMPLETE plan WITH the write/patch steps. "
                        "Output only the submit_plan tool call."})
                elif _fail_reason == "new_file_patch":
                    _msgs.append({"role": "user", "content":
                        f"Your previous plan used patch_file on `{_bad_newfile}`, but that file does NOT exist "
                        "yet. patch_file requires an existing anchor — you cannot patch a file that isn't there. "
                        "For any file you are CREATING, use write_file with the COMPLETE file contents in the "
                        "'content' field (and do NOT emit a read_file step for a file that doesn't exist). "
                        "Re-issue the plan using write_file for the new file. Output only the submit_plan tool call."})
                else:
                    _msgs.append({"role": "user", "content":
                        "CRITICAL: respond ONLY by calling submit_plan with valid JSON arguments. "
                        "Do NOT reason, explain, or write prose. If file contents are unknown, still "
                        "produce a best-effort plan (read_file steps first). Output the tool call, nothing else."})
            llm_response = await llm_chat(
                "coder",
                messages=_msgs,
                temperature=0.1 if _attempt == 0 else 0.45,  # bump temp on retry for variance
                format="json",
                tools=_SUBMIT_PLAN_TOOL,
                tool_choice="required",
                max_tokens=8192,   # full file bodies need headroom; 300s time budget
                timeout=300,
            )
            if not llm_response.success:
                logger.warning(f"coder plan attempt {_attempt} failed: {llm_response.error}")
                _fail_reason = "parse"
                continue
            content = llm_response.text
            logger.info(f"LLM returned (attempt {_attempt}): {content[:160]}...")
            cand = _extract_plan_json(content)
            if not isinstance(cand, dict):
                logger.warning(f"coder plan attempt {_attempt}: no parseable JSON; escalating")
                _fail_reason = "parse"
                continue
            # Implementation-consistency guard: an implement task MUST mutate.
            # Keep the best-effort plan in case we exhaust retries (bigbrain backstops).
            if _needs_mut and not _plan_has_mutation(cand):
                plan_data = cand
                logger.warning(f"coder plan attempt {_attempt}: recon-only (no mutation step) for an "
                               f"implementation task; re-prompting with mutation directive")
                _fail_reason = "no_mutation"
                continue
            # New-file guard: patch_file on a non-existent file fails (no anchor) — must be write_file.
            _bad_newfile = _plan_patches_nonexistent(cand)
            if _bad_newfile:
                plan_data = cand
                logger.warning(f"coder plan attempt {_attempt}: patch_file on non-existent {_bad_newfile} "
                               f"(new file); re-prompting for write_file")
                _fail_reason = "new_file_patch"
                continue
            plan_data = cand
            _fail_reason = None
            logger.info(f"coder plan accepted on attempt {_attempt} "
                        f"(needs_mutation={_needs_mut}, has_mutation={_plan_has_mutation(cand)})")
            break
        if _fail_reason == "no_mutation":
            logger.warning("coder: exhausted retries; plan still recon-only — passing to review as-is (bigbrain backstop)")

        # 4. Parse JSON
        try:
            if plan_data is None:
                raise ValueError(f"coder produced no parseable plan after retries: {content[:200]}")

            # Phase 39: Handle double-encoded JSON (LLM returns JSON string inside JSON)
            if isinstance(plan_data, str):
                logger.info("⚠️ Double-encoded JSON detected, re-parsing")
                plan_data = json.loads(plan_data)

            # Normalize keys
            if not isinstance(plan_data, dict):
                raise ValueError(f"Expected dict from LLM, got {type(plan_data).__name__}")
            if "initial_context" not in plan_data:
                plan_data["initial_context"] = {}

            # Phase 18.1: Normalize LLM output - models drift on key names
            # (uses _plan_steps so qwen3-coder's nested tasks->steps get flattened, 2026-06-05)
            plan_steps = _plan_steps(plan_data)

            # Phase 18.2: Handle bare step object (no plan wrapper)
            # If LLM returned {"action": "read_file", ...} instead of {"plan": [...]}
            if not plan_steps and "action" in plan_data:
                logger.info("⚠️ LLM returned bare step object, wrapping in plan")
                plan_steps = [plan_data]
            normalized_steps = []

            # Path drift defense: extract any absolute paths the user named in
            # their task. The per-step sanitizer below will override the model's
            # path with the user's path if they don't match (single-path case),
            # or log a mismatch warning (multi-path case). This catches the
            # Apr 6 verifier_test.txt regression where GPT-OSS 20B invented an
            # entirely different path than the one the user requested.
            user_paths = _extract_user_paths(request.query)
            if user_paths:
                logger.info(f"🛡️ User-provided paths detected: {user_paths}")
            # Phase 20.3: Action aliases — LLMs invent action names
            ACTION_ALIASES = {
                "insert_code": "deploy_patch",
                "add_code": "deploy_patch",
                "modify_file": "deploy_patch",
                "edit_file": "patch_file",
                "update_file": "patch_file",
                "create_file": "write_file",
                # qwen3-coder schema-map (2026-06-05): it emits bare verbs (edit/create/write/read/modify) + a `file` key
                "edit": "patch_file",
                "modify": "patch_file",
                "create": "write_file",
                "write": "write_file",
                "new_file": "write_file",
                "read": "read_file",
                "search": "web_search",
                "execute": "run_command",
                # Phase 23: RAG aliases
                "query_rag": "rag_query",
                "search_memory": "rag_query",
                "memory_search": "rag_query",
                "search_rag": "rag_query",
                # Phase 23: Log reading alias
                "read_log": "run_command",
                "read_logs": "run_command",
                "view_log": "run_command",
            }

            for i, step in enumerate(plan_steps):
                # Phase 39: Skip non-dict steps (LLM returned strings instead of objects)
                if isinstance(step, str):
                    try:
                        step = json.loads(step)
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(f"⚠️ Skipping non-dict plan step: {step[:80]}")
                        continue
                if not isinstance(step, dict):
                    logger.warning(f"⚠️ Skipping non-dict plan step type: {type(step).__name__}")
                    continue
                raw_action = step.get("action", step.get("type", "unknown"))
                resolved_action = ACTION_ALIASES.get(raw_action, raw_action)
                if resolved_action != raw_action:
                    logger.info(f"🔄 Action alias: {raw_action} -> {resolved_action}")
                ns = {"id": step.get("id", i + 1), "action": resolved_action}

                # Phase 23: Auto-build log read command from alias
                if raw_action in ("read_log", "read_logs", "view_log") and resolved_action == "run_command":
                    service = step.get("details", "").strip().lower()
                    log_path = f"{PROJECT_BASE_DIR}/logs/{service}.log"
                    ns["details"] = f"tail -n 50 '{log_path}'"
                    logger.info(f"📋 Auto-built log command for service: {service}")

                # Phase 18.2: Unwrap nested "parameters" object
                # Some LLM outputs nest action details inside {"parameters": {...}}
                params = step.get("parameters", {})
                if isinstance(params, dict):
                    # Merge params keys into step for downstream extraction
                    for pk, pv in params.items():
                        if pk not in step:
                            step[pk] = pv

                # Map alternative key names -> "details"
                if ns["action"] == "deploy_patch":
                    # DEBUG: Log raw step keys so we can see what the LLM produced
                    logger.info(f"🔍 RAW deploy_patch step keys: {list(step.keys())}")
                    logger.info(f"🔍 RAW deploy_patch step: {json.dumps({k: str(v)[:80] for k, v in step.items()}, indent=2)}")
                    # deploy_patch: details = service name
                    ns["details"] = step.get("details") or step.get("service") or step.get("service_name") or step.get("target") or ""
                else:
                    ns["details"] = step.get("details") or step.get("file_path") or step.get("filepath") or step.get("path") or step.get("file") or step.get("target") or step.get("command") or step.get("query") or ""

                # Guard: `details` must be a str — the path-fix ops + several
                # Path(ns["details"]) calls below (and the worker) assume it. LLMs
                # occasionally emit it as a dict on large write_file steps (nesting
                # path + content) → Path(dict) raised "not 'dict'" and 500'd planning.
                # Lift the path (and content, if nested) out, then ensure str.
                # (Surfaced 2026-05-25 building the snake game via the chain.)
                if isinstance(ns["details"], dict):
                    _dd = ns["details"]
                    if not step.get("content") and _dd.get("content"):
                        step["content"] = _dd["content"]
                    ns["details"] = (_dd.get("path") or _dd.get("file_path") or _dd.get("filepath")
                                     or _dd.get("details") or _dd.get("target") or _dd.get("name") or "")
                if not isinstance(ns["details"], str):
                    ns["details"] = "" if ns["details"] is None else str(ns["details"])

                # Phase 20.3: Auto-detect system file targets and route to deploy_patch
                SYSTEM_FILES_MAP = {
                    "worker.py": "worker", "orchestrator.py": "orchestrator", "coder.py": "coder",
                    "gateway.py": "gateway", "dashboard.py": "orchestrator", "companion.py": "orchestrator",
                    "schemas.py": "worker", "deployer.py": "worker",
                }
                if ns["action"] in ("patch_file", "write_file") and ns["details"]:
                    target_name = Path(ns["details"]).name
                    if target_name in SYSTEM_FILES_MAP:
                        svc = SYSTEM_FILES_MAP[target_name]
                        logger.info(f"🔄 Auto-routing {ns['action']} on {target_name} -> deploy_patch (service={svc})")
                        ns["action"] = "deploy_patch"
                        # Move file content to deploy_patch format
                        if ns.get("content") and "===" not in ns.get("content", ""):
                            # Content has no delimiters — likely raw code to insert
                            # Try to build ===PATCH=== from anchor + new code
                            pass  # Let the Worker's format handlers deal with it
                        ns["details"] = svc

                # Ensure absolute paths for file operations
                if ns["action"] in ("read_file", "write_file", "patch_file") and ns["details"] and not os.path.isabs(ns["details"]):
                    # Resolve relative path against working_dir or project root
                    base = plan_data.get("initial_context", {}).get("working_dir", PROJECT_BASE_DIR)
                    # Normalize tilde-paths in the working_dir before joining
                    if base.startswith("~"):
                        base = os.path.expanduser(base)
                    ns["details"] = os.path.join(base, ns["details"])

                # De-root guard (2026-05-25): the model sometimes emits a project file
                # at filesystem ROOT (e.g. "/snake.html", "/games/snake.html") when it
                # means a project-relative path — writing to / is never intended. If an
                # absolute write/patch target's top-level dir isn't a real system
                # location and it isn't already under the project, relocate it into the
                # project root. (Relative targets are already joined to the project above;
                # this catches the rooted-absolute case the relative resolver misses.)
                if ns["action"] in ("write_file", "patch_file") and isinstance(ns.get("details"), str) and os.path.isabs(ns["details"]):
                    _base = PROJECT_BASE_DIR.rstrip("/")
                    _p = ns["details"]
                    if _p != _base and not _p.startswith(_base + "/"):
                        _top = _p.lstrip("/").split("/", 1)[0]
                        _SYS_TOPS = {"home", "etc", "tmp", "var", "usr", "opt", "mnt",
                                     "media", "srv", "root", "bin", "sbin", "lib", "lib64",
                                     "boot", "dev", "proc", "sys", "run", "snap"}
                        if _top not in _SYS_TOPS:
                            _fixed = _base + "/" + _p.lstrip("/")
                            logger.info(f"PATH FIX (de-root → project): {_p} -> {_fixed}")
                            ns["details"] = _fixed

                # Normalize and fix common LLM path hallucinations on Linux.
                if ns["action"] in ("read_file", "write_file", "patch_file", "run_command") and ns.get("details"):
                    detail = ns["details"]

                    # Expand `~` to absolute home so downstream consumers don't have to.
                    if "~" in detail:
                        import re as _re_tilde
                        # Only expand `~` or `~user` at start of a path token (after start, space, or quote)
                        fixed = _re_tilde.sub(
                            r'(^|[\s"\'])~([A-Za-z0-9_-]*)(/)',
                            lambda m: m.group(1) + os.path.expanduser("~" + m.group(2)) + m.group(3),
                            detail,
                        )
                        if fixed != detail:
                            logger.info(f"PATH FIX (tilde-expand): {detail[:80]} -> {fixed[:80]}")
                            ns["details"] = fixed
                            detail = fixed

                    # Fix hallucinated usernames — only the running user's name is valid in /home/<user>/...
                    import re as _re2
                    wrong_user = _re2.search(rf'/home/(?!{_re2.escape(USER_NAME)}/)([^/]+)/', detail)
                    if wrong_user:
                        fixed = _re2.sub(r'/home/[^/]+/', rf'/home/{USER_NAME}/', detail)
                        logger.info(f"PATH FIX (wrong user): {detail[:80]} -> {fixed[:80]}")
                        ns["details"] = fixed
                        detail = fixed

                # Fix run_command steps that reference bare home paths instead of the project root.
                # LLMs sometimes guess /home/<user>/filename instead of /home/<user>/<project>/filename.
                if ns["action"] == "run_command" and ns.get("details"):
                    cmd = ns["details"]
                    wrong_root = f"/home/{USER_NAME}/"
                    correct_root = PROJECT_BASE_DIR.rstrip("/") + "/"
                    # Replace bare home-dir paths that aren't legitimate user-data subdirs
                    import re as _re
                    legit = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "Public", "Templates", ".", ".config", ".local", ".ssh", ".cache")
                    def _fix_path(m):
                        full = m.group(0)
                        after = full[len(wrong_root):]
                        if any(after.startswith(l) for l in legit):
                            return full
                        # If the path already points into the project base, leave it alone.
                        # Compare with trailing slash on BOTH sides so the regex match
                        # /home/user/RouxYou (no trailing /) doesn't fail the check just
                        # because correct_root has a trailing slash — that mismatch caused
                        # path-doubling on valid in-project paths (2026-05-23 fix).
                        if (full + "/").startswith(correct_root):
                            return full
                        # If the path EXISTS on disk as-is, the LLM cited a real file/dir
                        # outside the project (e.g. /home/user/from_windows/..., /home/user/TheSeed/...,
                        # /home/user/claude-memory-mcp/...). Don't mangle — that breaks legitimate
                        # cross-project references. Hallucinated paths still won't exist, so the
                        # heuristic below still catches them. (2026-05-24 fix after the path-fix
                        # middleware mangled /home/user/from_windows/.../models.py during scheduler
                        # port #2.)
                        if os.path.exists(full):
                            return full
                        return correct_root + after
                    fixed_cmd = _re.sub(
                        rf'/home/{_re.escape(USER_NAME)}/[^/\s"\']+',
                        _fix_path,
                        cmd
                    )
                    if fixed_cmd != cmd:
                        logger.info(f"PATH FIX (run_command): {cmd[:80]} -> {fixed_cmd[:80]}")
                        ns["details"] = fixed_cmd

                # User-provided paths are a hard constraint.
                # If the user gave an explicit absolute path and the model is doing
                # a file operation, the model's path MUST match. Otherwise override.
                if user_paths and ns["action"] in ("write_file", "patch_file", "read_file") and ns.get("details"):
                    model_path_norm = _normalize_path_for_compare(ns["details"])
                    user_paths_norm = [_normalize_path_for_compare(p) for p in user_paths]

                    if model_path_norm not in user_paths_norm:
                        if len(user_paths) == 1:
                            override = user_paths[0]
                            logger.warning(
                                f"🛡️ PATH OVERRIDE (user-constraint): model chose "
                                f"'{ns['details']}' but user explicitly requested "
                                f"'{override}'. Overriding to user's path."
                            )
                            ns["details"] = override
                        else:
                            # Multiple user paths — try to match by filename
                            from pathlib import Path as _PP
                            model_filename = _PP(ns["details"]).name.lower()
                            best_match = None
                            for up in user_paths:
                                if _PP(up).name.lower() == model_filename:
                                    best_match = up
                                    break
                            if best_match:
                                logger.warning(
                                    f"🛡️ PATH OVERRIDE (user-constraint, filename match): "
                                    f"model chose '{ns['details']}', matched user path "
                                    f"'{best_match}' by filename."
                                )
                                ns["details"] = best_match
                            else:
                                # Cannot auto-resolve; verifier will catch any drift downstream.
                                logger.warning(
                                    f"⚠️ PATH MISMATCH (multi-path, no filename match): "
                                    f"model chose '{ns['details']}', user provided "
                                    f"{user_paths}. Leaving for verifier to flag."
                                )

                # Phase 18.2: Codebase index path correction
                # If LLM guessed a path that doesn't exist, check if the filename
                # matches a known module in the codebase index and use that instead.
                if ns["action"] in ("read_file", "write_file", "patch_file", "verify_fix") and ns["details"]:
                    target_path = Path(ns["details"])
                    if not target_path.exists():
                        target_name = target_path.name
                        for mod_name, file_idx in codebase_index.files.items():
                            if file_idx.filepath.name == target_name and file_idx.filepath.exists():
                                logger.info(f"📍 PATH FIX: {ns['details']} -> {file_idx.filepath} (via codebase index)")
                                ns["details"] = str(file_idx.filepath)
                                break

                # Map alternative content structures -> "content" with delimiters
                # Phase 20.3: For aliased actions (insert_code -> deploy_patch),
                # build ===PATCH=== content from the LLM's various field names
                if ns["action"] == "deploy_patch" and not ns.get("content"):
                    # Try to build content from anchor/code fields
                    anchor = step.get("anchor") or step.get("anchor_text") or step.get("after") or step.get("find") or step.get("find_text") or ""
                    new_code = step.get("code") or step.get("new_code") or step.get("insert") or step.get("content") or step.get("replace") or step.get("replace_text") or step.get("new_text") or ""
                    if anchor and new_code:
                        ns["content"] = anchor + "\n===PATCH===\n" + new_code
                        logger.info(f"Built ===PATCH=== content from aliased action fields")
                    elif new_code:
                        # No anchor but has code — this will need the Worker to figure out
                        ns["content"] = new_code

                if ns["action"] == "deploy_patch":
                    # deploy_patch: content should be JSON patches array
                    # But LLMs might use various key names or formats
                    raw_content = step.get("content") or step.get("patches") or step.get("patch") or ""

                    # If LLM provided structured find/replace keys, build JSON patches
                    if not raw_content and ("find" in step or "find_text" in step or "anchor_text" in step):
                        find_t = step.get("find") or step.get("find_text") or step.get("anchor_text") or step.get("old_text") or ""
                        replace_t = step.get("replace") or step.get("replace_text") or step.get("new_text") or step.get("new_code") or ""
                        file_t = step.get("file") or step.get("file_path") or step.get("filepath") or "worker.py"
                        # Infer file from service if not given
                        if file_t == "worker.py" and ns["details"]:
                            svc_files = {"worker": "worker.py", "orchestrator": "orchestrator.py", "coder": "coder.py"}
                            file_t = svc_files.get(ns["details"], file_t)
                        raw_content = json.dumps([{"file": file_t, "find": find_t, "replace": replace_t}])
                        logger.info(f"Normalized deploy_patch: built JSON from find/replace keys")

                    # Convert to string, preserving ===PATCH===/===REPLACE=== format
                    if isinstance(raw_content, list):
                        # JSON array of patches — convert to ===REPLACE=== format
                        # (Worker prefers this over JSON)
                        if raw_content and isinstance(raw_content[0], dict) and "find" in raw_content[0]:
                            p = raw_content[0]
                            ns["content"] = p["find"] + "\n===REPLACE===\n" + p.get("replace", "")
                            logger.info(f"Converted JSON patches to ===REPLACE=== format for Worker")
                        else:
                            ns["content"] = json.dumps(raw_content)
                    elif isinstance(raw_content, str):
                        ns["content"] = raw_content
                    else:
                        ns["content"] = str(raw_content)

                    # Infer service name from file if details is empty
                    if not ns["details"] and ns["content"]:
                        try:
                            p = json.loads(ns["content"]) if isinstance(ns["content"], str) else ns["content"]
                            if isinstance(p, list) and p and "file" in p[0]:
                                fname = Path(p[0]["file"]).name
                                svc_map = {"worker.py": "worker", "orchestrator.py": "orchestrator", "coder.py": "coder"}
                                ns["details"] = svc_map.get(fname, "worker")
                                logger.info(f"Inferred deploy service from file: {ns['details']}")
                        except:
                            pass
                    if not ns["details"]:
                        ns["details"] = "worker"  # default fallback
                        logger.info(f"Defaulting deploy service to 'worker'")
                elif ns["action"] == "patch_file":
                    if "content" in step:
                        ns["content"] = step["content"]
                    elif "anchor_text" in step and "new_code" in step:
                        # Model split anchor and new code into separate fields
                        ns["content"] = step["anchor_text"] + "\n===PATCH===\n" + step["new_code"]
                        logger.info(f"Normalized anchor_text+new_code into patch content")
                    elif "find_text" in step and "replace_text" in step:
                        ns["content"] = step["find_text"] + "\n===REPLACE===\n" + step["replace_text"]
                        logger.info(f"Normalized find_text+replace_text into replace content")
                    elif "old_text" in step and "new_text" in step:
                        ns["content"] = step["old_text"] + "\n===REPLACE===\n" + step["new_text"]
                        logger.info(f"Normalized old_text+new_text into replace content")
                    else:
                        ns["content"] = step.get("content", "")
                elif "content" in step:
                    ns["content"] = step["content"]

                normalized_steps.append(ns)
                if ns["action"] == "deploy_patch":
                    logger.info(f"Step {ns['id']}: {ns['action']} -> service={ns.get('details','')} content={str(ns.get('content',''))[:120]}")
                else:
                    logger.info(f"Step {ns['id']}: {ns['action']} -> {str(ns.get('details',''))[:60]}")

            # Plan hygiene pre-pass (2026-06-02): (a) drop steps with junk/unknown
            # actions the model occasionally invents (e.g. "test_function"); (b) for a
            # file that doesn't exist yet but is patch_file'd, it's a NEW file — drop
            # any read_file targeting it (you can't read a file you're creating, and
            # read→write of the same new file reads as a destructive "clobber" to review).
            _KNOWN_ACTIONS = {"read_file", "write_file", "patch_file", "run_command",
                              "verify_fix", "web_search", "rag_query", "restart_service", "deploy_patch"}
            _newfile_paths = {ns["details"] for ns in normalized_steps
                              if ns.get("action") == "patch_file" and ns.get("details")
                              and not os.path.exists(ns["details"])}
            _cleaned = []
            _seen_patch = set()
            for ns in normalized_steps:
                if ns.get("action") not in _KNOWN_ACTIONS:
                    logger.info(f"🧹 dropping junk step: action={ns.get('action')!r} details={str(ns.get('details',''))[:40]!r}")
                    continue
                if ns.get("action") == "read_file" and ns.get("details") in _newfile_paths:
                    logger.info(f"🧹 dropping read_file for new file {ns['details']} (will be write_file'd)")
                    continue
                # Dedup redundant patch_file steps on the same file (model sometimes emits
                # the same edit 2-3x). Single-change proposals = one patch per file.
                if ns.get("action") == "patch_file" and ns.get("details"):
                    if ns["details"] in _seen_patch:
                        logger.info(f"🧹 dropping duplicate patch_file on {ns['details']}")
                        continue
                    _seen_patch.add(ns["details"])
                _cleaned.append(ns)
            for _i, ns in enumerate(_cleaned, 1):
                ns["id"] = _i
            normalized_steps = _cleaned

            # No-mutation rescue (2026-06-05, lever #2): agentic coders (qwen3-coder) are
            # non-deterministically STUBBORN — they plan only a read_file and stop, OR refuse a
            # CREATE with an empty "insufficient context" plan. If the task needs a mutation and
            # the plan has none, synthesize the mutation step for the file the task names and let
            # the robust content-gen below produce it: patch_file if the file EXISTS (edit),
            # write_file if it does NOT (create). Deterministic — not at the mercy of the model's
            # planning mood. (The earlier mutation-directive retries can't break either case.)
            if _needs_mut and not any(_step_action(s) in _MUTATION_ACTIONS for s in normalized_steps):
                import re as _re_rescue
                _read_targets = [_step_path(s) for s in normalized_steps if _step_action(s) == "read_file"]
                _named = _re_rescue.findall(r'[\w./-]+\.py', request.query or "")  # user_paths misses relative names
                for _tp in (_read_targets or user_paths or _named):
                    _rp = _resolve_under_roux(_tp) if _tp else None
                    if _rp is None or not str(_rp).endswith(".py"):
                        continue
                    _abs = str(_rp)
                    if os.path.exists(_abs):
                        normalized_steps.append({"action": "patch_file", "details": _abs,
                                                 "content": "__GENERATE__", "id": len(normalized_steps) + 1})
                        logger.info(f"🩹 no-mutation rescue: synthesized patch_file for existing {_abs}")
                    else:
                        normalized_steps.append({"action": "write_file", "details": _abs,
                                                 "content": "__GENERATE__", "id": len(normalized_steps) + 1})
                        logger.info(f"🆕 no-mutation rescue: synthesized write_file for NEW {_abs} "
                                    f"(coder returned empty/refusal plan for a create task)")
                    break  # one atomic change

            # CREATE-task content generation (2026-05-25): the JSON plan mode
            # truncates large file bodies (qwen2.5-coder stochastically stops
            # mid-file inside constrained JSON — snake-game probe). For write_file
            # steps whose content is the __GENERATE__ placeholder, empty, or
            # suspiciously short, regenerate the full body in a SEPARATE
            # UNCONSTRAINED (non-JSON) coder call so it never truncates. The plan
            # JSON stays small + parses reliably; the content is generated free.
            for ns in normalized_steps:
                act = ns.get("action")
                det = ns.get("details")
                if act not in ("write_file", "patch_file") or not det:
                    continue
                c = (ns.get("content") or "").strip()
                # NEW-FILE conversion: a patch_file on a file that doesn't exist is
                # really a CREATE — patch_file has no anchor to bind to. Flip it to
                # write_file (full-body gen below). Runs post-path-resolution so the
                # existence check is reliable. 2026-06-02.
                if act == "patch_file" and not os.path.exists(det):
                    logger.info(f"🆕 patch_file on non-existent {det} → write_file (new file)")
                    ns["action"] = act = "write_file"
                # Does this step need content generated? Empty/placeholder/too-short,
                # OR a patch_file whose content lacks the required delimiter.
                needs_gen = (c == "__GENERATE__") or (len(c) < 40) or (
                    act == "patch_file" and "===PATCH===" not in c and "===REPLACE===" not in c
                )
                if not needs_gen:
                    continue
                if act == "write_file":
                    fresh = await _generate_file_content(det, request.query, c)
                    if fresh:
                        logger.info(f"📝 write_file content generated via raw call: {det} ({len(fresh)}b)")
                        ns["content"] = fresh
                else:  # patch_file on an existing file with no usable content
                    fresh = await _generate_patch_content(det, request.query)
                    if fresh:
                        logger.info(f"🩹 patch_file content generated via raw call: {det} ({len(fresh)}b)")
                        ns["content"] = fresh

            # Lever #3 (self-edit verify-fix): verify each step's generated .py before bigbrain;
            # regenerate with the error fed back on failure. Flag-gated A/B, default OFF.
            if os.environ.get("ROUX_SELF_EDIT", "0") == "1":
                logger.info("🩺 ROUX_SELF_EDIT on — verify-fix pass over generated steps")
                for ns in normalized_steps:
                    await _self_edit_verify_fix(ns, request)

            plan_data["plan"] = normalized_steps
            plan_data["success"] = plan_data.get("success", True)

            # Broadcast the plan steps
            if normalized_steps:
                step_descriptions = [f"{s.get('action', '?')}: {str(s.get('details', ''))[:40]}..." for s in normalized_steps[:5]]
                broadcast_plan(step_descriptions)
                set_thought(f"Plan created with {len(normalized_steps)} steps. Sending to Worker...")

            return plan_data
        except json.JSONDecodeError:
            # Fallback for bad JSON
            logger.error("Failed to parse JSON response")
            return {"success": False, "error": "Invalid JSON from LLM"}

    except Exception as e:
        logger.error(f"Planning failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class DiagnoseRequest(BaseModel):
    """Simple request for error diagnosis"""
    prompt: str

@app.post("/diagnose")
async def diagnose_error(request: DiagnoseRequest):
    """Direct LLM call for error diagnosis - no planning, just text response"""
    logger.info(f"Diagnosing error...")
    
    try:
        # Phase 34: via shared/llm.py provider abstraction
        llm_response = await llm_generate(
            "router",  # ministral-3:8b — fast diagnosis
            prompt=request.prompt,
            timeout=60,
        )

        if not llm_response.success:
            return {"success": False, "response": llm_response.error}

        logger.info(f"Diagnosis response: {llm_response.text[:200]}...")
        return {"success": True, "response": llm_response.text}

    except Exception as e:
        logger.error(f"Diagnosis failed: {e}")
        return {"success": False, "response": str(e)}

async def _warmup_background():
    """Boot-warmup is INTENTIONALLY DISABLED (2026-05-29).

    Why: on the 16GB card the coder (qwen2.5-coder:14b, ~9GB GPU) and the
    companion/reasoning model (roux-vNN, ~18GB GPU) cannot co-reside. Warming
    the coder at startup collided with the orchestrator's companion warmup
    (orchestrator.py:1484) — both big GPU models tried to land at once →
    "requires more gpu memory, evicting" thrash that starved live /companion
    turns (empty-fallback responses). The coder is evicted whenever it's idle
    anyway, so a boot warm buys nothing: it just reloads on the first real
    /plan call (the intended coder↔roux evict+reload swap, MAX_LOADED_MODELS=3).
    Trade: first /plan pays a one-time cold-start. Worth it — keeps the resident
    companion model stable. Re-enable only if coder gets its own GPU/headroom.
    """
    logger.info("Coder boot-warmup skipped by design (avoids GPU co-residency "
                "thrash with the companion model on the 16GB card; coder loads "
                "lazily on first /plan).")
    return

@app.on_event("startup")
async def startup_event():
    import asyncio
    register_process("coder")
    init_providers()  # Phase 34: Initialize LLM provider registry
    logger.info(f"Coder initialized on port {PORT} using {MODEL_NAME}")
    logger.info(f"Codebase index: {len(codebase_index.files)} modules mapped")
    # Apr 6 2026: kick warmup as a background task so cold-start latency
    # doesn't bite the first /plan call. Service stays healthy throughout.
    asyncio.create_task(_warmup_background())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
