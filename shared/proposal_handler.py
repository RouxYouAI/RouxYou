"""
Proposal Handler — Manages the lifecycle of system proposals.
Extracted from orchestrator.py to keep concerns separated.
"""

import aiohttp
from pydantic import BaseModel
from typing import Dict, Any

from .memory import memory
from .schemas import TaskContext
from .task_queue import TaskQueue, TaskPriority
from .blackbox import log_event as _bb_log
from .logger import get_logger

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


async def _execute_manual(sub: ProposalSubmission, watchtower_cron_url: str) -> Dict:
    await finalize_proposal(sub, "completed", watchtower_cron_url, {"acknowledged": True})
    return {"success": True, "state": "completed", "message": "Manual proposal acknowledged"}
