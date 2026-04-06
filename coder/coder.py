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
MODEL_NAME = "gpt-oss:20b"

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
    """Quick check if SearXNG is reachable. Used to inform Coder."""
    import urllib.request
    try:
        urllib.request.urlopen("http://192.168.1.188:8888", timeout=2)
        return True
    except:
        return False

def _extract_user_paths(text: str) -> List[str]:
    """Extract absolute Windows paths from a user task string.

    Catches: C:\\path\\file.txt, C:/path/file.txt, C:\\\\path\\\\file.txt (escaped),
    quoted or unquoted, with or without surrounding sentence punctuation.

    Used by the coder's path sanitizer to detect when GPT-OSS 20B has invented
    its own path instead of using one the user explicitly named. Caught the
    Apr 6 verifier_test.txt regression where the model wrote to
    /SelfModifyingAgents/hello instead of the requested /Desktop/verifier_test.txt.
    """
    if not text:
        return []
    import re as _re_paths
    # Drive letter + colon + (back)slash + path chars (excluding whitespace and shell metacharacters)
    pattern = r'[A-Za-z]:[\\/](?:[^\s"\'<>|*?]+)'
    matches = _re_paths.findall(pattern, text)
    cleaned = []
    for m in matches:
        # Strip trailing sentence punctuation that almost certainly isn't part of the path.
        # File extensions have a dot followed by alpha chars, not a trailing dot.
        while m and m[-1] in '.,;:!?)]}':
            m = m[:-1]
        # Collapse over-escaped backslashes (\\\\ -> \\) so the comparison is normalized at extract time
        if "\\\\" in m:
            m = _re_paths.sub(r'\\{2,}', r'\\', m)
        if m:
            cleaned.append(m)
    return cleaned

def _normalize_path_for_compare(p: str) -> str:
    """Normalize a Windows path for case-insensitive comparison.
    Forward slashes become backslashes, lowercased, trailing slash stripped.
    """
    if not p:
        return ""
    return p.replace("/", "\\").lower().rstrip("\\")

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

    return f"""
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
      C:\\Users\\djoet\\Desktop\\SelfModifyingAgents

    When a task refers to a file by name only (e.g. "thank_you.txt", "notes.md"),
    ALWAYS assume it lives in that directory UNLESS context (memories or task
    description) explicitly says otherwise.
    NEVER guess C:\\Users\\djoet\\ as the root. That is almost always wrong.

    ## USER-PROVIDED PATHS (HARD CONSTRAINT — READ THIS)
    If the user's task contains an absolute path (anything matching C:\\... or C:/...),
    that path is a HARD CONSTRAINT, not a suggestion. You MUST use it EXACTLY as written:
    - Same drive letter
    - Same directory chain
    - Same filename
    - Same extension

    DO NOT invent a different filename. DO NOT "simplify" the path. DO NOT relocate
    the file to a more sensible directory. The user picked that path on purpose.

    Example:
      User: "create a file at C:/Users/djoet/Desktop/verifier_test.txt with the word hello"
      CORRECT: write_file at C:\\Users\\djoet\\Desktop\\verifier_test.txt
      WRONG:   write_file at C:\\Users\\djoet\\Desktop\\SelfModifyingAgents\\hello
      WRONG:   write_file at C:\\Users\\djoet\\Desktop\\verifier.txt
      WRONG:   write_file at .\\verifier_test.txt

    If you cannot use the user's path for some reason (e.g. it does not exist for a
    read operation), return {{"success": false, "error": "Cannot use path X because Y"}}
    instead of substituting a different path. The pipeline has a sanitizer that will
    OVERRIDE your path with the user's path if you ignore this rule, and the override
    is logged loudly. Don't make us correct you.

    ## OPERATING SYSTEM: WINDOWS
    This system runs on Windows 10/11. When using run_command:
    - Use `dir` NOT `ls`, `type` NOT `cat`, `copy` NOT `cp`, `del` NOT `rm`
    - PowerShell: prefix with `powershell -Command "..."`
    - Home directory: C:\\Users\\djoet

    ## PATH ESCAPING (READ CAREFULLY)
    You output JSON. Inside JSON strings, a single backslash is written as "\\".
    That means a Windows path C:\\Users\\djoet\\Desktop must appear in your JSON as:
      "C:\\\\Users\\\\djoet\\\\Desktop"  (each \\ becomes \\\\)
    DO NOT use more than double backslashes in the JSON source. When parsed the
    path will contain single backslashes, which is what Windows expects.
    If unsure, prefer forward slashes: "C:/Users/djoet/Desktop" — Windows accepts
    these and no escaping is needed.
    
    {memory_text}
    
    {skill_text}
    
    ## SYSTEM KNOWLEDGE (YOUR OWN INFRASTRUCTURE)
    Log files are at: C:\\Users\\djoet\\Desktop\\SelfModifyingAgents\\logs\\
    Available logs: coder.log, worker.log, orchestrator.log, watchtower.log,
                    gateway.log, deployer.log, memory.log, task_queue.log
    To READ a log: {{"action": "run_command", "details": "powershell -Command \"Get-Content C:\\Users\\djoet\\Desktop\\SelfModifyingAgents\\logs\\coder.log -Tail 50\""}}
    To LIST all logs: {{"action": "run_command", "details": "dir C:\\Users\\djoet\\Desktop\\SelfModifyingAgents\\logs"}}
    The shared logger function signature: read_log(service_name: str) — requires the service name.
    
    Services and ports:
      - Gateway: 8000 | Orchestrator: 8001 | Coder: 8002 | Worker: 8003 | Watchtower: 8010
    
    Memory file: C:\\Users\\djoet\\Desktop\\SelfModifyingAgents\\memory.json
    Task registry: C:\\Users\\djoet\\Desktop\\SelfModifyingAgents\\tasks.json
    
    ## WEB SEARCH AVAILABILITY
    Web search uses SearXNG hosted on the Proxmox server (192.168.1.188:8888).
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
       Format: {{"action": "read_file", "details": "C:\\\\full\\\\path\\\\file.py"}}
    
    2. **write_file** — Create NEW files ONLY. Never use on existing files.
       Format: {{"action": "write_file", "details": "C:\\\\full\\\\path\\\\new_file.py", "content": "full file contents"}}
    
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
    
    4. **run_command** — Execute shell commands.
       Format: {{"action": "run_command", "details": "python script.py"}}
    
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
    
    8. **deploy_patch** — Deploy code changes to system files via blue-green pipeline.
       ⚠️ MANDATORY for ALL system .py files (worker.py, orchestrator.py, coder.py, etc.)
       Direct write_file/patch_file on system files is BLOCKED. You MUST use this action.
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
        "working_dir": "C:\\\\path\\\\to\\\\project",
        "active_file": "target.py",
        "detected_errors": []
      }},
      "plan": [
        {{"id": 1, "action": "read_file", "details": "C:\\\\path\\\\file.py"}},
        {{"id": 2, "action": "patch_file", "details": "C:\\\\path\\\\file.py", "content": "exact anchor line\n===PATCH===\nnew code"}},
        {{"id": 3, "action": "verify_fix", "details": "file.py"}}
      ],
      "unverified_assumptions": []  // optional — list any claims from the task you could not verify
    }}

    ## PLANNING RULES
    1. EVERY edit workflow: read_file first, then edit, then verify_fix. No exceptions.
    2. EVERY plan ends with verify_fix as the final step.
    3. Use patch_file for NORMAL files. Use write_file ONLY for brand new files.
    4. All paths must be absolute (start with C:\\).
    5. If memories contain Working Code Patterns, adapt that code instead of reinventing.
    6. Before patching, confirm: do I have the exact anchor text from a read_file? If no, read first.
    
    ## SCOPE & EFFICIENCY RULES (CRITICAL)
    7. NEVER run analysis tools (flake8, pylint, grep, findstr) on the ENTIRE project directory.
       Always target SPECIFIC files or at most a single subdirectory.
       BAD:  run_command "flake8 C:\\Users\\djoet\\Desktop\\SelfModifyingAgents"
       GOOD: run_command "flake8 C:\\Users\\djoet\\Desktop\\SelfModifyingAgents\\orchestrator\\orchestrator.py"
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
    
    ## ⚠️ CRITICAL: SYSTEM FILE EDITING RULE (MANDATORY)
    System .py files (worker.py, orchestrator.py, coder.py, dashboard.py, schemas.py,
    gateway.py, deployer.py, companion.py, memory_agent.py, etc.) CANNOT be edited
    with patch_file or write_file. The Worker will REJECT those actions on system files.
    
    You MUST use **deploy_patch** (action #8) for ANY change to system .py files.
    Workflow: read_file → deploy_patch → (no verify_fix needed, deploy pipeline tests it)
    
    If you use patch_file on worker.py, orchestrator.py, or coder.py, it WILL FAIL.
    Use deploy_patch instead. This is not optional.

    ## CONTEXT EXTRACTION
    - If a relevant memory exists, use its working_dir.
    - Subfolder references like "m_t/trap.py" expand to: C:\\Users\\djoet\\Desktop\\SelfModifyingAgents\\m_t\\trap.py
    - NEVER use partial paths like "C:\\m_t" — always use the full absolute path.
    - Always populate initial_context with working_dir, active_file, and detected_errors.

    ## SYSTEM ARCHITECTURE
    {system_map}
    """

@app.post("/plan")
async def generate_plan(request: PlanRequest):
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
        llm_response = await llm_chat(
            "coder",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": f"Task: {request.query}\nContext: {request.context or 'None'}"}
            ],
            temperature=0.1,
            format="json",
        )
        if not llm_response.success:
            raise HTTPException(status_code=500, detail=f"LLM error: {llm_response.error}")

        content = llm_response.text

        logger.info(f"LLM returned: {content[:200]}...")

        # 4. Parse JSON
        try:
            plan_data = json.loads(content)

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
            plan_steps = plan_data.get("plan", plan_data.get("steps", plan_data.get("actions", [])))

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
                    log_path = f"C:\\Users\\djoet\\Desktop\\SelfModifyingAgents\\logs\\{service}.log"
                    ns["details"] = f'powershell -Command "Get-Content \'{log_path}\' -Tail 50"'
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
                    ns["details"] = step.get("details") or step.get("file_path") or step.get("filepath") or step.get("path") or step.get("target") or step.get("command") or step.get("query") or ""

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
                if ns["action"] in ("read_file", "write_file", "patch_file") and ns["details"] and not ns["details"].startswith("C:\\"):
                    # Resolve relative path against working_dir or project root
                    base = plan_data.get("initial_context", {}).get("working_dir", "C:\\Users\\djoet\\Desktop\\SelfModifyingAgents")
                    ns["details"] = os.path.join(base, ns["details"])

                # Fix file operation steps that reference wrong user paths.
                # LLMs sometimes hallucinate usernames (e.g. C:\Users\droux\ instead of C:\Users\djoet\).
                if ns["action"] in ("read_file", "write_file", "patch_file", "run_command") and ns.get("details"):
                    detail = ns["details"]

                    # Phase 39: GPT-OSS 20B over-escapes backslashes in JSON output,
                    # producing paths like C:\\Users\\djoet\\Desktop (literal double-\).
                    # Windows cmd.exe rejects those. Collapse repeated backslashes in
                    # local paths (but preserve UNC paths that start with \\).
                    if "\\\\" in detail and not detail.lstrip().startswith("\\\\"):
                        import re as _re_bs
                        # Collapse 2+ consecutive backslashes down to 1, outside UNC prefix
                        fixed = _re_bs.sub(r'\\{2,}', r'\\', detail)
                        if fixed != detail:
                            logger.info(f"PATH FIX (over-escaped): {detail[:80]} -> {fixed[:80]}")
                            ns["details"] = fixed
                            detail = fixed

                    # Fix hallucinated usernames — only C:\Users\djoet is valid
                    import re as _re2
                    wrong_user = _re2.search(r'C:\\Users\\(?!djoet\\)([^\\]+)\\', detail)
                    if wrong_user:
                        fixed = _re2.sub(r'C:\\Users\\[^\\]+\\', r'C:\\Users\\djoet\\', detail)
                        logger.info(f"PATH FIX (wrong user): {detail[:80]} -> {fixed[:80]}")
                        ns["details"] = fixed
                        detail = fixed

                # Fix run_command steps that reference bare filenames with wrong root path.
                # LLMs sometimes guess C:\Users\djoet\filename instead of the correct
                # C:\Users\djoet\Desktop\SelfModifyingAgents\filename.
                if ns["action"] == "run_command" and ns.get("details"):
                    cmd = ns["details"]
                    wrong_root = "C:\\Users\\djoet\\"
                    correct_root = "C:\\Users\\djoet\\Desktop\\SelfModifyingAgents\\"
                    # Replace bare home-dir paths that are NOT Desktop/AppData/etc
                    import re as _re
                    def _fix_path(m):
                        full = m.group(0)
                        # Allow legitimate sub-paths (Desktop, AppData, Documents...)
                        after = full[len(wrong_root):]
                        legit = ("Desktop", "AppData", "Documents", "Downloads", "Pictures", ".")
                        if any(after.startswith(l) for l in legit):
                            return full
                        return correct_root + after
                    fixed_cmd = _re.sub(
                        r'C:\\Users\\djoet\\[^\\\s"\']+',
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
    """Warm the LLM models this service depends on so the first real
    /plan call doesn't pay the cold-start penalty (28s for gpt-oss:20b
    measured Apr 6 morning). Runs as a background task so the service
    comes up healthy immediately."""
    try:
        # Coder uses the "coder" alias (gpt-oss:20b). Warming this also
        # warms informed_chat/coach/researcher since they share the model.
        results = await warm_models(["coder"])
        for alias, (success, elapsed, _err) in results.items():
            if success:
                logger.info(f"🔥 Coder warmup complete: {alias} loaded in {elapsed:.1f}s")
            else:
                logger.warning(f"⚠️ Coder warmup failed: {alias} ({_err})")
    except Exception as e:
        logger.warning(f"Warmup background task crashed (non-fatal): {e}")

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
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
