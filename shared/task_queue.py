"""
Phase 19: Task Queue System
----------------------------
Priority queue for the Orchestrator. Tasks come in from /companion,
get queued by priority, and execute sequentially via a background loop.

Priority levels:
  - urgent (0)   → jumps to front, interrupts background work
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
from enum import Enum
from typing import Optional, Dict, List, Any, Callable, Awaitable
from dataclasses import dataclass, field, asdict
from pathlib import Path

import sys
from pathlib import Path as _Path
_PROJECT_ROOT = _Path(__file__).parent.parent
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
    # For companion integration
    intent: Optional[str] = None
    confirmed: bool = False
    # Phase 28.3: Archive support
    archived: bool = False
    # Phase 29.5: Auto-retry tracking
    is_auto_retry: bool = False
    parent_task_id: Optional[str] = None
    # Phase 39: Captured intent spec for verification at task close
    intent_spec: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        """Serialize for API responses and snapshots."""
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
        if self.is_auto_retry:
            d["is_auto_retry"] = True
        if self.parent_task_id:
            d["parent_task_id"] = self.parent_task_id
        # Phase 39: Only emit intent_spec if present (keeps payloads clean)
        if self.intent_spec:
            d["intent_spec"] = self.intent_spec
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "QueuedTask":
        """Deserialize from snapshot."""
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
            is_auto_retry=data.get("is_auto_retry", False),
            parent_task_id=data.get("parent_task_id"),
            # Phase 39: Load-bearing .get() — older tasks in history don't have this field
            intent_spec=data.get("intent_spec"),
        )


# Default history file location
_DEFAULT_HISTORY_PATH = Path(__file__).parent.parent / "state" / "queue_history.json"


class TaskQueue:
    """
    Priority task queue with background processing loop.

    Usage:
        queue = TaskQueue()
        queue.set_executor(my_async_executor_fn)
        asyncio.create_task(queue.process_loop())

        task_id = queue.submit("Do something", priority=TaskPriority.NORMAL)
        state = queue.get_task(task_id)
    """

    # How many completed/failed tasks to keep in memory
    MAX_HISTORY = 50
    # How many to persist to disk (larger window for dashboard browsing)
    MAX_PERSISTED = 200

    def __init__(self, history_path: Optional[Path] = None):
        # Pending tasks sorted by (priority, created_at)
        self._pending: List[QueuedTask] = []
        # Currently executing task (only one at a time)
        self._current: Optional[QueuedTask] = None
        # Completed/failed/cancelled history
        self._history: List[QueuedTask] = []
        # The function that actually executes a task
        self._executor: Optional[Callable[[QueuedTask], Awaitable[Dict]]] = None
        # Callback for state changes (used to snapshot to Watchtower)
        self._on_change: Optional[Callable[[], Awaitable[None]]] = None
        # Processing loop control
        self._running = False
        self._paused = False
        self._loop_task: Optional[asyncio.Task] = None
        self._current_task_handle: Optional[asyncio.Task] = None
        # Persistence
        self._history_path = history_path or _DEFAULT_HISTORY_PATH
        self._pending_path = self._history_path.parent / "queue_pending.json"
        self._load_history()
        self._load_pending()

    def set_executor(self, fn: Callable[[QueuedTask], Awaitable[Dict]]):
        """Set the async function that processes tasks."""
        self._executor = fn

    def set_on_change(self, fn: Callable[[], Awaitable[None]]):
        """Set callback fired on every queue state change (for Watchtower snapshots)."""
        self._on_change = fn

    async def _notify_change(self):
        """Fire the on_change callback if set."""
        if self._on_change:
            try:
                await self._on_change()
            except Exception as e:
                logger.warning(f"Queue change notification failed: {e}")

    def submit(
        self,
        query: str,
        priority: TaskPriority = TaskPriority.NORMAL,
        intent: Optional[str] = None,
        confirmed: bool = False,
        intent_spec: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Add a task to the queue. Returns task_id immediately.
        Non-blocking — the task will be picked up by the process loop.

        Phase 39: Optional intent_spec is the captured "checkable constraints"
        for this task; the verifier reads it at task close to detect drift.
        """
        task = QueuedTask(
            id=uuid.uuid4().hex[:12],
            query=query,
            priority=priority,
            intent=intent,
            confirmed=confirmed,
            intent_spec=intent_spec,
        )

        # Insert in priority order (lower number = higher priority)
        # Within same priority, FIFO (earlier created_at goes first)
        inserted = False
        for i, existing in enumerate(self._pending):
            if task.priority.value < existing.priority.value:
                self._pending.insert(i, task)
                inserted = True
                break
        if not inserted:
            self._pending.append(task)

        logger.info(
            f"📥 QUEUED [{task.priority.name}] #{task.id}: "
            f"{task.query[:60]}... ({len(self._pending)} in queue)"
        )

        # Persist pending queue to disk
        self._save_pending()

        # Fire change notification (don't await in sync context — schedule it)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._notify_change())
        except RuntimeError:
            pass  # No running loop yet (startup)

        return task.id

    def submit_retry(self, original_task: "QueuedTask", error: str) -> Optional[str]:
        """
        Phase 29.5: Auto-retry a failed task with error context.
        Returns task_id or None if the original was already a retry (loop guard).
        """
        if original_task.is_auto_retry:
            logger.info(f"SKIP auto-retry for #{original_task.id} — already an auto-retry")
            return None

        retry_query = (
            f"{original_task.query}\n\n"
            f"[AUTO-RETRY] Previous attempt failed with: {error[:300]}. "
            f"Try a different approach."
        )
        task_id = self.submit(
            query=retry_query,
            priority=original_task.priority,
            intent=original_task.intent,
            confirmed=original_task.confirmed,
            # Phase 39: Inherit the parent's intent_spec — the retry exists to satisfy
            # the ORIGINAL goal, not the [AUTO-RETRY]-modified query.
            intent_spec=original_task.intent_spec,
        )
        # Mark the newly created task as an auto-retry
        for t in self._pending:
            if t.id == task_id:
                t.is_auto_retry = True
                t.parent_task_id = original_task.id
                break
        self._save_pending()
        logger.info(f"🔄 AUTO-RETRY queued #{task_id} (parent: #{original_task.id}): {error[:80]}")
        return task_id

    def cancel(self, task_id: str) -> bool:
        """Cancel a queued (not yet running) task."""
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
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._notify_change())
                except RuntimeError:
                    pass
                return True
        return False

    def cancel_running(self) -> Optional[str]:
        """
        Cancel the currently running task. Returns task_id if cancelled.
        The task's asyncio.Task gets cancelled, triggering CancelledError
        in the process loop which handles cleanup.
        """
        if not self._current:
            return None
        task_id = self._current.id
        if self._current_task_handle and not self._current_task_handle.done():
            self._current_task_handle.cancel()
            logger.info(f"🛑 ABORT requested for running task #{task_id}")
        return task_id

    def pause(self):
        """Pause queue processing. Current task finishes, but no new ones start."""
        self._paused = True
        logger.info("⏸️  Queue PAUSED — current task will finish, no new tasks will start")

    def resume(self):
        """Resume queue processing."""
        self._paused = False
        logger.info("▶️  Queue RESUMED")

    @property
    def is_paused(self) -> bool:
        return self._paused

    def get_task(self, task_id: str) -> Optional[Dict]:
        """Get the current state of any task (pending, running, or history)."""
        # Check current
        if self._current and self._current.id == task_id:
            return self._current.to_dict()
        # Check pending
        for task in self._pending:
            if task.id == task_id:
                return task.to_dict()
        # Check history
        for task in self._history:
            if task.id == task_id:
                return task.to_dict()
        return None

    def get_queue_state(self) -> Dict:
        """Full queue state for dashboard / API."""
        return {
            "pending": [t.to_dict() for t in self._pending],
            "current": self._current.to_dict() if self._current else None,
            "history": [t.to_dict() for t in reversed(self._history[-20:])],
            "stats": {
                "pending_count": len(self._pending),
                "is_processing": self._current is not None,
                "is_paused": self._paused,
                "total_completed": sum(
                    1 for t in self._history if t.state == TaskState.COMPLETED
                ),
                "total_failed": sum(
                    1 for t in self._history if t.state == TaskState.FAILED
                ),
            },
        }

    def _trim_history(self):
        """Keep in-memory history bounded and persist to disk."""
        if len(self._history) > self.MAX_HISTORY:
            self._history = self._history[-self.MAX_HISTORY:]
        self._save_history()

    # --- PERSISTENCE (Phase 19.1) ---

    def _load_history(self):
        """Load task history from disk on startup."""
        if not self._history_path.exists():
            logger.debug("No history file found, starting fresh")
            return
        try:
            with open(self._history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._history = [
                QueuedTask.from_dict(t) for t in data.get("tasks", [])
            ]
            # Trim to MAX_HISTORY for in-memory
            if len(self._history) > self.MAX_HISTORY:
                self._history = self._history[-self.MAX_HISTORY:]
            logger.info(
                f"📂 Loaded {len(self._history)} tasks from history file"
            )
        except Exception as e:
            logger.warning(f"Failed to load history: {e}")

    def _save_history(self):
        """Persist task history to disk."""
        try:
            # Load existing persisted history to merge (disk may have more than memory)
            existing = []
            if self._history_path.exists():
                try:
                    with open(self._history_path, "r", encoding="utf-8") as f:
                        existing = json.load(f).get("tasks", [])
                except (json.JSONDecodeError, KeyError):
                    existing = []

            # Build lookup of existing archived flags (disk is source of truth for archive state)
            existing_archived = {d["id"]: d.get("archived", False) for d in existing}
            
            # Merge: existing IDs + new from memory (dedup by id)
            seen_ids = set()
            merged = []
            # Memory tasks take priority (freshest state)
            for t in self._history:
                d = t.to_dict()
                # Preserve archived flag from disk if memory doesn't have it
                if not d.get("archived") and existing_archived.get(d["id"], False):
                    d["archived"] = True
                if d["id"] not in seen_ids:
                    seen_ids.add(d["id"])
                    merged.append(d)
            # Add older persisted tasks not in memory
            for d in existing:
                if d["id"] not in seen_ids:
                    seen_ids.add(d["id"])
                    merged.append(d)

            # Sort by completed_at descending, trim to MAX_PERSISTED
            merged.sort(
                key=lambda x: x.get("completed_at") or x.get("created_at") or 0,
                reverse=True,
            )
            merged = merged[:self.MAX_PERSISTED]

            # Ensure directory exists
            self._history_path.parent.mkdir(parents=True, exist_ok=True)

            with open(self._history_path, "w", encoding="utf-8") as f:
                json.dump({"tasks": merged, "updated_at": time.time()}, f, indent=2)

        except Exception as e:
            logger.warning(f"Failed to save history: {e}")

    def _save_pending(self):
        """Persist pending queue to disk for crash recovery."""
        try:
            self._pending_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "pending": [t.to_dict() for t in self._pending],
                "updated_at": time.time(),
            }
            with open(self._pending_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save pending queue: {e}")

    def _load_pending(self):
        """Load pending queue from disk on startup (crash recovery)."""
        if not self._pending_path.exists():
            return
        try:
            with open(self._pending_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            restored = [
                QueuedTask.from_dict(t) for t in data.get("pending", [])
            ]
            if restored:
                # Merge with any existing pending (from Watchtower restore)
                existing_ids = {t.id for t in self._pending}
                for t in restored:
                    if t.id not in existing_ids:
                        self._pending.append(t)
                # Re-sort by priority
                self._pending.sort(key=lambda t: (t.priority.value, t.created_at))
                logger.info(
                    f"📂 Restored {len(restored)} pending tasks from disk"
                )
            # Clear the file after successful restore
            self._pending_path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to load pending queue: {e}")

    def get_full_history(self, limit: int = 50, offset: int = 0, include_archived: bool = False) -> List[Dict]:
        """Read persisted history for dashboard (supports pagination)."""
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
        """Mark a task as archived in persistent history."""
        return self._set_archive_flag(task_id, True)

    def unarchive_task(self, task_id: str) -> bool:
        """Unarchive a task in persistent history."""
        return self._set_archive_flag(task_id, False)

    def archive_all(self) -> int:
        """Archive all completed/failed tasks. Returns count archived."""
        try:
            if not self._history_path.exists():
                return 0
            with open(self._history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            count = 0
            for task in data.get("tasks", []):
                if not task.get("archived", False):
                    task["archived"] = True
                    count += 1
            with open(self._history_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return count
        except Exception as e:
            logger.warning(f"Failed to archive all: {e}")
            return 0

    def _set_archive_flag(self, task_id: str, archived: bool) -> bool:
        """Set archive flag on a task in persistent history."""
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

    # --- SNAPSHOT / RESTORE (Watchtower durability) ---

    def snapshot(self) -> Dict:
        """Serialize full queue state for Watchtower backup."""
        return {
            "pending": [t.to_dict() for t in self._pending],
            "current": self._current.to_dict() if self._current else None,
            "history": [t.to_dict() for t in self._history],
            "timestamp": time.time(),
        }

    def restore(self, data: Dict):
        """Restore queue state from Watchtower snapshot."""
        if not data:
            return

        # Restore pending tasks
        self._pending = [
            QueuedTask.from_dict(t) for t in data.get("pending", [])
        ]

        # If a task was running when we died, re-queue it
        current_data = data.get("current")
        if current_data and current_data["state"] == TaskState.RUNNING.value:
            requeued = QueuedTask.from_dict(current_data)
            requeued.state = TaskState.QUEUED
            requeued.started_at = None
            # Put it at the front of its priority band
            self._pending.insert(0, requeued)
            logger.info(f"🔄 Re-queued interrupted task #{requeued.id}")

        # Restore history
        self._history = [
            QueuedTask.from_dict(t) for t in data.get("history", [])
        ]

        logger.info(
            f"📦 Queue restored: {len(self._pending)} pending, "
            f"{len(self._history)} in history"
        )

    # --- PROCESSING LOOP ---

    async def process_loop(self):
        """
        Background loop that pulls tasks from the queue and executes them.
        Run this with asyncio.create_task() on startup.
        """
        if not self._executor:
            raise RuntimeError("No executor set! Call set_executor() first.")

        self._running = True
        logger.info("🔄 Task queue processing loop started")

        while self._running:
            # Kill switch check — blocks all autonomous task execution
            if _kill_switch_engaged():
                if not self._paused:  # Only log transition once
                    logger.warning("🛑 Kill switch engaged — task queue frozen")
                    self._paused = True
                await asyncio.sleep(2)
                continue
            
            if not self._pending or self._paused:
                await asyncio.sleep(1)
                continue
            
            # Execution budget check — pause if hourly cap reached
            budget_ok, budget_info = _check_budget()
            if not budget_ok:
                resets_in = budget_info.get('window_resets_in', 0)
                logger.warning(
                    f"📊 Execution budget exhausted "
                    f"({budget_info['used']}/{budget_info['max']} this hour) "
                    f"— pausing for {resets_in}s"
                )
                await asyncio.sleep(min(30, max(5, resets_in)))  # Re-check periodically
                continue

            # Pop highest priority task
            task = self._pending.pop(0)
            task.state = TaskState.RUNNING
            task.started_at = time.time()
            self._current = task
            self._save_pending()  # Persist remaining queue

            logger.info(
                f"▶️  EXECUTING [{task.priority.name}] #{task.id}: "
                f"{task.query[:60]}..."
            )
            _bb_log("task_start", {
                "task_id": task.id, "query": task.query[:200],
                "priority": task.priority.name, "intent": task.intent,
            }, source="task_queue")
            await self._notify_change()

            try:
                # Wrap executor in a task so cancel_running() can cancel it
                self._current_task_handle = asyncio.current_task()
                result = await self._executor(task)
                task.result = result
                task.state = (
                    TaskState.COMPLETED if result.get("success") else TaskState.FAILED
                )
                if not result.get("success"):
                    task.error = result.get("error", "Unknown error")
            except asyncio.CancelledError:
                task.state = TaskState.CANCELLED
                logger.info(f"🚫 Task #{task.id} cancelled during execution")
                # Re-raise would kill the loop — we handle it and continue
            except Exception as e:
                task.state = TaskState.FAILED
                task.error = str(e)
                logger.error(f"❌ Task #{task.id} crashed: {e}")

            task.completed_at = time.time()
            duration = task.completed_at - task.started_at
            emoji = "✅" if task.state == TaskState.COMPLETED else "❌" if task.state == TaskState.FAILED else "🚫"
            logger.info(f"{emoji} #{task.id} {task.state.value} in {duration:.1f}s")

            self._current = None
            self._current_task_handle = None
            self._history.append(task)
            self._trim_history()
            
            # Record execution against budget (success or failure both count)
            _record_execution()
            
            # Black box audit trail
            _bb_log("task_complete" if task.state == TaskState.COMPLETED else "task_failed", {
                "task_id": task.id, "query": task.query[:200],
                "state": task.state.value, "duration": round(duration, 1),
                "error": task.error[:300] if task.error else None,
            }, source="task_queue")
            
            await self._notify_change()

    def stop(self):
        """Stop the processing loop gracefully."""
        self._running = False
        logger.info("⏹️  Task queue processing loop stopped")
