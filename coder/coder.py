"""
Coder Agent — Task Planning Engine
------------------------------------
Takes a natural-language task and produces a structured execution plan
for the Worker to carry out. Uses a local LLM via Ollama.
"""

import sys
import os
import json
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from shared.lifecycle import register_process
from shared.schemas import AgentPlan, Step, TaskContext
from shared.memory import memory
from shared.codebase_index import codebase_index
from shared.logger import get_logger
from shared.activity import set_thought, set_plan as broadcast_plan
from shared.trace import set_trace_id, HEADER_NAME as TRACE_HEADER
from config import CONFIG

try:
    from shared.skill_extractor import get_skills_for_task, format_skills_for_prompt
    SKILLS_AVAILABLE = True
except ImportError:
    SKILLS_AVAILABLE = False

logger = get_logger("coder")

# --- CONFIGURATION ---
PORT       = CONFIG.PORT_CODER
MODEL_NAME = CONFIG.MODEL_REASON

# Derive paths at runtime — no hardcoded user directories
PROJECT_ROOT = Path(project_root)
LOGS_DIR = PROJECT_ROOT / "logs"

# Detect OS for shell command hints in the system prompt
_IS_WINDOWS = os.name == "nt"
if _IS_WINDOWS:
    _OS_HINT = f"""## OPERATING SYSTEM: WINDOWS
When using run_command:
- Use `dir` NOT `ls`, `type` NOT `cat`, `copy` NOT `cp`, `del` NOT `rm`
- Use backslashes in paths: {PROJECT_ROOT}
- PowerShell: prefix with `powershell -Command "..."`
- To read a log: powershell -Command "Get-Content '{LOGS_DIR}\\\\service.log' -Tail 50"
- To list logs: dir {LOGS_DIR}"""
else:
    _OS_HINT = f"""## OPERATING SYSTEM: LINUX / MACOS
When using run_command:
- Use standard Unix commands: ls, cat, cp, rm, grep
- Paths use forward slashes: {PROJECT_ROOT}
- To read a log: tail -n 50 {LOGS_DIR}/service.log
- To list logs: ls {LOGS_DIR}"""

OLLAMA_CHAT_URL = f"{CONFIG.OLLAMA_HOST}/api/chat"

app = FastAPI(title="RouxYou Coder")

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

class TraceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        trace_id = request.headers.get(TRACE_HEADER, "")
        if trace_id:
            set_trace_id(trace_id)
        response = await call_next(request)
        if trace_id:
            response.headers[TRACE_HEADER] = trace_id
        return response

app.add_middleware(TraceMiddleware)


@app.get("/health")
async def health():
    return {"status": "ok"}


class PlanRequest(BaseModel):
    query: str
    context: Optional[str] = None
    history: List[str] = []


def _check_search_available() -> bool:
    """Quick non-blocking check if web search is configured and reachable."""
    from shared.search import search_available
    return search_available()


def _build_system_prompt(memories: List[Any] = [], query: str = "") -> str:
    """Build the Coder system prompt with injected memories, skills, and architecture."""

    search_online = _check_search_available()
    search_provider = CONFIG.SEARCH_PROVIDER

    codebase_index.refresh_if_stale()
    system_map = codebase_index.get_system_map()

    # Format memories
    memory_text = ""
    if memories:
        memory_text = "## RELEVANT MEMORIES (LEARNED KNOWLEDGE)\n"
        for m in memories:
            memory_text += f"- Past Task: {m.task_query}\n"
            memory_text += f"  Result: {m.plan_summary[:150]}\n"
            memory_text += f"  Location: {m.working_dir}\n"
            if m.code_artifacts:
                memory_text += "  Working Code Pattern:\n"
                for fname, code in list(m.code_artifacts.items())[:1]:
                    if fname.startswith("cmd:"):
                        continue
                    code_preview = code[:400]
                    memory_text += f"    File: {fname}\n```python\n{code_preview}\n```\n"
            memory_text += f"  Utility Score: {getattr(m, 'utility', 'N/A')}\n"

    # Inject relevant skills
    skill_text = ""
    if SKILLS_AVAILABLE and query:
        try:
            skills = get_skills_for_task(query, limit=3)
            if skills:
                skill_text = format_skills_for_prompt(skills)
                logger.info(f"SKILLS: Injected {len(skills)} skill(s)")
        except Exception as e:
            logger.warning(f"Skill retrieval error: {e}")

    if search_online:
        search_status = f"✅ ONLINE (provider: {search_provider}) — web_search is available"
        search_note = ""
    else:
        if search_provider == "none":
            search_status = "DISABLED — web_search.provider is set to 'none' in config.yaml"
        elif search_provider == "duckduckgo":
            search_status = "UNAVAILABLE — duckduckgo-search library not installed"
        else:
            search_status = f"OFFLINE — {search_provider} not reachable"
        search_note = "Solve ALL tasks with local tools only: read_file, run_command, patch_file, write_file."

    return f"""
## YOUR IDENTITY
You are a surgical code editor. You specialize in making the SMALLEST possible change to achieve a goal.
You NEVER rewrite entire files. You always use anchors and delimiters to make precise edits.
You always read a file before editing it so you have exact text to anchor against.

## HALLUCINATION PREVENTION (ANTI-COMPLIANCE RULES)

ANCHOR UNCERTAINTY PROTOCOL:
- If you have NOT seen the exact line in a read_file result from this session, do NOT guess the anchor.
- Instead, output the literal token ANCHOR_UNCERTAIN as the anchor text.
- The pipeline will catch ANCHOR_UNCERTAIN and trigger a read_file before executing the patch.
- A confident wrong anchor silently corrupts code. ANCHOR_UNCERTAIN is always recoverable.
- ANCHOR_UNCERTAIN format: {{"action": "patch_file", "details": "filepath", "content": "ANCHOR_UNCERTAIN\\n===REPLACE===\\nnew code"}}

WRONG PREMISE RESISTANCE:
- If the task provides a file path, function name, or code snippet you cannot verify, do NOT silently adopt it.
- Add "unverified_assumptions": ["<the claim you could not verify>"] to your response.
- Any plan depending on an unverified assumption MUST include a read_file step to confirm it first.
- If a user premise contradicts what you read from the file, trust the file.

UNCERTAINTY IS VALID OUTPUT:
- If you cannot safely accomplish a task, return: {{"success": false, "error": "Insufficient context — <what is needed>"}}
- A plan that admits uncertainty is recoverable. A hallucinated plan that executes is a production incident.

## DEFAULT WORKING DIRECTORY (CRITICAL)
The project root for ALL tasks is: {PROJECT_ROOT}

When a task refers to a file by name only (e.g. "worker.py", "notes.md"),
ALWAYS assume it lives in {PROJECT_ROOT} UNLESS context explicitly says otherwise.

{_OS_HINT}

{memory_text}

{skill_text}

## SYSTEM KNOWLEDGE (YOUR OWN INFRASTRUCTURE)
Log files are at: {LOGS_DIR}
Available logs: coder.log, worker.log, orchestrator.log, watchtower.log,
                gateway.log, deployer.log, memory.log, task_queue.log

Services and ports:
  - Gateway: {CONFIG.PORT_GATEWAY} | Orchestrator: {CONFIG.PORT_ORCHESTRATOR} | Coder: {CONFIG.PORT_CODER}
  - Worker: {CONFIG.PORT_WORKER} | Watchtower: {CONFIG.PORT_WATCHTOWER}

Memory file: {PROJECT_ROOT / "memory.json"}
Task registry: {PROJECT_ROOT / "tasks.json"}

## WEB SEARCH AVAILABILITY
Current status: {search_status}
{search_note}
NEVER use web_search as a fallback for tasks you don't understand.
If a task is unclear, return {{"success": false}} with a clear error message.

## CREDENTIALS (SECURE)
Credentials live in `.env` at project root. NEVER read/write the .env file directly.
In scripts, use: `from dotenv import load_dotenv; load_dotenv(); os.getenv('KEY_NAME')`
Available keys (if configured): HA_TOKEN, REMOTE_SERVER_HOST, REMOTE_SERVER_USER, REMOTE_SERVER_PASSWORD
NEVER print, log, or hardcode credential values.

## AVAILABLE ACTIONS (8 total)

1. **read_file** — Read file contents. ALWAYS do this before patch_file.
   Format: {{"action": "read_file", "details": "/full/path/to/file.py"}}

2. **write_file** — Create NEW files ONLY. Never use on existing files.
   Format: {{"action": "write_file", "details": "/full/path/new_file.py", "content": "full file contents"}}

3. **patch_file** — Surgically edit EXISTING files. This is your primary editing tool.

   PATCH_FILE FORMAT LOCK (MANDATORY — NO EXCEPTIONS)

   The "content" field must have EXACTLY 3 parts:
     PART 1: Anchor text (exact line(s) copied from the file)
     PART 2: Delimiter (===PATCH=== or ===REPLACE===)
     PART 3: New code to insert or substitute

   INSERT example (adds code AFTER the anchor):
     {{"action": "patch_file", "details": "filepath", "content": "app = FastAPI()\\n===PATCH===\\n\\n@app.get(\\"/new\\")\nasync def new():\\n    return {{\\"ok\\": True}}"}}

   REPLACE example (swaps anchor with new code):
     {{"action": "patch_file", "details": "filepath", "content": "PORT = 8001\\n===REPLACE===\\nPORT = 9001"}}

   ENFORCED CONSTRAINTS:
   - Content MUST contain ===PATCH=== or ===REPLACE===
   - Content without a delimiter is REJECTED
   - Full file rewrites in content are REJECTED
   - Anchor text must be EXACT characters from read_file output
   - If anchor text is uncertain, use ANCHOR_UNCERTAIN — do NOT guess

4. **run_command** — Execute shell commands.
   Format: {{"action": "run_command", "details": "python script.py"}}

5. **verify_fix** — Run and verify code works. Must be LAST step.
   Format: {{"action": "verify_fix", "details": "script.py"}}

6. **web_search** — Search the internet.
   Format: {{"action": "web_search", "details": "search query"}}

6b. **rag_query** — Search the system's knowledge base (semantic search).
    Use this to find context about past tasks, conversations, and decisions.
    Format: {{"action": "rag_query", "details": "what you want to know about"}}

7. **restart_service** — Restart a service.
   Format: {{"action": "restart_service", "details": "worker"}}

8. **deploy_patch** — Deploy code changes to system files via blue-green pipeline.
   ⚠️ MANDATORY for ALL system .py files (worker.py, orchestrator.py, coder.py, etc.)
   Direct write_file/patch_file on system files is BLOCKED. You MUST use this action.

   Format is IDENTICAL to patch_file but action="deploy_patch" and details="service_name":
   INSERT: {{"action": "deploy_patch", "details": "worker", "content": "anchor_line\\n===PATCH===\\nnew code"}}
   REPLACE: {{"action": "deploy_patch", "details": "worker", "content": "old line\\n===REPLACE===\\nnew line"}}

   Service names: worker, orchestrator, coder
   IMPORTANT: read_file FIRST to get exact anchor text, then use deploy_patch.

## OUTPUT FORMAT LOCK
You return ONLY valid JSON. No markdown. No explanation. No commentary.
{{
  "success": true,
  "initial_context": {{
    "working_dir": "{PROJECT_ROOT}",
    "active_file": "target.py",
    "detected_errors": []
  }},
  "plan": [
    {{"id": 1, "action": "read_file", "details": "/path/file.py"}},
    {{"id": 2, "action": "patch_file", "details": "/path/file.py", "content": "exact anchor\\n===PATCH===\\nnew code"}},
    {{"id": 3, "action": "verify_fix", "details": "file.py"}}
  ],
  "unverified_assumptions": []
}}

## PLANNING RULES
1. EVERY edit workflow: read_file first, then edit, then verify_fix.
2. EVERY plan ends with verify_fix as the final step.
3. Use patch_file for NORMAL files. write_file ONLY for brand new files.
4. All paths must be absolute.
5. If memories contain Working Code Patterns, adapt that code instead of reinventing.
6. Before patching: do I have exact anchor text from a read_file this session? If no, read first.

## SCOPE & EFFICIENCY RULES
7. NEVER run analysis tools on the ENTIRE project directory. Always target specific files.
8. Keep plans to 6 steps or fewer.
9. run_command has a 60-second timeout. Scope commands to individual files.
10. For broad tasks, pick the 2-3 MOST IMPORTANT files. Do NOT process everything at once.
11. UNCERTAINTY OVER CONFIDENCE: wrong anchor is always worse than ANCHOR_UNCERTAIN.

## ⚠️ CRITICAL: SYSTEM FILE EDITING RULE
System .py files (worker.py, orchestrator.py, coder.py, dashboard.py, schemas.py,
gateway.py, deployer.py, companion.py, etc.) CANNOT be edited with patch_file or write_file.
Use **deploy_patch** for ANY change to system .py files.
Workflow: read_file → deploy_patch (no verify_fix needed, deploy pipeline tests it)

## SYSTEM ARCHITECTURE
{system_map}
"""


@app.post("/plan")
async def generate_plan(request: PlanRequest):
    logger.info(f"Planning: {request.query[:80]}")
    set_thought(f"Planning: {request.query[:60]}...")

    try:
        set_thought("Searching memory for relevant experiences...")
        relevant_memories = memory.retrieve_relevant(request.query)
        if relevant_memories:
            logger.info(f"Found {len(relevant_memories)} relevant memories")

        system_instruction = _build_system_prompt(relevant_memories, query=request.query)

        set_thought("Consulting local LLM for task planning...")
        import aiohttp
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": MODEL_NAME,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": f"Task: {request.query}\nContext: {request.context or 'None'}"}
                ],
                "temperature": 0.1,
                "stream": False,
                "format": "json",
            }

            async with session.post(OLLAMA_CHAT_URL, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=180)) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=500, detail=f"Ollama error: {await resp.text()}")

                result = await resp.json()
                content = result["message"]["content"]

                # Strip thinking tokens if present
                if "<think>" in content:
                    content = content.split("</think>")[-1].strip()

                logger.info(f"LLM returned: {content[:200]}...")

                try:
                    plan_data = json.loads(content)
                except json.JSONDecodeError:
                    logger.error("Failed to parse JSON from LLM")
                    return {"success": False, "error": "Invalid JSON from LLM"}

                if "initial_context" not in plan_data:
                    plan_data["initial_context"] = {}

                # Normalize plan key variants
                plan_steps = plan_data.get("plan",
                             plan_data.get("steps",
                             plan_data.get("actions", [])))

                # Handle bare step object (no plan wrapper)
                if not plan_steps and "action" in plan_data:
                    plan_steps = [plan_data]

                # Action aliases — LLMs invent action names
                ACTION_ALIASES = {
                    "insert_code": "deploy_patch",
                    "add_code": "deploy_patch",
                    "modify_file": "deploy_patch",
                    "edit_file": "patch_file",
                    "update_file": "patch_file",
                    "create_file": "write_file",
                    "search": "web_search",
                    "execute": "run_command",
                    "query_rag": "rag_query",
                    "search_memory": "rag_query",
                    "memory_search": "rag_query",
                    "search_rag": "rag_query",
                    "read_log": "run_command",
                    "read_logs": "run_command",
                    "view_log": "run_command",
                }

                normalized_steps = []
                for i, step in enumerate(plan_steps):
                    raw_action = step.get("action", step.get("type", "unknown"))
                    resolved_action = ACTION_ALIASES.get(raw_action, raw_action)
                    if resolved_action != raw_action:
                        logger.info(f"Action alias: {raw_action} → {resolved_action}")

                    ns = {"id": step.get("id", i + 1), "action": resolved_action}

                    # Auto-build log read commands
                    if raw_action in ("read_log", "read_logs", "view_log"):
                        service = step.get("details", "").strip().lower()
                        log_path = LOGS_DIR / f"{service}.log"
                        if _IS_WINDOWS:
                            ns["details"] = f'powershell -Command "Get-Content \'{log_path}\' -Tail 50"'
                        else:
                            ns["details"] = f"tail -n 50 {log_path}"
                        logger.info(f"Auto-built log command for: {service}")
                    else:
                        # Unwrap nested "parameters" object
                        params = step.get("parameters", {})
                        if isinstance(params, dict):
                            for pk, pv in params.items():
                                if pk not in step:
                                    step[pk] = pv

                        # Extract details from various key names
                        ns["details"] = (
                            step.get("details") or step.get("path") or
                            step.get("file_path") or step.get("target") or
                            step.get("command") or step.get("query") or ""
                        )

                    # Handle deploy_patch service inference
                    if ns["action"] == "deploy_patch":
                        if not ns.get("details") and step.get("content"):
                            try:
                                p = json.loads(step["content"]) if isinstance(step["content"], str) else step["content"]
                                if isinstance(p, list) and p and "file" in p[0]:
                                    fname = Path(p[0]["file"]).name
                                    svc_map = {"worker.py": "worker", "orchestrator.py": "orchestrator", "coder.py": "coder"}
                                    ns["details"] = svc_map.get(fname, "worker")
                            except Exception:
                                pass
                        if not ns.get("details"):
                            ns["details"] = "worker"

                    elif ns["action"] == "patch_file":
                        if "content" in step:
                            ns["content"] = step["content"]
                        elif "anchor_text" in step and "new_code" in step:
                            ns["content"] = step["anchor_text"] + "\n===PATCH===\n" + step["new_code"]
                        elif "find_text" in step and "replace_text" in step:
                            ns["content"] = step["find_text"] + "\n===REPLACE===\n" + step["replace_text"]
                        elif "old_text" in step and "new_text" in step:
                            ns["content"] = step["old_text"] + "\n===REPLACE===\n" + step["new_text"]
                        else:
                            ns["content"] = step.get("content", "")
                    elif "content" in step:
                        ns["content"] = step["content"]

                    normalized_steps.append(ns)

                plan_data["plan"] = normalized_steps
                plan_data["success"] = plan_data.get("success", True)

                if normalized_steps:
                    step_descriptions = [
                        f"{s.get('action', '?')}: {str(s.get('details', ''))[:40]}..."
                        for s in normalized_steps[:5]
                    ]
                    broadcast_plan(step_descriptions)
                    set_thought(f"Plan ready: {len(normalized_steps)} steps.")

                return plan_data

    except Exception as e:
        logger.error(f"Planning failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class DiagnoseRequest(BaseModel):
    prompt: str


@app.post("/diagnose")
async def diagnose_error(request: DiagnoseRequest):
    """Direct LLM call for quick error diagnosis — no planning, just text response."""
    logger.info("Diagnosing error...")
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": CONFIG.MODEL_ROUTER,  # Use fast model for diagnosis
                "prompt": request.prompt,
                "stream": False,
            }
            async with session.post(
                f"{CONFIG.OLLAMA_HOST}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    return {"success": False, "response": "Ollama error"}
                result = await resp.json()
                response_text = result.get("response", "")
                if "<think>" in response_text:
                    response_text = response_text.split("</think>")[-1].strip()
                return {"success": True, "response": response_text}
    except Exception as e:
        logger.error(f"Diagnosis failed: {e}")
        return {"success": False, "response": str(e)}


@app.on_event("startup")
async def startup_event():
    register_process("coder")
    logger.info(f"Coder initialized on port {PORT} using {MODEL_NAME}")
    logger.info(f"Codebase index: {len(codebase_index.files)} modules mapped")
    logger.info(f"Search provider: {CONFIG.SEARCH_PROVIDER}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
