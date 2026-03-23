"""
Orchestrator Agent — Task Queue Edition
----------------------------------------
Central brain of RouxYou. Routes intents, manages the task queue,
coordinates Coder + Worker, and provides real-time state to the dashboard.
"""

import sys
import os
import asyncio
import aiohttp
import time as _time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from shared.lifecycle import register_process
from shared.memory import memory
from shared.schemas import TaskContext
from shared.logger import get_logger
from shared.activity import set_thought, set_status, complete_task, clear_activity
from shared.task_queue import TaskQueue, TaskPriority, TaskState, QueuedTask
from shared.companion import (
    classify_intent, synthesize_response,
    generate_chat_response, generate_informed_chat_response,
    format_confirmation_request as generate_confirmation_prompt,
    format_clarification_request as generate_clarification_prompt,
    add_user_message, add_assistant_message, get_recent_messages,
)
from shared.blackbox import log_event as _bb_log
from shared.roux_client import roux as _roux
from shared.redact import redact as _redact
from shared.proposal_handler import (
    ProposalSubmission, handle_proposal, finalize_proposal,
    proposal_task_map as _proposal_task_map,
)
from shared.trace import set_trace_id, HEADER_NAME as TRACE_HEADER
from config import CONFIG

# --- CONFIGURATION ---
PORT             = CONFIG.PORT_ORCHESTRATOR
GATEWAY_URL      = f"http://localhost:{CONFIG.PORT_GATEWAY}"
CODER_URL        = f"{GATEWAY_URL}/coder/plan"
WORKER_URL       = f"{GATEWAY_URL}/worker/execute"
WATCHTOWER_URL   = f"http://localhost:{CONFIG.PORT_WATCHTOWER}"
WATCHTOWER_CRON_URL = f"http://localhost:{CONFIG.PORT_WATCHTOWER_CRON}"
MAX_RETRIES = 2

RETRYABLE_ERRORS = [
    "Anchor line not found", "FIND text not found", "FIND text appears",
    "Invalid patch format", "PATCH ROLLED BACK",
]

logger = get_logger("orchestrator")
_start_time = _time.time()
_request_count = 0
task_queue = TaskQueue()
app = FastAPI(title="RouxYou Orchestrator")

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


# =====================================================
#  QUEUE ENDPOINTS
# =====================================================

@app.get("/queue")
async def get_queue():
    return task_queue.get_queue_state()

@app.get("/queue/history")
async def get_queue_history(limit: int = 50, offset: int = 0, include_archived: bool = False):
    tasks = task_queue.get_full_history(limit=limit, offset=offset, include_archived=include_archived)
    return {"tasks": tasks, "count": len(tasks), "offset": offset}

@app.get("/queue/{task_id}")
async def get_queue_task(task_id: str):
    task = task_queue.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task

@app.delete("/queue/{task_id}")
async def cancel_queue_task(task_id: str):
    success = task_queue.cancel(task_id)
    if not success:
        raise HTTPException(status_code=400, detail=f"Task {task_id} not found or already running")
    return {"cancelled": True, "task_id": task_id}

@app.post("/queue/abort")
async def abort_running_task():
    task_id = task_queue.cancel_running()
    if not task_id:
        raise HTTPException(status_code=400, detail="No task currently running")
    return {"aborted": True, "task_id": task_id}

@app.post("/queue/pause")
async def pause_queue():
    task_queue.pause()
    return {"paused": True}

@app.post("/queue/resume")
async def resume_queue():
    task_queue.resume()
    return {"paused": False}

@app.post("/queue/{task_id}/archive")
async def archive_task(task_id: str):
    success = task_queue.archive_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found in history")
    return {"archived": True, "task_id": task_id}

@app.post("/queue/{task_id}/unarchive")
async def unarchive_task(task_id: str):
    success = task_queue.unarchive_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found in history")
    return {"archived": False, "task_id": task_id}

@app.post("/queue/archive-all")
async def archive_all_tasks():
    count = task_queue.archive_all()
    return {"archived_count": count}


# =====================================================
#  PROPOSAL EXECUTION (delegated to shared.proposal_handler)
# =====================================================

@app.post("/queue/proposal")
async def submit_proposal(submission: ProposalSubmission):
    return await handle_proposal(submission, task_queue, WATCHTOWER_CRON_URL)


# =====================================================
#  STANDARD ENDPOINTS
# =====================================================

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/metrics")
async def metrics():
    qs = task_queue.get_queue_state()["stats"]
    return {
        "uptime_seconds": round(_time.time() - _start_time, 1),
        "request_count": _request_count,
        "queue_pending": qs["pending_count"],
        "queue_completed": qs["total_completed"],
        "queue_failed": qs["total_failed"],
    }


# =====================================================
#  CORE EXECUTION LOGIC
# =====================================================

class UserQuery(BaseModel):
    query: str

def _is_retryable(error_msg: str) -> bool:
    return bool(error_msg) and any(p in error_msg for p in RETRYABLE_ERRORS)

_REQUIRES_READ_FIRST = {"patch_file", "edit_file", "append_to_file", "patch"}

def _ensure_read_before_write(plan: list) -> list:
    result = list(plan)
    i = 0
    while i < len(result):
        step = result[i]
        action = (step.get("action") or "").lower()
        if action in _REQUIRES_READ_FIRST:
            path = step.get("path") or step.get("file_path") or step.get("target") or ""
            if path:
                already_read = any(
                    (s.get("action") or "").lower() == "read_file" and
                    (s.get("path") == path or s.get("file_path") == path or s.get("target") == path)
                    for s in result[:i]
                )
                if not already_read:
                    result.insert(i, {"action": "read_file", "path": path, "_injected": True})
                    i += 1
        i += 1
    return result


async def _dispatch_to_worker(plan_data: dict, query: str) -> dict:
    """Send an execution plan to the Worker via the Gateway."""
    message = {
        "task": "execute_plan",
        "data": {
            "plan": plan_data.get("plan", []),
            "initial_context": plan_data.get("initial_context", {"working_dir": "."}),
            "query": query,
        }
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                WORKER_URL, json=message,
                timeout=aiohttp.ClientTimeout(total=300)
            ) as resp:
                return await resp.json()
    except aiohttp.ClientConnectorError:
        return {"success": False, "error": "Worker unreachable"}
    except asyncio.TimeoutError:
        return {"success": False, "error": "Worker timeout (300s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _dispatch_to_coder(query: str, context: str = "", history: list = None) -> dict:
    """Send a planning request to the Coder via the Gateway."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                CODER_URL,
                json={"query": query, "context": context, "history": history or []},
                timeout=aiohttp.ClientTimeout(total=180)
            ) as resp:
                return await resp.json()
    except aiohttp.ClientConnectorError:
        return {"success": False, "error": "Coder unreachable"}
    except asyncio.TimeoutError:
        return {"success": False, "error": "Coder timeout (180s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _execute_query(query: str, intent: str = "execute_explain") -> dict:
    """
    Core execution pipeline: Coder plans → Worker executes.
    Handles retries on anchor-related failures.
    """
    set_thought(f"Planning: {query[:60]}...")

    memories = memory.retrieve_relevant(query, limit=3)
    memory_context = ""
    if memories:
        memory_context = "\n".join([
            f"Past task: {m.task_query[:80]} | Result: {m.plan_summary[:100]}"
            for m in memories
        ])

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            logger.info(f"RETRY {attempt}/{MAX_RETRIES}: {last_error[:80] if last_error else '?'}")
            set_thought(f"Retrying (attempt {attempt+1})...")
            retry_context = f"{memory_context}\n\nPREVIOUS ATTEMPT FAILED: {last_error}"
            plan_data = await _dispatch_to_coder(query, retry_context)
        else:
            plan_data = await _dispatch_to_coder(query, memory_context)

        if not plan_data.get("success"):
            return {"success": False, "error": plan_data.get("error", "Coder failed")}

        plan = plan_data.get("plan", [])
        if isinstance(plan, dict) and "steps" in plan:
            plan = plan["steps"]

        plan = _ensure_read_before_write(plan)
        plan_data["plan"] = plan

        set_thought(f"Executing {len(plan)} steps...")
        result = await _dispatch_to_worker(plan_data, query)

        if result.get("success"):
            summary_text = result.get("summary", "")[:500]
            memory.save_episode(
                task=query, plan_summary=summary_text,
                context=TaskContext(), success=True,
                plan_steps=plan, execution_results=result.get("results", []),
            )
            # Auto-extract skills from successful episodes
            try:
                from shared.skill_extractor import extract_skill_from_episode, add_skill
                # Build code artifacts from results
                artifacts = {}
                for step, res in zip(plan, result.get("results", [])):
                    r = res.get("result", {}) if isinstance(res, dict) else {}
                    s = step if isinstance(step, dict) else {}
                    if s.get("action") in ("write_file", "patch_file") and r.get("success"):
                        artifacts[s.get("details", "unknown")] = s.get("content", "")
                skill = extract_skill_from_episode(
                    task_query=query, plan_summary=summary_text,
                    code_artifacts=artifacts if artifacts else None,
                )
                if skill:
                    add_skill(skill)
                    logger.info(f"SKILL: Extracted '{skill['name']}' from successful task")
            except Exception:
                pass  # Skill extraction is optional
            return result

        error_msg = _redact(result.get("error", "") or result.get("summary", ""))
        if _is_retryable(error_msg) and attempt < MAX_RETRIES:
            last_error = error_msg[:200]
            continue

        memory.save_episode(
            task=query, plan_summary=error_msg[:300],
            context=TaskContext(), success=False,
        )
        return result

    return {"success": False, "error": f"Max retries exceeded. Last: {last_error or 'unknown'}"}


async def _queue_executor(task: QueuedTask) -> dict:
    """Background queue processor — called for each task."""
    query = task.query
    intent = task.intent or "execute_explain"

    set_thought(f"Processing: {query[:60]}...")

    try:
        result = await _execute_query(query, intent)
        success = result.get("success", False)
        summary = result.get("summary", "")

        # Synthesize natural response
        response_text = await synthesize_response(
            original_request=query, intent=intent, success=success,
            summary=summary, errors=_redact(result.get("error", "")),
        )
        add_assistant_message(response_text, intent=intent, executed=True)

        # Voice notification
        try:
            if success:
                await _roux.task_complete(agent="worker", summary=_redact(summary[:100]))
            else:
                await _roux.task_failed(agent="worker", error=_redact(result.get("error", "")[:100]))
        except Exception:
            pass

        # Update proposal bus if this was a proposal task
        if task.id in _proposal_task_map:
            prop_info = _proposal_task_map.pop(task.id)
            sub = ProposalSubmission(**prop_info, description="", proposed_action=query)
            await finalize_proposal(sub, "completed" if success else "failed", WATCHTOWER_CRON_URL)

        complete_task(success, summary[:100] if summary else None)
        return result

    except Exception as e:
        error_str = _redact(str(e))
        logger.error(f"Queue executor error: {error_str}")
        add_assistant_message(f"Sorry, something went wrong: {error_str[:200]}")
        complete_task(False)
        return {"success": False, "error": error_str}


async def _snapshot_to_watchtower():
    """Snapshot queue state to Watchtower for durability."""
    try:
        snapshot = task_queue.snapshot()
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{WATCHTOWER_URL}/queue_snapshot",
                json={"snapshot": snapshot},
                timeout=aiohttp.ClientTimeout(total=3),
            )
    except Exception:
        pass


# =====================================================
#  COMPANION ENDPOINT
# =====================================================

class CompanionRequest(BaseModel):
    message: str
    confirmed: bool = False
    priority: Optional[str] = None


@app.post("/companion")
async def companion_chat(request: CompanionRequest):
    global _request_count
    _request_count += 1

    user_input = request.message.strip()
    set_thought(f"Processing: {user_input[:60]}...")

    classification = await classify_intent(user_input)
    intent = classification.get("intent", "execute_explain")
    risk_level = classification.get("risk_level", "low")

    add_user_message(user_input, intent=intent)

    if intent == "chat":
        response = await generate_chat_response(user_input)
        add_assistant_message(response)
        clear_activity()
        return {"success": True, "response": response, "intent": intent, "executed": False}

    if intent == "chat_informed":
        response = await generate_informed_chat_response(user_input)
        add_assistant_message(response)
        clear_activity()
        return {"success": True, "response": response, "intent": intent, "executed": False}

    if intent == "clarify":
        question = classification.get("clarifying_question")
        response = generate_clarification_prompt(user_input, question)
        add_assistant_message(response)
        clear_activity()
        return {"success": True, "response": response, "intent": intent,
                "executed": False, "needs_clarification": True}

    if intent == "confirm" and not request.confirmed:
        response = generate_confirmation_prompt(user_input, f"This is a {risk_level}-risk operation.")
        add_assistant_message(response)
        return {"success": True, "response": response, "intent": intent,
                "executed": False, "needs_confirmation": True, "original_request": user_input}

    # Queue for execution
    priority_map = {"urgent": TaskPriority.URGENT, "normal": TaskPriority.NORMAL,
                    "background": TaskPriority.BACKGROUND}
    priority = priority_map.get((request.priority or "normal").lower(), TaskPriority.NORMAL)

    task_id = task_queue.submit(query=user_input, priority=priority,
                                intent=intent, confirmed=request.confirmed)

    task_summary = classification.get("task_summary", user_input)
    queue_state = task_queue.get_queue_state()
    position = queue_state["stats"]["pending_count"]

    ack_msg = (f"⚡ On it — {task_summary[:100]}. I'll have results shortly."
               if position <= 1
               else f"⚡ Got it — {task_summary[:100]}. Queued (position {position}).")
    add_assistant_message(ack_msg)

    return {"success": True, "response": ack_msg, "intent": intent, "executed": False,
            "queued": True, "task_id": task_id, "priority": priority.name.lower(),
            "queue_position": position}


@app.get("/conversation")
async def get_conversation():
    return {"messages": get_recent_messages(20)}


# =====================================================
#  STARTUP
# =====================================================

@app.on_event("startup")
async def startup_event():
    register_process("orchestrator")
    logger.info(f"Orchestrator initialized on port {PORT}")

    task_queue.set_executor(_queue_executor)
    task_queue.set_on_change(_snapshot_to_watchtower)
    asyncio.create_task(task_queue.process_loop())

    async def _restore_queue():
        for _ in range(6):
            await asyncio.sleep(5)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{WATCHTOWER_URL}/queue_snapshot", timeout=3) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("snapshot"):
                                task_queue.restore(data["snapshot"])
                    async with session.get(f"{WATCHTOWER_URL}/pending_results", timeout=3) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for item in data.get("results", []):
                                result = item.get("result", {})
                                clean = result.get("summary", "Recovered from restart").split("\n")[0]
                                emoji = "✅" if result.get("success") else "❌"
                                add_assistant_message(f"{emoji} [Recovered after restart] {clean}")
                return
            except Exception:
                pass

    asyncio.create_task(_restore_queue())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
