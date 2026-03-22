import json
import time
import uuid
import sys
import os
from pathlib import Path
from typing import List, Optional

try:
    from .schemas import Task, TaskStatus, TaskType
except ImportError:
    BASE_DIR = Path(__file__).parent.parent
    sys.path.append(str(BASE_DIR))
    from shared.schemas import Task, TaskStatus, TaskType

BASE_DIR = Path(__file__).parent.parent
TASK_FILE = BASE_DIR / "tasks.json"


class TaskRegistry:
    def __init__(self):
        self.tasks: List[Task] = []
        self._load()

    def _load(self):
        if TASK_FILE.exists():
            try:
                with open(TASK_FILE, "r") as f:
                    data = json.load(f)
                    self.tasks = [Task(**t) for t in data]
            except Exception as e:
                print(f"REGISTRY: Corrupt task file, starting fresh. ({e})")
                self.tasks = []

    def save(self):
        with open(TASK_FILE, "w") as f:
            data = [t.model_dump() if hasattr(t, 'model_dump') else t.dict() for t in self.tasks]
            json.dump(data, f, indent=2)

    def add_task(self, title: str, type: TaskType, priority: int = 1,
                 description: str = "", auto_approve: bool = False) -> Task:
        for t in self.tasks:
            if t.title == title and t.status == TaskStatus.PENDING_APPROVAL:
                if t.priority < 10:
                    t.priority = max(t.priority, priority)
                    self.save()
                return t

        status = TaskStatus.APPROVED if auto_approve else TaskStatus.PENDING_APPROVAL
        new_task = Task(
            id=str(uuid.uuid4())[:8],
            title=title,
            description=description,
            type=type,
            priority=priority,
            status=status,
            created_at=time.time()
        )
        self.tasks.append(new_task)
        self.save()
        return new_task

    def get_next_approved_task(self) -> Optional[Task]:
        approved = [t for t in self.tasks if t.status == TaskStatus.APPROVED]
        if not approved:
            return None
        return sorted(approved, key=lambda t: t.priority, reverse=True)[0]

    def update_status(self, task_id: str, status: TaskStatus):
        for t in self.tasks:
            if t.id == task_id:
                t.status = status
                self.save()
                return
        print(f"REGISTRY: Task ID {task_id} not found.")

    def block_task(self, task_id: str, reason: str, question: str):
        for t in self.tasks:
            if t.id == task_id:
                t.status = TaskStatus.BLOCKED
                t.blocked_reason = reason
                t.blocked_question = question
                self.save()
                return

    def get_task_by_id(self, task_id: str) -> Optional[Task]:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None
