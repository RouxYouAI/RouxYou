"""
Proposal Handler — Manages the lifecycle of system proposals.
Extracted from orchestrator.py to keep concerns separated.
"""

import json
import time
import aiohttp
from pathlib import Path
from pydantic import BaseModel
from typing import Dict, Any, List

from .memory import memory
from .schemas import TaskContext
from .task_queue import TaskQueue, TaskPriority
from .blackbox import log_event as _bb_log
from .logger import get_logger

BASE_DIR = Path(__file__).parent.parent
TASKS_FILE = BASE_DIR / "tasks.json"

logger = get_logger("proposal_handler")


class ProposalSubmission(BaseModel):
    proposal_id: str
    title: str
    description: str
    category: str
    priority: int
    proposed_action: str
    executor: str
    executor_meta: Dict[str, Any] = {}


# Tracks which queued task IDs correspond to which proposals
proposal_task_map: dict = {}


async def handle_proposal(
    submission: ProposalSubmission,
    task_queue: TaskQueue,
    watchtower_cron_url: str,
) -> Dict:
    """
    Route a proposal to the correct executor.
    Returns the result dict for the API response.
    """
    prop_id = submission.proposal_id
    logger.info(f"PROPOSAL: {prop_id} — {submission.title} (executor: {submission.executor})")

    # Notify Watchtower that execution has started
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{watchtower_cron_url}/proposals/{prop_id}/state",
                params={"new_state": "executing"}, timeout=5,
            )
    except Exception:
        pass

    if submission.executor == "watchtower":
        return await _execute_watchtower(submission, watchtower_cron_url)
    elif submission.executor in ("coder", "worker"):
        return await _execute_code(submission, task_queue)
    elif submission.executor == "task_action":
        return await _execute_task_action(submission, watchtower_cron_url)
    elif submission.executor == "manual":
        return await _execute_manual(submission, watchtower_cron_url)
    return {"success": False, "error": f"Unknown executor: {submission.executor}"}


async def finalize_proposal(
    sub: ProposalSubmission,
    final_state: str,
    watchtower_cron_url: str,
    result_data: Any = None,
):
    """Update proposal state, save to memory, log to blackbox."""
    success = final_state == "completed"

    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{watchtower_cron_url}/proposals/{sub.proposal_id}/state",
                params={"new_state": final_state}, timeout=5,
            )
    except Exception:
        pass

    recurrence = 0
    try:
        from .proposal_bus import get_recurrence_count
        recurrence = get_recurrence_count(sub.title)
    except Exception:
        pass

    recurrence_note = f" Recurrence: {recurrence}x." if recurrence > 1 else ""
    plan_summary = (
        f"Category: {sub.category}. Executor: {sub.executor}. "
        f"Action: {sub.proposed_action}. Result: {final_state}.{recurrence_note}"
    )
    memory.save_episode(
        task=f"[PROPOSAL] {sub.title}", plan_summary=plan_summary,
        context=TaskContext(), success=success,
    )
    _bb_log(f"proposal_{final_state}", {
        "proposal_id": sub.proposal_id, "title": sub.title,
        "category": sub.category, "executor": sub.executor, "priority": sub.priority,
    }, source="orchestrator")


async def _execute_watchtower(sub: ProposalSubmission, watchtower_cron_url: str) -> Dict:
    svc_name = sub.executor_meta.get("service_name")
    result_data = None
    if svc_name:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{watchtower_cron_url}/restart/{svc_name}", timeout=30
                ) as resp:
                    result_data = await resp.json()
        except Exception as e:
            result_data = {"success": False, "error": str(e)}
    else:
        result_data = {"success": False, "error": "No service_name in executor_meta"}

    success = result_data.get("success", False)
    new_state = "completed" if success else "failed"
    await finalize_proposal(sub, new_state, watchtower_cron_url, result_data)
    return {"success": success, "result": result_data, "state": new_state}


async def _execute_code(sub: ProposalSubmission, task_queue: TaskQueue) -> Dict:
    priority_map = {
        9: TaskPriority.URGENT, 8: TaskPriority.URGENT,
        7: TaskPriority.NORMAL, 6: TaskPriority.NORMAL,
        5: TaskPriority.NORMAL, 4: TaskPriority.NORMAL,
        3: TaskPriority.BACKGROUND, 2: TaskPriority.BACKGROUND,
        1: TaskPriority.BACKGROUND,
    }
    priority = priority_map.get(sub.priority, TaskPriority.NORMAL)
    task_id = task_queue.submit(
        query=f"[PROPOSAL] {sub.title}: {sub.proposed_action}",
        priority=priority, intent="execute",
    )
    proposal_task_map[task_id] = {
        "proposal_id": sub.proposal_id, "title": sub.title,
        "category": sub.category, "executor": sub.executor, "priority": sub.priority,
    }
    return {"success": True, "queued": True, "task_id": task_id, "priority": priority.name.lower()}


async def _execute_task_action(sub: ProposalSubmission, watchtower_cron_url: str) -> Dict:
    """Handle task-category proposals by acting on stale/failed tasks."""
    action = sub.executor_meta.get("action", "cancel_stale")
    result_data = {"action": action, "affected": []}

    try:
        if action == "cancel_stale":
            result_data = _cancel_stale_tasks()
        elif action == "cancel_failed":
            result_data = _cancel_failed_tasks()
        else:
            result_data = {"success": False, "error": f"Unknown task action: {action}"}
    except Exception as e:
        result_data = {"success": False, "error": str(e)}

    success = result_data.get("success", False)
    new_state = "completed" if success else "failed"
    await finalize_proposal(sub, new_state, watchtower_cron_url, result_data)
    return {"success": success, "result": result_data, "state": new_state}


def _cancel_stale_tasks(max_age_hours: int = 24) -> Dict:
    """Cancel pending tasks older than max_age_hours from tasks.json."""
    if not TASKS_FILE.exists():
        return {"success": True, "cancelled": 0, "message": "No tasks.json found", "affected": []}

    try:
        with open(TASKS_FILE, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, Exception):
        return {"success": False, "error": "Could not read tasks.json"}

    # Handle both list and dict formats
    if isinstance(data, list):
        tasks = data
    elif isinstance(data, dict):
        tasks = data.get("tasks", [])
    else:
        return {"success": False, "error": "Unexpected tasks.json format"}

    now = time.time()
    cutoff = now - (max_age_hours * 3600)
    cancelled = []
    kept = []

    for task in tasks:
        is_stale = (
            task.get("status") == "pending"
            and task.get("created_at", now) < cutoff
        )
        if is_stale:
            task["status"] = "cancelled"
            task["cancelled_at"] = now
            task["cancelled_reason"] = "Auto-cancelled: stale (>24h pending)"
            cancelled.append({
                "title": task.get("title", "unknown")[:80],
                "age_hours": round((now - task.get("created_at", now)) / 3600, 1),
            })
        kept.append(task)

    # Write back
    if isinstance(data, list):
        write_data = kept
    else:
        data["tasks"] = kept
        write_data = data

    with open(TASKS_FILE, "w") as f:
        json.dump(write_data, f, indent=2)

    if cancelled:
        names = ", ".join(t["title"] for t in cancelled)
        logger.info(f"TASK_ACTION: Cancelled {len(cancelled)} stale tasks: {names}")

    return {
        "success": True,
        "cancelled": len(cancelled),
        "affected": cancelled,
        "message": f"Cancelled {len(cancelled)} stale task(s)" if cancelled else "No stale tasks found",
    }


def _cancel_failed_tasks() -> Dict:
    """Remove repeatedly failed tasks from the queue history."""
    from .task_queue import TaskQueue
    # For now, just report — don't delete queue history
    return {"success": True, "cancelled": 0, "message": "Failed task cleanup not yet implemented"}


async def _execute_manual(sub: ProposalSubmission, watchtower_cron_url: str) -> Dict:
    await finalize_proposal(sub, "completed", watchtower_cron_url, {"acknowledged": True})
    return {"success": True, "state": "completed", "message": "Manual proposal acknowledged"}
