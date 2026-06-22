"""
ACTIVITY BROADCASTER
Lets agents "think out loud" by broadcasting their current status.
The dashboard reads this to show what the system is doing in real-time.
"""
import json
import time
from pathlib import Path
from typing import Optional

ACTIVITY_FILE = Path(__file__).parent.parent / "state" / "activity.json"

def _ensure_state_dir():
    ACTIVITY_FILE.parent.mkdir(exist_ok=True)

def _read_activity() -> dict:
    _ensure_state_dir()
    if ACTIVITY_FILE.exists():
        try:
            with open(ACTIVITY_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

def _write_activity(data: dict):
    _ensure_state_dir()
    data["updated_at"] = time.time()
    with open(ACTIVITY_FILE, "w") as f:
        json.dump(data, f, indent=2)

def set_active_task(task_id: str, task_title: str, agent: str = "system"):
    activity = _read_activity()
    activity.update({
        "task_id": task_id,
        "task_title": task_title,
        "agent": agent,
        "status": "active",
        "step": 0,
        "total_steps": 0,
        "thought": f"Starting task: {task_title}",
        "plan": []
    })
    _write_activity(activity)

def set_thought(thought: str):
    activity = _read_activity()
    activity["thought"] = thought
    _write_activity(activity)

def set_plan(steps: list):
    activity = _read_activity()
    activity["plan"] = steps
    activity["total_steps"] = len(steps)
    activity["step"] = 0
    _write_activity(activity)

def set_step(step_num: int, description: Optional[str] = None):
    activity = _read_activity()
    activity["step"] = step_num
    if description:
        activity["thought"] = description
    _write_activity(activity)

def set_status(status: str):
    activity = _read_activity()
    activity["status"] = status
    _write_activity(activity)

def complete_task(success: bool = True, summary: Optional[str] = None):
    activity = _read_activity()
    activity["status"] = "success" if success else "failed"
    activity["thought"] = summary or ("Task completed successfully!" if success else "Task failed.")
    activity["step"] = activity.get("total_steps", 0)
    _write_activity(activity)

def clear_activity():
    _write_activity({
        "task_id": None,
        "task_title": None,
        "agent": None,
        "status": "idle",
        "step": 0,
        "total_steps": 0,
        "thought": "System idle. Waiting for tasks...",
        "plan": []
    })

def get_activity() -> dict:
    activity = _read_activity()
    if not activity:
        return {
            "task_id": None,
            "task_title": None,
            "agent": None,
            "status": "idle",
            "step": 0,
            "total_steps": 0,
            "thought": "System idle. Waiting for tasks...",
            "plan": [],
            "updated_at": time.time()
        }
    return activity
