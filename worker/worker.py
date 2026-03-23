"""
Super Worker Agent — Execution Engine
Responsibilities: File Ops, Command Execution, Web Search,
                  Vision, Verification, Blue-Green Deploy Pipeline.
"""

from worker.capabilities.vision import capture_screen_analysis
from worker.capabilities.verify import run_verification
import sys
import os
import json
import asyncio
import re
import requests
from pathlib import Path
from typing import Dict, List, Any
from dotenv import load_dotenv

# Load .env (HA_TOKEN etc.)
_PROJECT_ROOT = Path(__file__).parent
load_dotenv(_PROJECT_ROOT / ".env")

sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI, Body
import uvicorn

from shared.schemas import ExecutionResult, Step, TaskContext
from shared.lifecycle import register_process
from shared.logger import get_logger
from shared.activity import set_thought, set_step, set_active_task
from shared.search import web_search
from shared.trace import set_trace_id, HEADER_NAME as TRACE_HEADER
from config import CONFIG

logger = get_logger("worker")

# --- CONFIGURATION (all from CONFIG) ---
HA_URL   = CONFIG.HA_URL
HA_TOKEN = CONFIG.HA_TOKEN or os.getenv("HA_TOKEN", "")

WATCHTOWER_URL = f"http://localhost:{CONFIG.PORT_WATCHTOWER}"
RAG_API_URL    = f"http://localhost:{CONFIG.PORT_RAG}"

# Safe paths: project root and parent (no personal paths hardcoded)
SAFE_PATHS = [
    _PROJECT_ROOT.resolve(),
    _PROJECT_ROOT.parent.resolve(),
]

# --- SECURITY ---
SENSITIVE_FILES = {".env", ".env.local", ".env.production", ".env.secret"}

from shared.redact import redact as redact_credentials, redact_dict


class SuperWorker:
    """The Hands of the System"""

    def __init__(self):
        self.logger = logger
        self.logger.info("Super Worker initializing...")

    def _validate_path(self, file_path: str) -> Path:
        if not os.path.isabs(file_path):
            target = Path(os.getcwd()) / file_path
        else:
            target = Path(file_path)
        return target.resolve()

    # Files that can NEVER be modified by the agent
    IMMUTABLE_FILES = {"watchtower.py", "lifecycle.py"}

    # System files that MUST go through the deploy pipeline
    DEPLOY_ONLY_FILES = {
        "worker.py", "orchestrator.py", "coder.py", "gateway.py",
        "dashboard.py", "companion.py", "memory_agent.py",
        "deployer.py", "schemas.py", "logger.py", "activity.py",
        "task_registry.py", "codebase_index.py", "infrastructure_monitor.py",
        "registry.py",
    }

    FILE_TO_SERVICE = {
        "worker.py": "worker",
        "orchestrator.py": "orchestrator",
        "coder.py": "coder",
        "companion.py": "orchestrator",
        "registry.py": "coder",
        "deployer.py": "worker",
        "schemas.py": "worker",
        "logger.py": "worker",
        "activity.py": "worker",
        "task_registry.py": "orchestrator",
        "codebase_index.py": "coder",
        "infrastructure_monitor.py": "orchestrator",
        "dashboard.py": "orchestrator",
        "gateway.py": "gateway",
        "memory_agent.py": "worker",
    }

    async def execute_step(self, step: Step, context: TaskContext) -> Dict[str, Any]:
        self.logger.info(f"Step {step.id}: {step.action} [{redact_credentials(step.details[:50])}...]")

        try:
            # --- IMMUTABLE FILE CHECK ---
            if step.action in ("write_file", "patch_file"):
                target_name = Path(step.details.split('\n')[0]).name if step.details else ""
                if target_name in self.IMMUTABLE_FILES:
                    return {
                        "success": False,
                        "error": f"IMMUTABLE FILE: {target_name} cannot be modified. "
                                 "This file is part of the supervisor layer."
                    }

            # --- DEPLOY GATE ---
            if step.action in ("write_file", "patch_file"):
                target_path = step.details.split('\n')[0] if step.details else ""
                target_name = Path(target_path).name
                if target_name in self.DEPLOY_ONLY_FILES:
                    service = self.FILE_TO_SERVICE.get(target_name, "worker")
                    return {
                        "success": False,
                        "error": f"DEPLOY REQUIRED: {target_name} is a system file. "
                                 f"Use action 'deploy_patch' with details='{service}'. "
                                 "Direct editing is blocked."
                    }

            # --- DEPLOY PATCH ---
            if step.action == "deploy_patch":
                service_name = step.details.strip().lower()
                patch_content = step.content or ""

                self.logger.info(f"DEPLOY_PATCH: Routing change to {service_name} via deploy pipeline")

                from shared.deployer import SERVICE_SOURCES
                config = SERVICE_SOURCES.get(service_name, {})
                entry = config.get("entry_script", f"{service_name}.py")

                patches = None

                if "===REPLACE===" in patch_content:
                    parts = patch_content.split("===REPLACE===", 1)
                    patches = [{"file": entry, "find": parts[0].strip(), "replace": parts[1].strip()}]
                elif "===PATCH===" in patch_content:
                    parts = patch_content.split("===PATCH===", 1)
                    find_text = parts[0].strip()
                    patches = [{"file": entry, "find": find_text, "replace": find_text + "\n" + parts[1].strip()}]
                else:
                    try:
                        patches = json.loads(patch_content) if isinstance(patch_content, str) else patch_content
                        if not isinstance(patches, list):
                            patches = [patches]
                    except (json.JSONDecodeError, TypeError):
                        return {"success": False, "error": "deploy_patch content must use ===PATCH=== or ===REPLACE=== format."}

                try:
                    loop = asyncio.get_running_loop()
                    def do_deploy():
                        resp = requests.post(
                            f"{WATCHTOWER_URL}/deploy/{service_name}",
                            json={"patches": patches}, timeout=30
                        )
                        return resp.json()
                    result = await loop.run_in_executor(None, do_deploy)
                    if result.get("success"):
                        return {
                            "success": True,
                            "output": f"Deploy staged for {service_name}: {result.get('deploy_id')}. "
                                      "Health check passed. Awaiting human approval in dashboard.",
                            "deploy_id": result.get("deploy_id"),
                        }
                    return {"success": False, "error": f"Deploy failed: {result.get('error')}"}
                except requests.exceptions.ConnectionError:
                    return {"success": False, "error": f"Cannot reach Watchtower at {WATCHTOWER_URL}. Is it running?"}
                except Exception as e:
                    return {"success": False, "error": f"Deploy error: {e}"}

            # --- FILE OPERATIONS ---
            if step.action == "write_file":
                if hasattr(step, 'content') and step.content:
                    fname, content = step.details, step.content
                elif "\n" in step.details:
                    fname, content = step.details.split("\n", 1)
                else:
                    fname, content = step.details, ""

                path = self._validate_path(fname)

                # Write protection
                if path.exists():
                    existing_size = path.stat().st_size
                    new_size = len(content.encode('utf-8'))
                    if existing_size > 500 and new_size < existing_size * 0.5:
                        return {
                            "success": False,
                            "error": f"WRITE PROTECTION: Refusing to overwrite {path.name} — "
                                     f"new content is {new_size*100//existing_size}% the size of existing. "
                                     "Read the file first and patch, don't rewrite."
                        }

                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                return {"success": True, "path": str(path), "bytes": len(content)}

            elif step.action == "patch_file":
                path = self._validate_path(step.details)
                if not path.exists():
                    return {"success": False, "error": f"File not found: {path}"}

                patch_content = step.content or ""

                # H-NEURON: ANCHOR_UNCERTAIN intercept
                if patch_content.startswith("ANCHOR_UNCERTAIN"):
                    try:
                        file_content = path.read_text(encoding='utf-8')
                        return {
                            "success": False,
                            "error_code": "ANCHOR_UNCERTAIN",
                            "error": (
                                f"ANCHOR_UNCERTAIN: Coder did not have verified anchor text for {path.name}. "
                                f"Re-plan using the exact text below.\n\nFILE CONTENT ({path.name}):\n{file_content}"
                            )
                        }
                    except Exception as read_err:
                        return {"success": False, "error_code": "ANCHOR_UNCERTAIN",
                                "error": f"ANCHOR_UNCERTAIN: Could not auto-read {path.name}: {read_err}"}

                original = path.read_text(encoding='utf-8')

                # Normalize delimiters
                import re as _re
                patch_content = _re.sub(r'={2,}\s*REPLACE\s*={2,}', '===REPLACE===', patch_content, flags=_re.IGNORECASE)
                patch_content = _re.sub(r'={2,}\s*PATCH\s*={2,}', '===PATCH===', patch_content, flags=_re.IGNORECASE)
                patch_content = _re.sub(r'-{2,}\s*REPLACE\s*-{2,}', '===REPLACE===', patch_content, flags=_re.IGNORECASE)
                patch_content = _re.sub(r'-{2,}\s*PATCH\s*-{2,}', '===PATCH===', patch_content, flags=_re.IGNORECASE)

                def find_anchor(anchor: str, source: str):
                    if anchor in source: return anchor
                    stripped = anchor.strip()
                    if stripped and stripped in source: return stripped
                    collapsed = ' '.join(anchor.split())
                    for line in source.splitlines():
                        if ' '.join(line.split()) == collapsed: return line
                    anchor_lines = [l for l in anchor.strip().splitlines() if l.strip()]
                    if len(anchor_lines) >= 2:
                        last_line = anchor_lines[-1].strip()
                        if last_line and last_line in source and source.count(last_line) == 1:
                            return last_line
                    stripped_anchor = anchor.strip()
                    if stripped_anchor and len(stripped_anchor) >= 10:
                        anchor_id = stripped_anchor.split('=')[0].strip() if '=' in stripped_anchor else stripped_anchor.split('(')[0].strip()
                        if anchor_id and len(anchor_id) >= 3:
                            id_matches = [line for line in source.splitlines()
                                          if line.strip().startswith(anchor_id) and ('=' in line or '(' in line)]
                            if len(id_matches) == 1:
                                return id_matches[0]
                    return None

                patched = None
                patch_mode = None

                if "===REPLACE===" in patch_content:
                    find_text, new_text = patch_content.split("===REPLACE===", 1)
                    find_text, new_text = find_text.strip(), new_text.strip()
                    actual = find_anchor(find_text, original)
                    if actual is None:
                        return {"success": False, "error": f"FIND text not found in {path.name}. Read the file first."}
                    if original.count(actual) > 1:
                        return {"success": False, "error": f"FIND text appears {original.count(actual)} times. Be more specific."}
                    patched = original.replace(actual, new_text, 1)
                    patch_mode = "replaced"

                elif "===PATCH===" in patch_content:
                    find_line, new_code = patch_content.split("===PATCH===", 1)
                    find_line = find_line.strip()
                    actual = find_anchor(find_line, original)
                    if actual is None:
                        return {"success": False, "error": f"Anchor line not found in {path.name}. Read the file first."}
                    patched = original.replace(actual, actual + "\n" + new_code.rstrip(), 1)
                    patch_mode = "inserted"

                else:
                    if len(patch_content) > 200 and ('import ' in patch_content or 'def ' in patch_content):
                        return {"success": False, "error": "WRONG FORMAT: Use ===PATCH=== or ===REPLACE=== delimiter in content."}
                    return {"success": False, "error": "Invalid patch format. Content must contain ===PATCH=== or ===REPLACE===."}

                path.write_text(patched, encoding='utf-8')

                if path.suffix == '.py':
                    import ast
                    try:
                        ast.parse(patched)
                    except SyntaxError as e:
                        path.write_text(original, encoding='utf-8')
                        return {
                            "success": False,
                            "error": f"PATCH ROLLED BACK: Syntax error at line {e.lineno}: {e.msg}. Original restored."
                        }

                return {"success": True, "path": str(path), "action": patch_mode, "bytes": len(patched)}

            elif step.action == "read_file":
                path = self._validate_path(step.details)
                if not path.exists():
                    return {"success": False, "error": "File not found"}
                if path.name.lower() in SENSITIVE_FILES:
                    return {"success": True, "content": f"[PROTECTED] {path.name} is loaded via environment variables. Use os.getenv() — do NOT read this file directly."}
                for enc in ('utf-8', 'utf-16', 'latin-1'):
                    try:
                        with open(path, 'r', encoding=enc) as f:
                            return {"success": True, "content": f.read()}
                    except (UnicodeDecodeError, UnicodeError):
                        continue
                return {"success": False, "error": f"Cannot decode {path.name}"}

            # --- RAG QUERY ---
            elif step.action == "rag_query":
                query_text = step.details.strip()
                try:
                    loop = asyncio.get_running_loop()
                    def do_rag():
                        resp = requests.post(f"{RAG_API_URL}/query",
                                             json={"query": query_text, "k": 5}, timeout=10)
                        resp.raise_for_status()
                        return resp.json()
                    data = await loop.run_in_executor(None, do_rag)
                    results = data.get("results", [])
                    if results:
                        parts = [f"RAG returned {len(results)} results:"]
                        for r in results:
                            parts.append(f"  [{r.get('similarity', 0):.0%}] {r.get('text', '')[:300]}")
                        return {"success": True, "output": "\n".join(parts), "results": results}
                    return {"success": True, "output": "No relevant memories found.", "results": []}
                except requests.exceptions.ConnectionError:
                    return {"success": False, "error": f"RAG API not available at {RAG_API_URL}"}
                except Exception as e:
                    return {"success": False, "error": f"RAG query failed: {e}"}

            # --- WEB SEARCH ---
            elif step.action in ("search_web", "web_search"):
                results = await web_search(step.details)
                if not results:
                    from shared.search import search_available, CONFIG as _cfg
                    provider = _cfg.SEARCH_PROVIDER
                    if provider == "none":
                        return {"success": False, "error": "Web search is disabled (search.provider=none in config.yaml)"}
                    elif provider == "duckduckgo":
                        return {"success": False, "error": "Web search unavailable. Install: pip install duckduckgo-search"}
                    else:
                        return {"success": False, "error": f"Web search returned no results (provider: {provider})"}
                return {"success": True, "results": results}

            # --- HOME ASSISTANT ---
            elif step.action == "ha_control":
                if not HA_TOKEN:
                    return {"success": False, "error": "HA_TOKEN not set in .env"}
                if not HA_URL:
                    return {"success": False, "error": "home_assistant.url not set in config.yaml"}

                ha_headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
                parts = step.details.strip().split(None, 1)
                action = parts[0] if parts else ""
                entity_id = parts[1] if len(parts) > 1 else ""

                if action == "list_entities":
                    resp = requests.get(f"{HA_URL}/api/states", headers=ha_headers, timeout=10)
                    if resp.status_code == 200:
                        entities = resp.json()
                        if entity_id:
                            entities = [e for e in entities if e["entity_id"].startswith(entity_id)]
                        summary = "\n".join([f"{e['entity_id']}: {e['state']}" for e in entities[:50]])
                        return {"success": True, "output": summary}
                    return {"success": False, "error": f"HA API error: {resp.status_code}"}

                elif action == "get_state":
                    resp = requests.get(f"{HA_URL}/api/states/{entity_id}", headers=ha_headers, timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        return {"success": True, "output": f"{entity_id}: {data['state']}"}
                    return {"success": False, "error": f"Entity not found: {entity_id}"}

                elif action in ("turn_on", "turn_off", "toggle"):
                    domain = entity_id.split(".")[0] if "." in entity_id else "homeassistant"
                    resp = requests.post(f"{HA_URL}/api/services/{domain}/{action}",
                                         headers=ha_headers, json={"entity_id": entity_id}, timeout=10)
                    if resp.status_code == 200:
                        return {"success": True, "output": f"{action} -> {entity_id}: OK"}
                    return {"success": False, "error": f"HA service call failed: {resp.status_code}"}

                return {"success": False, "error": f"Unknown HA action: {action}. Use turn_on/turn_off/toggle/get_state/list_entities"}

            # --- SYSTEM COMMANDS ---
            elif step.action == "run_command":
                import subprocess
                work_dir = (Path(context.working_dir)
                            if context.working_dir and Path(context.working_dir).exists()
                            else _PROJECT_ROOT)
                result = subprocess.run(step.details, shell=True, capture_output=True,
                                        text=True, timeout=60, cwd=str(work_dir))
                output = result.stdout + (f"\nSTDERR: {result.stderr}" if result.stderr else "")
                output = redact_credentials(output)
                if result.returncode == 0:
                    return {"success": True, "output": output}
                error_msg = redact_credentials(result.stderr if result.stderr else result.stdout)
                return {"success": False, "output": output, "error": error_msg}

            # --- SERVICE MANAGEMENT ---
            elif step.action == "restart_service":
                service_name = step.details.strip().lower()

                if service_name == "worker":
                    loop = asyncio.get_running_loop()
                    def fire_restart():
                        import time
                        time.sleep(3)
                        try:
                            requests.post(f"{WATCHTOWER_URL}/restart/{service_name}", timeout=2)
                        except:
                            pass
                    loop.run_in_executor(None, fire_restart)
                    return {"success": True, "output": f"{service_name} self-restart triggered. Service will restart momentarily."}

                try:
                    loop = asyncio.get_running_loop()
                    def do_restart():
                        resp = requests.post(f"{WATCHTOWER_URL}/restart/{service_name}", timeout=30)
                        return resp.json()
                    result = await loop.run_in_executor(None, do_restart)
                    if result.get("success"):
                        return {"success": True, "output": f"{service_name} restarted and healthy"}
                    return {"success": False, "error": f"Restart failed: {result.get('error')}"}
                except requests.exceptions.ConnectionError:
                    return {"success": False, "error": f"Cannot reach Watchtower at {WATCHTOWER_URL}"}
                except Exception as e:
                    return {"success": False, "error": f"Restart error: {e}"}

            # --- ADVANCED CAPABILITIES ---
            elif step.action == "vision_analysis":
                return capture_screen_analysis(step.details)

            elif step.action == "verify_fix":
                return run_verification(step.details, working_dir=context.working_dir)

            else:
                return {"success": False, "error": f"Unknown action: {step.action}"}

        except Exception as e:
            self.logger.error(f"Step {step.id} failed: {e}")
            return {"success": False, "error": str(e)}

    async def execute_plan(self, data: Dict) -> Dict:
        raw_plan = data.get("plan", [])
        if isinstance(raw_plan, dict) and "steps" in raw_plan:
            raw_plan = raw_plan["steps"]

        context_data = data.get("initial_context")
        current_context = TaskContext(**context_data) if context_data else TaskContext()

        # Per-step rate limiting
        MAX_STEPS_PER_TASK = CONFIG.MAX_STEPS_PER_TASK if hasattr(CONFIG, 'MAX_STEPS_PER_TASK') else 20
        MAX_COMMANDS_PER_TASK = CONFIG.MAX_COMMANDS_PER_TASK if hasattr(CONFIG, 'MAX_COMMANDS_PER_TASK') else 10

        if len(raw_plan) > MAX_STEPS_PER_TASK:
            self.logger.warning(f"Plan has {len(raw_plan)} steps, capping at {MAX_STEPS_PER_TASK}")
            raw_plan = raw_plan[:MAX_STEPS_PER_TASK]

        results = []
        success_count = 0
        last_error = None
        command_count = 0
        total_steps = len(raw_plan)
        _read_file_cache = {}

        set_thought(f"Worker executing plan with {total_steps} steps...")

        for idx, step_dict in enumerate(raw_plan):
            try:
                step = Step(**step_dict) if isinstance(step_dict, dict) else step_dict
                step_num = idx + 1
                action_desc = f"{step.action}: {redact_credentials(step.details[:50])}..."
                set_step(step_num, f"Step {step_num}/{total_steps}: {action_desc}")

                # Anchor auto-correction using cached file content
                if step.action in ("deploy_patch", "patch_file") and hasattr(step, 'content') and step.content:
                    patch_content = step.content
                    for delim in ("===REPLACE===", "===PATCH==="):
                        if delim in patch_content:
                            anchor_text = patch_content.split(delim, 1)[0].strip()
                            cached_content = None
                            if step.action == "deploy_patch":
                                svc = step.details.strip().lower()
                                svc_files = {"worker": "worker.py", "orchestrator": "orchestrator.py", "coder": "coder.py"}
                                cached_content = _read_file_cache.get(svc_files.get(svc, f"{svc}.py"))
                            else:
                                cached_content = _read_file_cache.get(step.details) or _read_file_cache.get(Path(step.details).name)

                            if cached_content and anchor_text and anchor_text not in cached_content:
                                # Try quote normalization
                                alt = anchor_text.replace("'", '"') if "'" in anchor_text else anchor_text.replace('"', "'")
                                if alt in cached_content:
                                    step = Step(id=step.id, action=step.action, details=step.details,
                                                content=patch_content.replace(anchor_text, alt, 1))
                                    self.logger.info("Anchor auto-corrected: quote normalization")
                            break

                # Per-task command rate limit
                if step.action == "run_command":
                    command_count += 1
                    if command_count > MAX_COMMANDS_PER_TASK:
                        result = {
                            "success": False,
                            "error": f"RATE LIMIT: Task exceeded {MAX_COMMANDS_PER_TASK} shell commands. "
                                     "Break this into smaller tasks."
                        }
                        results.append({"step": step.id, "action": step.action, "result": redact_dict(result)})
                        last_error = result["error"]
                        break

                result = await self.execute_step(step, current_context)

                if step.action == "read_file" and result.get("success") and result.get("content"):
                    _read_file_cache[step.details] = result["content"]
                    _read_file_cache[Path(step.details).name] = result["content"]

                if step.action == "verify_fix" and result.get("success"):
                    current_context.verified = True

                results.append({"step": step.id, "action": step.action, "result": redact_dict(result)})

                if result.get("success"):
                    success_count += 1
                else:
                    last_error = redact_credentials(result.get("error") or result.get("output") or "Step failed")
                    if step.action != "verify_fix":
                        break

            except Exception as e:
                self.logger.error(f"Execution Error: {e}")
                last_error = str(e)
                results.append({"error": f"Invalid step data: {step_dict}"})
                break

        critical_steps = [r for r in results if r.get("action") != "verify_fix"]
        is_success = (sum(1 for r in critical_steps if r.get("result", {}).get("success")) == len(critical_steps)) and len(critical_steps) > 0

        summary_parts = [f"Executed {success_count}/{len(raw_plan)} steps."]
        for r in results:
            res = r.get("result", {})
            if res.get("output"):
                summary_parts.append(res["output"][:2000])
            elif res.get("content"):
                summary_parts.append(res["content"][:2000])
            elif res.get("results"):
                for sr in res["results"][:5]:
                    summary_parts.append(f"- {sr.get('title', '')}: {sr.get('snippet', sr.get('body', ''))[:200]}")
            elif res.get("path"):
                summary_parts.append(f"Saved to: {res['path']}")

        full_summary = redact_credentials("\n".join(summary_parts)[:3000])

        exec_result = ExecutionResult(
            success=is_success,
            summary=full_summary,
            results=results,
            error=redact_credentials(last_error) if last_error else None,
            final_context=current_context
        ).model_dump()

        # Escrow result if orchestrator was restarted
        restarted_orch = any(
            s.get("action") == "restart_service" and "orchestrator" in str(s.get("details", "")).lower()
            for s in raw_plan
        )
        if restarted_orch:
            try:
                requests.post(f"{WATCHTOWER_URL}/task_result", json=exec_result, timeout=5)
            except Exception as e:
                self.logger.error(f"ESCROW: Failed to deposit result: {e}")

        return exec_result


# --- APP ---
import time as _time
_start_time = _time.time()
PORT = CONFIG.PORT_WORKER

app = FastAPI(title="RouxYou Worker")

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
worker = SuperWorker()


@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/health_detail")
async def health_detail():
    return {
        "status": "ok",
        "uptime_seconds": round(_time.time() - _start_time, 1),
        "capabilities_count": 9,
    }

@app.get("/capabilities")
async def get_capabilities():
    return {"capabilities": [
        "read_file", "write_file", "patch_file", "run_command",
        "verify_fix", "web_search", "rag_query", "restart_service", "deploy_patch"
    ]}

@app.get("/info")
async def info():
    return {"service_name": "worker"}

@app.get("/uptime")
async def uptime():
    return {"uptime_seconds": round(_time.time() - _start_time, 1)}

@app.post("/execute")
async def handle_message(msg: Dict = Body(...)):
    data = msg.get("data", msg)
    task = msg.get("task", "execute_plan")
    if task == "execute_plan":
        return await worker.execute_plan(data)
    elif task == "search_web":
        results = await web_search(data.get("query", ""))
        summary = "\n".join([f"- {r['title']}: {r.get('snippet', '')}" for r in results])
        return ExecutionResult(success=bool(results), summary=summary[:2000], results=results).model_dump()
    return {"success": False, "error": "Unknown task"}


if __name__ == "__main__":
    register_process("worker")
    logger.info(f"Starting Worker on port {PORT}...")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
