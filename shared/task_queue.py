"""
Task Queue System
------------------
Priority queue for the Orchestrator. Tasks come in from /companion,
get queued by priority, and execute sequentially via a background loop.

Priority levels:
  - urgent (0)   → jumps to front
  - normal (1)   → standard FIFO within priority band
  - background (2) → only runs when nothing else is queued

Supports:
  - Non-blocking task submission (returns task_id immediately)
  - Cancel/pause queued tasks
  - Snapshot/restore for Watchtower durability
  - Real-time status via get_queue_state()
"""

import asyncio
import time
import uuid
import json
import sys
from enum import Enum
from typing import Optional, Dict, List, Any, Callable, Awaitable
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.logger import get_logger
from shared.kill_switch import is_engaged as _kill_switch_engaged
from shared.execution_budget import check_budget as _check_budget, record_execution as _record_execution
from shared.blackbox import log_event as _bb_log

logger = get_logger("task_queue")


class TaskPriority(int, Enum):
    URGENT = 0
    NORMAL = 1
    BACKGROUND = 2


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class QueuedTask:
    id: str
    query: str
    priority: TaskPriority
    state: TaskState = TaskState.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    intent: Optional[str] = None
    confirmed: bool = False
    archived: bool = False

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "query": self.query,
            "priority": self.priority.value,
            "priority_label": self.priority.name.lower(),
            "state": self.state.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error,
            "intent": self.intent,
            "confirmed": self.confirmed,
        }
        if self.archived:
            d["archived"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "QueuedTask":
        return cls(
            id=data["id"],
            query=data["query"],
            priority=TaskPriority(data["priority"]),
            state=TaskState(data["state"]),
            created_at=data.get("created_at", time.time()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            result=data.get("result"),
            error=data.get("error"),
            intent=data.get("intent"),
            confirmed=data.get("confirmed", False),
            archived=data.get("archived", False),
        )


_DEFAULT_HISTORY_PATH = Path(__file__).parent.parent / "state" / "queue_history.json"


class TaskQueue:
    """Priority task queue with background processing loop."""

    MAX_HISTORY = 50
    MAX_PERSISTED = 200

    def __init__(self, history_path: Optional[Path] = None):
        self._pending: List[QueuedTask] = []
        self._current: Optional[QueuedTask] = None
        self._history: List[QueuedTask] = []
        self._executor: Optional[Callable[[QueuedTask], Awaitable[Dict]]] = None
        self._on_change: Optional[Callable[[], Awaitable[None]]] = None
        self._running = False
        self._paused = False
        self._loop_task: Optional[asyncio.Task] = None
        self._current_task_handle: Optional[asyncio.Task] = None
        self._history_path = history_path or _DEFAULT_HISTORY_PATH
        self._pending_path = self._history_path.parent / "queue_pending.json"
        self._load_history()
        self._load_pending()

    def set_executor(self, fn: Callable[[QueuedTask], Awaitable[Dict]]):
        self._executor = fn

    def set_on_change(self, fn: Callable[[], Awaitable[None]]):
        self._on_change = fn

    async def _notify_change(self):
        if self._on_change:
            try:
                await self._on_change()
            except Exception as e:
                logger.warning(f"Queue change notification failed: {e}")

    def submit(self, query: str, priority: TaskPriority = TaskPriority.NORMAL,
               intent: Optional[str] = None, confirmed: bool = False) -> str:
        task = QueuedTask(id=uuid.uuid4().hex[:12], query=query, priority=priority,
                          intent=intent, confirmed=confirmed)
        inserted = False
        for i, existing in enumerate(self._pending):
            if task.priority.value < existing.priority.value:
                self._pending.insert(i, task)
                inserted = True
                break
        if not inserted:
            self._pending.append(task)
        logger.info(f"📥 QUEUED [{task.priority.name}] #{task.id}: {task.query[:60]} ({len(self._pending)} in queue)")
        self._save_pending()
        try:
            asyncio.get_running_loop().create_task(self._notify_change())
        except RuntimeError:
            pass
        return task.id

    def cancel(self, task_id: str) -> bool:
        for i, task in enumerate(self._pending):
            if task.id == task_id:
                task.state = TaskState.CANCELLED
                task.completed_at = time.time()
                self._pending.pop(i)
                self._history.append(task)
                self._trim_history()
                self._save_pending()
                logger.info(f"🚫 CANCELLED #{task_id}")
                try:
                    asyncio.get_running_loop().create_task(self._notify_change())
                except RuntimeError:
                    pass
                return True
        return False

    def cancel_running(self) -> Optional[str]:
        if not self._current:
            return None
        task_id = self._current.id
        if self._current_task_handle and not self._current_task_handle.done():
            self._current_task_handle.cancel()
            logger.info(f"🛑 ABORT requested for running task #{task_id}")
        return task_id

    def pause(self):
        self._paused = True
        logger.info("⏸️  Queue PAUSED")

    def resume(self):
        self._paused = False
        logger.info("▶️  Queue RESUMED")

    @property
    def is_paused(self) -> bool:
        return self._paused

    def get_task(self, task_id: str) -> Optional[Dict]:
        if self._current and self._current.id == task_id:
            return self._current.to_dict()
        for task in self._pending:
            if task.id == task_id:
                return task.to_dict()
        for task in self._history:
            if task.id == task_id:
                return task.to_dict()
        return None

    def get_queue_state(self) -> Dict:
        return {
            "pending": [t.to_dict() for t in self._pending],
            "current": self._current.to_dict() if self._current else None,
            "history": [t.to_dict() for t in reversed(self._history[-20:])],
            "stats": {
                "pending_count": len(self._pending),
                "is_processing": self._current is not None,
                "is_paused": self._paused,
                "total_completed": sum(1 for t in self._history if t.state == TaskState.COMPLETED),
                "total_failed": sum(1 for t in self._history if t.state == TaskState.FAILED),
            },
        }

    def _trim_history(self):
        if len(self._history) > self.MAX_HISTORY:
            self._history = self._history[-self.MAX_HISTORY:]
        self._save_history()

    def _load_history(self):
        if not self._history_path.exists():
            return
        try:
            with open(self._history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._history = [QueuedTask.from_dict(t) for t in data.get("tasks", [])]
            if len(self._history) > self.MAX_HISTORY:
                self._history = self._history[-self.MAX_HISTORY:]
            logger.info(f"📂 Loaded {len(self._history)} tasks from history")
        except Exception as e:
            logger.warning(f"Failed to load history: {e}")

    def _save_history(self):
        try:
            existing = []
            if self._history_path.exists():
                try:
                    with open(self._history_path, "r", encoding="utf-8") as f:
                        existing = json.load(f).get("tasks", [])
                except Exception:
                    existing = []
            existing_archived = {d["id"]: d.get("archived", False) for d in existing}
            seen_ids = set()
            merged = []
            for t in self._history:
                d = t.to_dict()
                if not d.get("archived") and existing_archived.get(d["id"], False):
                    d["archived"] = True
                if d["id"] not in seen_ids:
                    seen_ids.add(d["id"])
                    merged.append(d)
            for d in existing:
                if d["id"] not in seen_ids:
                    seen_ids.add(d["id"])
                    merged.append(d)
            merged.sort(key=lambda x: x.get("completed_at") or x.get("created_at") or 0, reverse=True)
            merged = merged[:self.MAX_PERSISTED]
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._history_path, "w", encoding="utf-8") as f:
                json.dump({"tasks": merged, "updated_at": time.time()}, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save history: {e}")

    def _save_pending(self):
        try:
            self._pending_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._pending_path, "w", encoding="utf-8") as f:
                json.dump({"pending": [t.to_dict() for t in self._pending], "updated_at": time.time()}, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save pending: {e}")

    def _load_pending(self):
        if not self._pending_path.exists():
            return
        try:
            with open(self._pending_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            restored = [QueuedTask.from_dict(t) for t in data.get("pending", [])]
            if restored:
                existing_ids = {t.id for t in self._pending}
                for t in restored:
                    if t.id not in existing_ids:
                        self._pending.append(t)
                self._pending.sort(key=lambda t: (t.priority.value, t.created_at))
                logger.info(f"📂 Restored {len(restored)} pending tasks from disk")
            self._pending_path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to load pending: {e}")

    def get_full_history(self, limit: int = 50, offset: int = 0, include_archived: bool = False) -> List[Dict]:
        try:
            if not self._history_path.exists():
                return []
            with open(self._history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tasks = data.get("tasks", [])
            if not include_archived:
                tasks = [t for t in tasks if not t.get("archived", False)]
            return tasks[offset:offset + limit]
        except Exception:
            return [t.to_dict() for t in reversed(self._history)]

    def archive_task(self, task_id: str) -> bool:
        return self._set_archive_flag(task_id, True)

    def unarchive_task(self, task_id: str) -> bool:
        return self._set_archive_flag(task_id, False)

    def archive_all(self) -> int:
        try:
            if not self._history_path.exists():
                return 0
            with open(self._history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            count = sum(1 for t in data.get("tasks", []) if not t.get("archived", False))
            for task in data.get("tasks", []):
                task["archived"] = True
            with open(self._history_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return count
        except Exception as e:
            logger.warning(f"Failed to archive all: {e}")
            return 0

    def _set_archive_flag(self, task_id: str, archived: bool) -> bool:
        try:
            if not self._history_path.exists():
                return False
            with open(self._history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for task in data.get("tasks", []):
                if task.get("id") == task_id:
                    task["archived"] = archived
                    with open(self._history_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
                    return True
            return False
        except Exception as e:
            logger.warning(f"Failed to set archive flag: {e}")
            return False

    def snapshot(self) -> Dict:
        return {
            "pending": [t.to_dict() for t in self._pending],
            "current": self._current.to_dict() if self._current else None,
            "history": [t.to_dict() for t in self._history],
            "timestamp": time.time(),
        }

    def restore(self, data: Dict):
        if not data:
            return
        self._pending = [QueuedTask.from_dict(t) for t in data.get("pending", [])]
        current_data = data.get("current")
        if current_data and current_data["state"] == TaskState.RUNNING.value:
            requeued = QueuedTask.from_dict(current_data)
            requeued.state = TaskState.QUEUED
            requeued.started_at = None
            self._pending.insert(0, requeued)
            logger.info(f"🔄 Re-queued interrupted task #{requeued.id}")
        self._history = [QueuedTask.from_dict(t) for t in data.get("history", [])]
        logger.info(f"📦 Queue restored: {len(self._pending)} pending, {len(self._history)} in history")

    async def process_loop(self):
        """Background loop — pulls tasks from queue and executes them."""
        if not self._executor:
            raise RuntimeError("No executor set! Call set_executor() first.")
        self._running = True
        logger.info("🔄 Task queue processing loop started")
        while self._running:
            if _kill_switch_engaged():
                if not self._paused:
                    logger.warning("🛑 Kill switch engaged — task queue frozen")
                    self._paused = True
                await asyncio.sleep(2)
                continue
            if not self._pending or self._paused:
                await asyncio.sleep(1)
                continue
            budget_ok, budget_info = _check_budget()
            if not budget_ok:
                resets_in = budget_info.get("window_resets_in", 0)
                logger.warning(f"📊 Budget exhausted ({budget_info['used']}/{budget_info['max']}/hr) — pausing")
                await asyncio.sleep(min(30, max(5, resets_in)))
                continue
            task = self._pending.pop(0)
            task.state = TaskState.RUNNING
            task.started_at = time.time()
            self._current = task
            self._save_pending()
            logger.info(f"▶️  EXECUTING [{task.priority.name}] #{task.id}: {task.query[:60]}")
            _bb_log("task_start", {"task_id": task.id, "query": task.query[:200],
                                    "priority": task.priority.name, "intent": task.intent}, source="task_queue")
            await self._notify_change()
            try:
                self._current_task_handle = asyncio.current_task()
                result = await self._executor(task)
                task.result = result
                task.state = TaskState.COMPLETED if result.get("success") else TaskState.FAILED
                if not result.get("success"):
                    task.error = result.get("error", "Unknown error")
            except asyncio.CancelledError:
                task.state = TaskState.CANCELLED
                logger.info(f"🚫 Task #{task.id} cancelled during execution")
            except Exception as e:
                task.state = TaskState.FAILED
                task.error = str(e)
                logger.error(f"❌ Task #{task.id} crashed: {e}")
            task.completed_at = time.time()
            duration = task.completed_at - task.started_at
            emoji = "✅" if task.state == TaskState.COMPLETED else ("❌" if task.state == TaskState.FAILED else "🚫")
            logger.info(f"{emoji} #{task.id} {task.state.value} in {duration:.1f}s")
            self._current = None
            self._current_task_handle = None
            self._history.append(task)
            self._trim_history()
            _record_execution()
            _bb_log("task_complete" if task.state == TaskState.COMPLETED else "task_failed",
                    {"task_id": task.id, "query": task.query[:200], "state": task.state.value,
                     "duration": round(duration, 1), "error": task.error[:300] if task.error else None},
                    source="task_queue")
            await self._notify_change()

    def stop(self):
        self._running = False
        logger.info("⏹️  Task queue processing loop stopped")
