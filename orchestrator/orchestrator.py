"""
Orchestrator Agent (Level 5) — Phase 19: Task Queue Edition
------------------------------------------------------------
Central brain of Mission Control. Routes intents, manages the task queue,
coordinates Coder + Worker, and provides real-time state to the dashboard.

Phase 19 changes:
  - /companion is now NON-BLOCKING for execution tasks (returns task_id)
  - Background queue loop processes tasks sequentially by priority
  - Queue state snapshotted to Watchtower for durability
  - New endpoints: GET /queue, GET /queue/{id}, DELETE /queue/{id}
"""

import sys
import os
import asyncio
import aiohttp
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any

# Ensure we can import from shared
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Load .env before anything reads env vars (Phase 34: Claude API key, etc.)
# Phase 39: override=True because parent process (Claude Code, etc.) may
# inject empty API key env vars that would otherwise block the real values.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(project_root, ".env"), override=True)
except ImportError:
    pass  # python-dotenv not installed — env vars must be set externally

from shared.lifecycle import register_process
from shared.memory import memory
from shared.schemas import TaskContext
from shared.logger import get_logger
from shared.activity import set_thought, set_status, complete_task, clear_activity
from shared.task_queue import TaskQueue, TaskPriority, TaskState, QueuedTask
from shared.companion import (
    classify_intent,
    synthesize_response,
    generate_chat_response,
    generate_informed_chat_response,
    format_confirmation_request as generate_confirmation_prompt,
    format_clarification_request as generate_clarification_prompt,
    add_user_message,
    add_assistant_message,
    get_recent_messages,
)
from shared.blackbox import log_event as _bb_log
from shared.roux_client import roux as _roux
from shared.llm import init_providers, get_registry, warm_models  # Phase 34
from shared.event_bus import create_sse_endpoint, create_events_rest_endpoint  # Phase 37
from shared.cost_tracker import tracker as cost_tracker  # Phase 35 dashboard
from shared.verifier import extract_intent_spec, verify_task  # Phase 39: silent drift verifier
from shared.verifier_log import (  # Apr 6: verifier observability layer
    record_verification as _verifier_log_record,
    tag_verification as _verifier_log_tag,
    read_recent as _verifier_log_recent,
    compute_stats as _verifier_log_stats,
)

# Phase 28: Track proposal_id -> task_id mapping for completion callbacks
_proposal_task_map: dict = {}  # {task_id: proposal_id}

# Phase 39: Track in-flight intent extraction futures by task_id.
# Extraction kicks off in /companion (non-blocking), the queue executor
# awaits the future right before calling verify_task at task close.
_intent_spec_futures: dict = {}  # {task_id: asyncio.Task}

# Intents we never verify — no execution to grade against
_VERIFY_SKIP_INTENTS = {"chat", "chat_informed", "clarify", "confirm"}
import re
import time as _time

# SECURITY: Credential redaction — Phase 36: now uses shared/permissions.py
from shared.permissions import redact_credentials as _redact

# --- CONFIGURATION ---
PORT = 8001
GATEWAY_URL = "http://localhost:8000"  # Phase 20: Route through Gateway
CODER_URL = f"{GATEWAY_URL}/coder/plan"
WORKER_URL = f"{GATEWAY_URL}/worker/execute"
WATCHTOWER_URL = "http://localhost:8010"  # Direct — supervisor is privileged
MAX_RETRIES = 2

RETRYABLE_ERRORS = [
    "Anchor line not found",
    "FIND text not found",
    "FIND text appears",
    "Invalid patch format",
    "PATCH ROLLED BACK",
]

# Phase 29.5: Errors that should NOT trigger auto-retry (genuine dead ends)
NON_RETRYABLE_ERRORS = [
    "No such file", "File not found", "File Not Found", "does not exist",
    "Permission denied", "not a valid",
    "Worker unreachable", "Coder unreachable",
    "Kill switch", "Budget exhausted",
]


def _should_auto_retry(error_msg: str) -> bool:
    """Check if a failed task should be auto-retried with a different approach."""
    if not error_msg:
        return False
    lower = error_msg.lower()
    return not any(p.lower() in lower for p in NON_RETRYABLE_ERRORS)

# --- LOGGING & METRICS ---
logger = get_logger("orchestrator")

_start_time = _time.time()
_request_count = 0

# --- TASK QUEUE (Phase 19) ---
task_queue = TaskQueue()

# --- FASTAPI APP ---
app = FastAPI(title="Orchestrator Agent (Level 5) — Phase 19")


# =====================================================
#  QUEUE ENDPOINTS (Phase 19)
# =====================================================

@app.get("/queue")
async def get_queue():
    """Full queue state for dashboard polling."""
    return task_queue.get_queue_state()

@app.get("/queue/history")
async def get_queue_history(limit: int = 50, offset: int = 0, include_archived: bool = False):
    """Paginated task history from persistent storage."""
    tasks = task_queue.get_full_history(limit=limit, offset=offset, include_archived=include_archived)
    return {"tasks": tasks, "count": len(tasks), "offset": offset}

@app.get("/queue/{task_id}")
async def get_queue_task(task_id: str):
    """Get state of a specific queued task."""
    task = task_queue.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task


@app.delete("/queue/{task_id}")
async def cancel_queue_task(task_id: str):
    """Cancel a queued (not yet running) task."""
    success = task_queue.cancel(task_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"Task {task_id} not found in queue (may already be running or completed)",
        )
    return {"cancelled": True, "task_id": task_id}


@app.post("/queue/abort")
async def abort_running_task():
    """Cancel the currently running task."""
    task_id = task_queue.cancel_running()
    if not task_id:
        raise HTTPException(status_code=400, detail="No task currently running")
    return {"aborted": True, "task_id": task_id}


@app.post("/queue/pause")
async def pause_queue():
    """Pause queue processing. Current task finishes, no new ones start."""
    task_queue.pause()
    return {"paused": True}


@app.post("/queue/resume")
async def resume_queue():
    """Resume queue processing."""
    task_queue.resume()
    return {"paused": False}

@app.post("/queue/{task_id}/archive")
async def archive_task(task_id: str):
    """Archive a completed task (hide from default history view)."""
    success = task_queue.archive_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found in history")
    return {"archived": True, "task_id": task_id}


@app.post("/queue/{task_id}/unarchive")
async def unarchive_task(task_id: str):
    """Unarchive a task (show in default history view again)."""
    success = task_queue.unarchive_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found in history")
    return {"archived": False, "task_id": task_id}


@app.post("/queue/archive-all")
async def archive_all_tasks():
    """Archive all completed/failed tasks."""
    count = task_queue.archive_all()
    return {"archived_count": count}



# =====================================================
#  PROPOSAL EXECUTION (Phase 25.1b)
# =====================================================

WATCHTOWER_CRON_URL = "http://localhost:8012"

class ProposalSubmission(BaseModel):
    proposal_id: str
    title: str
    description: str
    category: str
    priority: int
    proposed_action: str
    executor: str  # watchtower, coder, worker, manual
    executor_meta: Dict[str, Any] = {}


@app.post("/queue/proposal")
async def submit_proposal(submission: ProposalSubmission):
    """
    Phase 25.1b: Accept an approved proposal and route it.
    
    Flow:
      1. Mark proposal as 'executing' in the bus
      2. Route based on executor type:
         - watchtower: call /restart/{service} directly
         - coder/worker: submit to task queue as normal task
         - manual: just log it, mark completed
      3. Update proposal state on completion
    """
    prop_id = submission.proposal_id
    logger.info(f"PROPOSAL: Received approved proposal {prop_id} — {submission.title}")
    logger.info(f"  Executor: {submission.executor}, Meta: {submission.executor_meta}")
    
    # Mark executing in the bus
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{WATCHTOWER_CRON_URL}/proposals/{prop_id}/state",
                params={"new_state": "executing"},
                timeout=5,
            ) as resp:
                pass
    except Exception:
        pass  # Non-fatal — bus might be down
    
    # Route based on executor
    if submission.executor == "watchtower":
        return await _execute_watchtower_proposal(submission)
    elif submission.executor in ("coder", "worker"):
        return await _execute_code_proposal(submission)
    elif submission.executor == "manual":
        return await _execute_manual_proposal(submission)
    else:
        return {"success": False, "error": f"Unknown executor: {submission.executor}"}


async def _finalize_proposal(sub: ProposalSubmission, final_state: str, result_data: Any = None):
    """
    Phase 25.1c: Unified post-execution hook for ALL proposal types.
    
    1. Updates proposal state in the bus (executing → completed/failed)
    2. Saves enriched episode to episodic memory
    3. Includes proposal metadata for future Coach queries
    """
    success = final_state == "completed"
    
    # 1. Update bus state
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{WATCHTOWER_CRON_URL}/proposals/{sub.proposal_id}/state",
                params={"new_state": final_state},
                timeout=5,
            ) as resp:
                pass
    except Exception:
        pass
    
    # 2. Get recurrence count for context
    recurrence = 0
    try:
        from shared.proposal_bus import get_recurrence_count
        recurrence = get_recurrence_count(sub.title)
    except Exception:
        pass
    
    # 3. Build enriched plan_summary with proposal metadata
    #    This makes proposals searchable via memory.retrieve_relevant()
    recurrence_note = f" Recurrence: {recurrence}x." if recurrence > 1 else ""
    plan_summary = (
        f"Category: {sub.category}. Executor: {sub.executor}. "
        f"Action: {sub.proposed_action}. Result: {final_state}."
        f"{recurrence_note}"
    )
    
    # 4. Save to episodic memory
    memory.save_episode(
        task=f"[PROPOSAL] {sub.title}",
        plan_summary=plan_summary,
        context=TaskContext(),
        success=success,
    )
    
    logger.info(f"PROPOSAL: {sub.proposal_id} → {final_state}")
    _bb_log(f"proposal_{final_state}", {
        "proposal_id": sub.proposal_id, "title": sub.title,
        "category": sub.category, "executor": sub.executor,
        "priority": sub.priority,
    }, source="orchestrator")


async def _execute_watchtower_proposal(sub: ProposalSubmission) -> Dict:
    """Handle infrastructure proposals (restarts, deploys)."""
    svc_name = sub.executor_meta.get("service_name")
    result_data = None
    
    if svc_name:
        logger.info(f"PROPOSAL: Dispatching restart of {svc_name} to Watchtower")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{WATCHTOWER_CRON_URL}/restart/{svc_name}",
                    timeout=30,
                ) as resp:
                    result_data = await resp.json()
        except Exception as e:
            result_data = {"success": False, "error": str(e)}
    else:
        result_data = {"success": False, "error": "No service_name in executor_meta"}
    
    # Update bus state + save to memory
    success = result_data.get("success", False)
    new_state = "completed" if success else "failed"
    await _finalize_proposal(sub, new_state, result_data)
    
    return {"success": success, "result": result_data, "state": new_state}


async def _execute_code_proposal(sub: ProposalSubmission) -> Dict:
    """Handle code/worker proposals — submit to the task queue."""
    priority_map = {
        9: TaskPriority.URGENT, 8: TaskPriority.URGENT,
        7: TaskPriority.NORMAL, 6: TaskPriority.NORMAL,
        5: TaskPriority.NORMAL, 4: TaskPriority.NORMAL,
        3: TaskPriority.BACKGROUND, 2: TaskPriority.BACKGROUND,
        1: TaskPriority.BACKGROUND,
    }
    priority = priority_map.get(sub.priority, TaskPriority.NORMAL)
    
    # Submit as a regular task — the Coder/Worker will handle it
    task_id = task_queue.submit(
        query=f"[PROPOSAL] {sub.title}: {sub.proposed_action}",
        priority=priority,
        intent="execute",
    )
    
    logger.info(f"PROPOSAL: {sub.proposal_id} queued as task {task_id} (priority: {priority.name})")
    
    # Phase 28: Record mapping so we can update proposal bus when task completes
    _proposal_task_map[task_id] = {
        "proposal_id": sub.proposal_id,
        "title": sub.title,
        "category": sub.category,
        "executor": sub.executor,
        "priority": sub.priority,
    }
    logger.info(f"PROPOSAL->TASK MAP: {sub.proposal_id} -> {task_id}")
    
    return {
        "success": True,
        "queued": True,
        "task_id": task_id,
        "priority": priority.name.lower(),
    }


async def _execute_manual_proposal(sub: ProposalSubmission) -> Dict:
    """Handle manual proposals — just log and mark completed."""
    logger.info(f"PROPOSAL: Manual proposal acknowledged — {sub.title}")
    
    # Mark completed in bus + save to memory
    await _finalize_proposal(sub, "completed", {"acknowledged": True})
    
    return {"success": True, "state": "completed", "message": "Manual proposal acknowledged"}


# =====================================================
#  STANDARD ENDPOINTS
# =====================================================

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

@app.get("/health")
async def health():
    return {"status": "ok"}


# =====================================================
#  VERIFIER OBSERVABILITY (Apr 6 2026)
# =====================================================
# Phase 1 verifier is flag-only — these endpoints expose the data
# DJ needs to compute false-positive / false-negative rates and
# decide when Phase 2 (auto-remediation) is safe to ship.

@app.get("/verifier/recent")
async def verifier_recent(n: int = 20, kind: Optional[str] = None):
    """Return the N most recent verifier records.

    Query params:
      n: how many records to return (default 20)
      kind: filter by record kind ("verification" or "tag"). Omit for all.
    """
    return {"records": _verifier_log_recent(n=n, kind=kind)}


@app.get("/verifier/stats")
async def verifier_stats(window_hours: float = 24.0):
    """Aggregate verifier metrics over the given time window.

    Query params:
      window_hours: lookback window in hours (default 24, use 0 for all-time)
    """
    if window_hours <= 0:
        window_hours = None
    return _verifier_log_stats(window_hours=window_hours)


class VerifierTagRequest(BaseModel):
    task_id: str  # hex string from task_queue
    tag: str  # one of: true_positive, false_positive, true_negative, false_negative
    note: Optional[str] = ""


@app.post("/verifier/tag")
async def verifier_tag(request: VerifierTagRequest):
    """Tag a past verification as TP/FP/TN/FN for FP/FN rate computation.

    Tags are append-only events on the same JSONL log — calling this twice
    for the same task_id keeps both records, the most recent one wins
    when computing stats. The audit trail is preserved.
    """
    ok = _verifier_log_tag(
        task_id=request.task_id,
        tag=request.tag,
        note=request.note or "",
    )
    if not ok:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid tag '{request.tag}' or write failed. "
                "Valid tags: true_positive, false_positive, true_negative, false_negative"
            ),
        )
    return {"success": True, "task_id": request.task_id, "tag": request.tag}


# =====================================================
#  CORE EXECUTION LOGIC (used by queue processor)
# =====================================================

class UserQuery(BaseModel):
    query: str

def _is_retryable(error_msg: str) -> bool:
    if not error_msg:
        return False
    return any(pattern in error_msg for pattern in RETRYABLE_ERRORS)


# Actions that require reading the file before operating on it.
# write_file is excluded — it creates/overwrites and doesn't need anchor text.
_REQUIRES_READ_FIRST = {"patch_file", "edit_file", "append_to_file", "patch"}

def _ensure_read_before_write(plan: list) -> list:
    """
    Orchestrator-level guard (Phase 29.4+):
    For any step that modifies an existing file (patch_file, edit_file, etc.),
    ensure a read_file step for the same path appears earlier in the plan.
    If not, prepend one immediately before the write step.

    This is hard enforcement — Coder's prompt is a soft reminder,
    but this guard runs every time regardless of LLM behaviour.
    """
    # Collect all paths already read before each index
    result = list(plan)
    i = 0
    while i < len(result):
        step = result[i]
        action = (step.get("action") or "").lower()
        if action in _REQUIRES_READ_FIRST:
            # Find the file path — Coder uses 'path', 'file_path', or 'target'
            path = (
                step.get("path")
                or step.get("file_path")
                or step.get("target")
                or ""
            )
            if path:
                # Check if any earlier step already reads this path
                already_read = any(
                    (s.get("action") or "").lower() == "read_file"
                    and (
                        s.get("path") == path
                        or s.get("file_path") == path
                        or s.get("target") == path
                    )
                    for s in result[:i]
                )
                if not already_read:
                    read_step = {
                        "action": "read_file",
                        "path": path,
                        "_injected_by_orchestrator": True,
                    }
                    result.insert(i, read_step)
                    logger.info(
                        f"PLAN GUARD: Injected read_file for '{path}' "
                        f"before '{action}' (step {i+1})"
                    )
                    i += 1  # Skip past the injected read step
        i += 1
    return result

async def _execute_task_internal(query: str) -> Dict:
    """
    Core execution: Coder plans → Worker executes → retry on failure.
    Returns {"success": bool, "response": str, "error": str|None}
    
    This is the guts of the old /chat endpoint, extracted so both
    /chat (synchronous) and the queue processor can use it.
    """
    global _request_count
    _request_count += 1

    logger.info(f"Received query: {query}")
    set_thought(f"Received task: {query[:80]}...")

    last_error = None
    last_results = None

    for attempt in range(1, MAX_RETRIES + 2):
        async with aiohttp.ClientSession() as session:
            # Build Coder payload
            if attempt == 1:
                coder_payload = {
                    "query": query,
                    "context": (
                        "Visual intent detected."
                        if "vision" in query.lower() or "look" in query.lower()
                        else None
                    ),
                }
            else:
                logger.info(f"🔁 RETRY {attempt-1}/{MAX_RETRIES}: Re-planning with error context")
                set_thought(f"Retry {attempt-1}/{MAX_RETRIES}: Re-planning...")

                file_content_hint = ""
                if last_results:
                    for r in last_results:
                        if r.get("action") == "read_file" and r.get("result", {}).get("success"):
                            content = r["result"].get("content", "")
                            if len(content) > 2000:
                                file_content_hint = f"\n\nFile END:\n```\n...{content[-2000:]}\n```"
                            else:
                                file_content_hint = f"\n\nFull file:\n```\n{content}\n```"
                            break

                coder_payload = {
                    "query": query,
                    "context": (
                        f"PREVIOUS ATTEMPT FAILED. Error: {last_error}\n"
                        f"Use EXACT text from the file as your patch anchor."
                        f"{file_content_hint}"
                    ),
                }

            # --- CODER ---
            try:
                set_thought(
                    "Sending to Coder..."
                    if attempt == 1
                    else f"Retry {attempt-1}: Re-planning..."
                )
                async with session.post(CODER_URL, json=coder_payload) as resp:
                    if resp.status != 200:
                        set_status("failed")
                        return {"success": False, "error": f"Coder failed: {await resp.text()}"}
                    plan_data = await resp.json()
            except Exception as e:
                set_status("failed")
                return {"success": False, "error": f"Coder unreachable: {str(e)}"}

            if not plan_data.get("success"):
                return {"success": False, "error": "Coder could not generate plan"}

            plan = plan_data.get("plan", [])
            if not isinstance(plan, list):
                return {"success": False, "error": "Invalid plan format"}

            # Hard guard: ensure read_file precedes any patch/edit step
            plan = _ensure_read_before_write(plan)

            # --- WORKER ---
            worker_payload = {
                "sender": "orchestrator",
                "recipient": "worker",
                "task": "execute_plan",
                "data": {
                    "plan": plan,
                    "initial_context": plan_data.get("initial_context"),
                },
            }

            try:
                async with session.post(WORKER_URL, json=worker_payload) as resp:
                    if resp.status != 200:
                        return {"success": False, "error": f"Worker failed: {await resp.text()}"}
                    result = await resp.json()

                    if result.get("success"):
                        if attempt > 1:
                            logger.info(f"✅ Succeeded on retry {attempt-1}!")
                        logger.info("Task successful! Saving to episodic memory...")
                        complete_task(True, result.get("summary", "Task completed!"))

                        final_ctx = result.get("final_context", {})
                        ctx_obj = TaskContext(**final_ctx) if final_ctx else TaskContext()
                        memory.save_episode(
                            task=query,
                            plan_summary=result.get("summary", "Task completed"),
                            context=ctx_obj,
                            success=True,
                            plan_steps=plan,
                            execution_results=result.get("results", []),
                        )
                        # Phase 39: Propagate plan + worker results so the verifier
                        # can see the actual code that was written, not just prose.
                        return {
                            "success": True,
                            "response": result.get("summary"),
                            "plan": plan,
                            "worker_results": result.get("results", []),
                        }
                    else:
                        error_msg = result.get("error") or "Unknown worker error"
                        worker_summary = result.get("summary", "")

                        if _is_retryable(error_msg) and attempt <= MAX_RETRIES:
                            logger.warning(f"⚠️ Retryable (attempt {attempt}): {error_msg[:200]}")
                            last_error = error_msg
                            last_results = result.get("results", [])
                            continue

                        logger.warning(f"Task failed: {error_msg[:200]}")
                        complete_task(False, f"Failed: {error_msg[:100]}")
                        return {
                            "success": False,
                            "error": error_msg,
                            "response": worker_summary,
                        }
            except Exception as e:
                return {"success": False, "error": f"Worker execution failed: {str(e)}"}

    return {"success": False, "error": f"Max retries exceeded. Last error: {last_error}"}


# =====================================================
#  QUEUE PROCESSOR (Phase 19: Background Loop)
# =====================================================

async def _queue_executor(task: QueuedTask) -> Dict:
    """
    Called by TaskQueue.process_loop() for each task.
    Wraps _execute_task_internal with activity broadcasting.
    """
    set_thought(f"Queue processing [{task.priority.name}]: {task.query[:60]}...")

    result = await _execute_task_internal(task.query)

    # Synthesize a natural response and save to conversation
    success = result.get("success", False)
    summary = _redact(result.get("response") or result.get("error", ""))
    content = summary[:2000]
    errors = _redact(result.get("error", "")) if not success else ""

    response = await synthesize_response(
        original_request=task.query,
        intent=task.intent or "execute",
        success=success,
        summary=summary,
        files=None,
        content_preview=content,
        errors=errors,
    )

    add_assistant_message(response)
    clear_activity()

    # Notify Roux about task result
    if success:
        await _roux.task_complete(agent=task.intent or "system", summary=summary[:200])
    else:
        await _roux.task_failed(
            agent=task.intent or "system", error=errors[:200],
            query=_redact(task.query[:200]), task_id=task.id,
            intent=task.intent or "execute",
        )
        # Phase 29.5: Auto-retry with different approach (capped at 1)
        error_msg = result.get("error", "") or result.get("summary", "")
        if _should_auto_retry(error_msg) and not getattr(task, "is_auto_retry", False):
            retry_id = task_queue.submit_retry(task, error_msg)
            if retry_id:
                logger.info(f"AUTO-RETRY: #{retry_id} queued for failed #{task.id}")
                add_assistant_message("🔄 That didn't work — automatically retrying with a different approach.")

    # Attach synthesized response to the result
    result["synthesized_response"] = response

    # ============================================================
    # Phase 39: SILENT SEMANTIC DRIFT VERIFIER
    # Runs at task close on successful verifiable tasks. Compares
    # the actual code that was written against the constraints
    # extracted from the user's original request at submission time.
    # Advisory only — never blocks the task, never raises.
    # ============================================================
    try:
        if (
            success
            and task.intent not in _VERIFY_SKIP_INTENTS
            and task.intent is not None
        ):
            # Wait for intent extraction to finish (was kicked off at submission)
            spec_future = _intent_spec_futures.pop(task.id, None)
            intent_spec = None
            if spec_future is not None:
                try:
                    intent_spec = await asyncio.wait_for(spec_future, timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning(f"VERIFIER: intent_spec extraction timed out for #{task.id}")
                except Exception as e:
                    logger.warning(f"VERIFIER: intent_spec future raised: {e}")
            elif task.intent_spec:
                # Auto-retry path — parent's intent_spec was inherited via submit_retry,
                # no in-flight future for this child task
                intent_spec = task.intent_spec

            if intent_spec is None:
                # Nothing to verify against — skipped, no badge in dashboard
                skipped_verdict = {
                    "verdict": "skipped",
                    "reason": "no intent_spec available",
                    "missing_constraints": [],
                    "verifier_model": "",
                    "verified_at": _time.time(),
                }
                result["verification_result"] = skipped_verdict
                # Apr 6: log skipped verifications too — they tell us how often
                # the extraction phase is failing or returning empty specs.
                try:
                    _verifier_log_record(
                        task_id=task.id,
                        query=task.query,
                        intent=task.intent,
                        intent_spec=None,
                        plan_steps=result.get("plan", []),
                        verdict_dict=skipped_verdict,
                        latency_ms=0,
                    )
                except Exception:
                    pass
            else:
                # Persist the spec on the task for retry inheritance and history
                task.intent_spec = intent_spec
                # Verify (timed for observability)
                _verify_start = _time.time()
                verdict_dict = await verify_task(
                    original_query=task.query,
                    intent_spec=intent_spec,
                    execution_result=result,
                    plan_steps=result.get("plan", []),
                )
                _verify_latency_ms = int((_time.time() - _verify_start) * 1000)
                result["verification_result"] = verdict_dict

                # Apr 6: persist full record to verifier_log for FP/FN analysis
                try:
                    _verifier_log_record(
                        task_id=task.id,
                        query=task.query,
                        intent=task.intent,
                        intent_spec=intent_spec,
                        plan_steps=result.get("plan", []),
                        verdict_dict=verdict_dict,
                        latency_ms=_verify_latency_ms,
                    )
                except Exception as e:
                    logger.warning(f"VERIFIER: verifier_log write failed: {e}")

                # Episodic logging — every verification (incl. skipped) for tuning
                try:
                    from memory.core_state import get_core_state as _get_core
                    _core_state = _get_core()
                    verdict_str = verdict_dict.get("verdict", "uncertain")
                    success_flag = (
                        True if verdict_str == "pass"
                        else False if verdict_str == "fail"
                        else None
                    )
                    _core_state.record_episode(
                        event_type="verification",
                        source="verifier",
                        summary=verdict_dict.get("reason", "")[:500],
                        success=success_flag,
                        failure_mode=("semantic_drift" if verdict_str == "fail" else None),
                    )
                except Exception as e:
                    logger.warning(f"VERIFIER: episodic logging failed: {e}")

                logger.info(
                    f"VERIFIER #{task.id}: verdict={verdict_dict.get('verdict')} "
                    f"latency={_verify_latency_ms}ms "
                    f"reason={verdict_dict.get('reason', '')[:100]}"
                )
    except Exception as e:
        # The verifier MUST NOT break task completion. Log and move on.
        logger.warning(f"VERIFIER: top-level exception (non-fatal): {e}")
        result["verification_result"] = {
            "verdict": "uncertain",
            "reason": f"verifier wrapper exception: {str(e)[:150]}",
            "missing_constraints": [],
            "verifier_model": "",
            "verified_at": _time.time(),
        }
    finally:
        # Always clean up the future map even on early exit
        _intent_spec_futures.pop(task.id, None)

    # Phase 28: If this task came from a proposal, update proposal bus state
    if task.id in _proposal_task_map:
        prop_info = _proposal_task_map.pop(task.id)
        prop_id = prop_info["proposal_id"]
        new_state = "completed" if success else "failed"
        try:
            import requests as _req
            _req.post(
                f"http://localhost:8012/proposals/{prop_id}/state",
                params={"new_state": new_state},
                timeout=5,
            )
            logger.info(f"PROPOSAL STATE SYNC: {prop_id} -> {new_state} (task {task.id})")
        except Exception as e:
            logger.warning(f"Failed to sync proposal state for {prop_id}: {e}")
    
    return result


async def _snapshot_to_watchtower():
    """Snapshot queue state to Watchtower for durability."""
    try:
        data = task_queue.snapshot()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{WATCHTOWER_URL}/queue_snapshot",
                json=data,
                timeout=3,
            ) as resp:
                if resp.status == 200:
                    logger.debug("📦 Queue snapshot sent to Watchtower")
                else:
                    logger.warning(f"📦 Watchtower snapshot failed: {resp.status}")
    except Exception:
        pass  # Watchtower might be down — non-fatal


# =====================================================
#  /chat ENDPOINT (backward compatible, synchronous)
# =====================================================

@app.post("/chat")
async def handle_chat(request: UserQuery):
    """
    Synchronous execution endpoint.
    Used by Watchtower task loop and direct API calls.
    Phase 19: Delegates to shared _execute_task_internal.
    """
    return await _execute_task_internal(request.query)


# =====================================================
#  /companion ENDPOINT (Phase 19: Non-blocking execution)
# =====================================================

class CompanionRequest(BaseModel):
    message: str
    confirmed: bool = False
    priority: Optional[str] = None  # "urgent", "normal", "background"


@app.post("/companion")
async def companion_chat(request: CompanionRequest):
    """
    Companion chat endpoint.
    - Chat/clarify/confirm intents: handled immediately (synchronous)
    - Execute intents: queued and returned immediately with task_id (async)
    - bigbrain: prefix: route directly to Claude Sonnet via Anthropic API
    """
    user_input = request.message.strip()
    logger.info(f"Companion received: {user_input[:80]}...")
    set_thought(f"Processing: {user_input[:60]}...")

    # Phase 39: bigbrain escalation — explicit user-triggered route to Claude
    if user_input.lower().startswith("bigbrain:"):
        prompt = user_input[len("bigbrain:"):].strip()
        logger.info(f"BIGBRAIN escalation: {prompt[:80]}")
        set_thought("Escalating to Claude Sonnet via bigbrain...")
        add_user_message(user_input, intent="bigbrain")
        try:
            from shared.llm import llm_generate
            llm_response = await llm_generate(
                "bigbrain",
                prompt=prompt,
                system=(
                    "You are Claude Sonnet, being called via RouxYou's bigbrain "
                    "escalation path. RouxYou is DJ's local AI agent system that "
                    "normally runs GPT-OSS 20B for reasoning. When the local stack "
                    "can't handle something, the user explicitly asks you by "
                    "prefixing 'bigbrain:' on a message. Respond directly and "
                    "substantively. You don't have access to DJ's filesystem or "
                    "RouxYou's memory — you're pure reasoning only."
                ),
                temperature=0.7,
                timeout=90,
            )
            if llm_response.success:
                response = llm_response.text
            else:
                response = f"Bigbrain call failed: {llm_response.error}"
        except Exception as e:
            response = f"Bigbrain error: {e}"
        add_assistant_message(response)
        clear_activity()
        return {
            "success": True,
            "response": response,
            "intent": "bigbrain",
            "executed": False,
        }

    # 1. Classify Intent
    classification = await classify_intent(user_input)
    intent = classification.get("intent", "execute_explain")
    risk_level = classification.get("risk_level", "low")
    logger.info(f"Intent: {intent}, Risk: {risk_level}")

    # 2. Store user message
    add_user_message(user_input, intent=intent)

    # --- CHAT: Immediate response ---
    if intent == "chat":
        set_thought("Generating conversational response...")
        response = await generate_chat_response(user_input)
        add_assistant_message(response)
        clear_activity()
        return {
            "success": True,
            "response": response,
            "intent": intent,
            "executed": False,
        }

    # --- CHAT_INFORMED: RAG-augmented conversational response ---
    if intent == "chat_informed":
        set_thought("Checking memory for context...")
        response = await generate_informed_chat_response(user_input)
        add_assistant_message(response)
        clear_activity()
        return {
            "success": True,
            "response": response,
            "intent": intent,
            "executed": False,
        }

    # --- CLARIFY ---
    if intent == "clarify":
        question = classification.get("clarifying_question")
        response = generate_clarification_prompt(user_input, question)
        add_assistant_message(response)
        clear_activity()
        return {
            "success": True,
            "response": response,
            "intent": intent,
            "executed": False,
            "needs_clarification": True,
        }

    # --- CONFIRM (needs user approval) ---
    if intent == "confirm" and not request.confirmed:
        response = generate_confirmation_prompt(
            user_input, f"This is classified as a {risk_level}-risk operation."
        )
        add_assistant_message(response)
        return {
            "success": True,
            "response": response,
            "intent": intent,
            "executed": False,
            "needs_confirmation": True,
            "original_request": user_input,
        }

    # --- EXECUTE: Queue it! (Phase 19 — non-blocking) ---
    # Determine priority
    priority_map = {
        "urgent": TaskPriority.URGENT,
        "normal": TaskPriority.NORMAL,
        "background": TaskPriority.BACKGROUND,
    }
    priority = priority_map.get(
        (request.priority or "normal").lower(), TaskPriority.NORMAL
    )

    task_id = task_queue.submit(
        query=user_input,
        priority=priority,
        intent=intent,
        confirmed=request.confirmed,
    )

    # Phase 39: Kick off intent extraction in parallel with execution.
    # By the time the worker finishes (typ. 20s-2min), this 2-5s extraction
    # call has been done for ages. The queue executor awaits it at task close.
    if intent not in _VERIFY_SKIP_INTENTS:
        try:
            _intent_spec_futures[task_id] = asyncio.create_task(
                extract_intent_spec(user_input, intent)
            )
        except Exception as e:
            logger.warning(f"intent_spec extraction kickoff failed: {e}")

    # Build a conversational acknowledgment that describes the task
    task_summary = classification.get("task_summary", user_input)
    queue_state = task_queue.get_queue_state()
    position = queue_state["stats"]["pending_count"]
    
    # Keep it natural - describe what we're doing, not just queue metadata
    if position <= 1:
        ack_msg = f"⚡ On it — {task_summary[:100]}. I'll have results shortly."
    else:
        ack_msg = (
            f"⚡ Got it — {task_summary[:100]}. "
            f"Queued up (position {position}), will get to it soon."
        )
    add_assistant_message(ack_msg)

    return {
        "success": True,
        "response": ack_msg,
        "intent": intent,
        "executed": False,
        "queued": True,
        "task_id": task_id,
        "priority": priority.name.lower(),
        "queue_position": position,
    }


# =====================================================
#  CONVERSATION ENDPOINT
# =====================================================

@app.get("/conversation")
async def get_conversation():
    messages = get_recent_messages(20)
    return {"messages": messages}


# =====================================================
#  STARTUP
# =====================================================

async def _warmup_background():
    """Apr 6 2026: warm the LLM models that the orchestrator's hot paths use
    so cold-start latency (13s chat / 28s informed_chat measured Apr 5) doesn't
    bite the user's first message. Background task — service stays healthy
    throughout, models are loaded into VRAM in parallel."""
    try:
        # Hot paths: companion (ministral-3:8b) and informed_chat (gpt-oss:20b).
        # router shares the ministral model so it gets warmed for free.
        results = await warm_models(["companion", "informed_chat"])
        for alias, (success, elapsed, err) in results.items():
            if success:
                logger.info(f"🔥 Orchestrator warmup: {alias} loaded in {elapsed:.1f}s")
            else:
                logger.warning(f"⚠️ Orchestrator warmup failed for {alias}: {err}")
    except Exception as e:
        logger.warning(f"Warmup background task crashed (non-fatal): {e}")


@app.on_event("startup")
async def startup_event():
    register_process("orchestrator")
    init_providers()  # Phase 34: Initialize LLM provider registry
    logger.info(f"Orchestrator initialized on port {PORT}")
    logger.info("Phase 19: Task Queue ENABLED")
    logger.info("Phase 37: SSE event stream available at /events/stream")

    # Apr 6 2026: kick warmup as background task — service comes up healthy
    # immediately, models are loaded into VRAM in parallel.
    asyncio.create_task(_warmup_background())

    # Wire up the queue
    task_queue.set_executor(_queue_executor)
    task_queue.set_on_change(_snapshot_to_watchtower)

    # Start the queue processing loop
    asyncio.create_task(task_queue.process_loop())
    logger.info("🔄 Queue processing loop started")

    # Phase 19: Restore queue from Watchtower
    async def _restore_queue():
        """Poll Watchtower for queue snapshot + escrowed results."""
        for attempt in range(6):
            await asyncio.sleep(5)
            try:
                async with aiohttp.ClientSession() as session:
                    # 1. Restore queue snapshot
                    async with session.get(
                        f"{WATCHTOWER_URL}/queue_snapshot", timeout=3
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("snapshot"):
                                task_queue.restore(data["snapshot"])
                                logger.info("📦 Queue restored from Watchtower")

                    # 2. Pick up escrowed results (Phase 18.1 compat)
                    async with session.get(
                        f"{WATCHTOWER_URL}/pending_results", timeout=3
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            pending = data.get("results", [])
                            if pending:
                                logger.info(
                                    f"📦 ESCROW: Picked up {len(pending)} result(s)"
                                )
                                for item in pending:
                                    result = item.get("result", {})
                                    success = result.get("success", False)
                                    summary = result.get(
                                        "summary", "Recovered from restart"
                                    )
                                    clean = (
                                        summary.split("\n")[0] if summary else "Done"
                                    )
                                    emoji = "✅" if success else "❌"
                                    add_assistant_message(
                                        f"{emoji} [Recovered after restart] {clean}"
                                    )
                    return  # Done
            except Exception:
                pass
        logger.debug("Startup restore: no data from Watchtower after 30s")

    asyncio.create_task(_restore_queue())


# Phase 37: SSE event stream + REST polling endpoints
app.add_api_route("/events/stream", create_sse_endpoint(), methods=["GET"])
app.add_api_route("/events/recent", create_events_rest_endpoint(), methods=["GET"])


# Phase 35: Cost tracking endpoint for dashboard
@app.get("/cost/status")
async def cost_status():
    return cost_tracker.status()

@app.get("/cost/by-provider")
async def cost_by_provider():
    return cost_tracker.by_provider()

@app.get("/cost/by-caller")
async def cost_by_caller():
    return cost_tracker.by_caller()

@app.get("/cost/recent")
async def cost_recent(n: int = 10):
    return {"events": cost_tracker.recent(n)}


# Phase 34b: LLM provider status + hot-reload
@app.get("/llm/status")
async def llm_status():
    registry = get_registry()
    status = registry.status()
    # Add health checks for each provider
    health = {}
    for name in status.get("providers", []):
        provider = registry.get_provider(name)
        if provider:
            try:
                health[name] = await provider.health_check()
            except Exception:
                health[name] = False
    status["health"] = health
    return status

@app.post("/llm/reload")
async def llm_reload():
    """Hot-reload LLM config from config.yaml without full restart."""
    registry = get_registry()
    # Reset initialized flag so it re-reads config
    registry._initialized = False
    init_providers()
    return {"success": True, "status": registry.status()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
