"""
RouxYou — Structured Event Protocol (Phase 37)
================================================
Typed events replacing free-form activity broadcasting.
Provides a stable contract for dashboard, mobile clients, and SSE consumers.

Usage:
    from shared.stream_events import StreamEvent, EventType

    event = StreamEvent.thought("orchestrator", "Planning task...")
    event = StreamEvent.task_started("orchestrator", task_id="abc", title="Fix worker")
    event = StreamEvent.step_completed("worker", step=2, total=5, description="File written")

    # Serialize for SSE/JSON
    event.to_dict()
    event.to_json()
"""

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any


class EventType(str, Enum):
    """All possible event types in the system."""

    # Task lifecycle
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"

    # Agent narration
    THOUGHT = "thought"
    PLAN_CREATED = "plan_created"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STATUS_CHANGED = "status_changed"

    # Tool execution
    TOOL_CALLED = "tool_called"
    TOOL_RESULT = "tool_result"

    # System events
    SERVICE_HEALTH = "service_health"
    PROPOSAL_CREATED = "proposal_created"
    PROPOSAL_APPROVED = "proposal_approved"
    PROPOSAL_EXECUTED = "proposal_executed"
    ERROR = "error"

    # LLM events (Phase 35 cost tracking)
    LLM_CALL = "llm_call"

    # Heartbeat
    HEARTBEAT = "heartbeat"


@dataclass
class StreamEvent:
    """
    A single typed event in the RouxYou event stream.
    Immutable after creation — create new events, don't mutate.
    """
    type: EventType
    agent: str              # orchestrator, coder, worker, watchtower, roux, system
    timestamp: float = 0.0
    task_id: str = ""
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "agent": self.agent,
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "data": self.data,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @staticmethod
    def from_dict(d: dict) -> "StreamEvent":
        return StreamEvent(
            type=EventType(d["type"]),
            agent=d["agent"],
            timestamp=d.get("timestamp", 0),
            task_id=d.get("task_id", ""),
            data=d.get("data", {}),
        )

    # ── Factory methods for common events ──────────────────────

    @staticmethod
    def task_started(agent: str, task_id: str = "", title: str = "") -> "StreamEvent":
        return StreamEvent(
            type=EventType.TASK_STARTED, agent=agent,
            task_id=task_id, data={"title": title}
        )

    @staticmethod
    def task_completed(agent: str, task_id: str = "", success: bool = True,
                       summary: str = "") -> "StreamEvent":
        return StreamEvent(
            type=EventType.TASK_COMPLETED if success else EventType.TASK_FAILED,
            agent=agent, task_id=task_id,
            data={"success": success, "summary": summary}
        )

    @staticmethod
    def thought(agent: str, message: str, task_id: str = "") -> "StreamEvent":
        return StreamEvent(
            type=EventType.THOUGHT, agent=agent,
            task_id=task_id, data={"message": message}
        )

    @staticmethod
    def plan_created(agent: str, steps: list[str], task_id: str = "") -> "StreamEvent":
        return StreamEvent(
            type=EventType.PLAN_CREATED, agent=agent,
            task_id=task_id, data={"steps": steps, "total": len(steps)}
        )

    @staticmethod
    def step_started(agent: str, step: int, total: int = 0,
                     description: str = "", task_id: str = "") -> "StreamEvent":
        return StreamEvent(
            type=EventType.STEP_STARTED, agent=agent,
            task_id=task_id,
            data={"step": step, "total": total, "description": description}
        )

    @staticmethod
    def step_completed(agent: str, step: int, total: int = 0,
                       description: str = "", success: bool = True,
                       task_id: str = "") -> "StreamEvent":
        return StreamEvent(
            type=EventType.STEP_COMPLETED, agent=agent,
            task_id=task_id,
            data={"step": step, "total": total, "description": description,
                  "success": success}
        )

    @staticmethod
    def status_changed(agent: str, status: str, task_id: str = "") -> "StreamEvent":
        return StreamEvent(
            type=EventType.STATUS_CHANGED, agent=agent,
            task_id=task_id, data={"status": status}
        )

    @staticmethod
    def tool_called(agent: str, tool: str, target: str = "",
                    task_id: str = "") -> "StreamEvent":
        return StreamEvent(
            type=EventType.TOOL_CALLED, agent=agent,
            task_id=task_id, data={"tool": tool, "target": target}
        )

    @staticmethod
    def tool_result(agent: str, tool: str, success: bool = True,
                    summary: str = "", task_id: str = "") -> "StreamEvent":
        return StreamEvent(
            type=EventType.TOOL_RESULT, agent=agent,
            task_id=task_id,
            data={"tool": tool, "success": success, "summary": summary}
        )

    @staticmethod
    def service_health(service: str, healthy: bool, port: int = 0) -> "StreamEvent":
        return StreamEvent(
            type=EventType.SERVICE_HEALTH, agent="system",
            data={"service": service, "healthy": healthy, "port": port}
        )

    @staticmethod
    def error(agent: str, message: str, task_id: str = "") -> "StreamEvent":
        return StreamEvent(
            type=EventType.ERROR, agent=agent,
            task_id=task_id, data={"message": message}
        )

    @staticmethod
    def heartbeat() -> "StreamEvent":
        return StreamEvent(type=EventType.HEARTBEAT, agent="system")

    @staticmethod
    def llm_call(agent: str, provider: str = "", model: str = "",
                 input_tokens: int = 0, output_tokens: int = 0,
                 cost_usd: float = 0.0) -> "StreamEvent":
        return StreamEvent(
            type=EventType.LLM_CALL, agent=agent,
            data={
                "provider": provider, "model": model,
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "cost_usd": cost_usd,
            }
        )
