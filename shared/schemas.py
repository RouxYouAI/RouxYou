"""
SHARED SCHEMAS
Strict Pydantic models to ensure agents share state and memory.
"""
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Dict, Any
from enum import Enum

class TaskContext(BaseModel):
    working_dir: str = Field(default=".", description="Current active directory")
    active_file: Optional[str] = Field(None, description="The specific file path being modified")
    detected_errors: List[str] = Field(default_factory=list)
    verified: bool = Field(default=False)
    metadata: Dict[str, Any] = Field(default_factory=dict)

class SkillRecord(BaseModel):
    name: str
    description: str = ""
    code_pattern: str = ""
    dependencies: List[str] = Field(default_factory=list)
    times_used: int = 0
    times_succeeded: int = 0

class EpisodicMemory(BaseModel):
    timestamp: float
    task_query: str
    plan_summary: str
    working_dir: str
    affected_files: List[str]
    success: bool
    plan_steps: Optional[List[Dict[str, Any]]] = None
    execution_results: Optional[List[Dict[str, Any]]] = None
    code_artifacts: Optional[Dict[str, str]] = None
    utility: float = Field(default=0.5)
    reuse_count: int = 0
    reuse_successes: int = 0
    skills: Optional[List[str]] = None

class Step(BaseModel):
    id: int
    action: str
    details: str
    reasoning: Optional[str] = None
    content: Optional[str] = None
    context_update: Optional[Dict[str, Any]] = None

    @field_validator('details', mode='before')
    @classmethod
    def coerce_details_to_str(cls, v):
        if isinstance(v, dict):
            if 'filepath' in v:
                return v['filepath']
            return str(v)
        return v

class AgentPlan(BaseModel):
    steps: List[Step]
    estimated_complexity: str = "medium"
    reasoning: str
    initial_context: Optional[TaskContext] = None

class ExecutionResult(BaseModel):
    success: bool
    summary: str
    results: List[Dict[str, Any]] = []
    error: Optional[str] = None
    final_context: Optional[TaskContext] = None

class AgentMessage(BaseModel):
    task: str
    sender: str
    recipient: str
    context: Dict[str, Any] = {}
    data: Dict[str, Any] = {}

class TaskType(str, Enum):
    USER_GOAL = "user_goal"
    SYSTEM_HEALTH = "health"
    RESEARCH = "research"
    MAINTENANCE = "maintenance"

class TaskStatus(str, Enum):
    PENDING_APPROVAL = "pending"
    APPROVED = "approved"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"

class Task(BaseModel):
    id: str
    title: str
    description: str
    type: TaskType
    priority: int = 1
    status: TaskStatus = TaskStatus.PENDING_APPROVAL
    created_at: float
    context: Optional[Dict[str, Any]] = {}
    blocked_reason: Optional[str] = None
    blocked_question: Optional[str] = None
    user_response: Optional[str] = None
    retry_count: int = 0
