"""
Communication utilities for inter-agent messaging.
Includes SessionMemory for shared context (Blackboard Pattern).
"""

import requests
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field
from datetime import datetime
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import CONFIG


class SessionMemory:
    """
    Shared context store for all agents in a session.
    Singleton pattern ensures consistency across the system.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SessionMemory, cls).__new__(cls)
            cls._instance.store = {
                "short_term": {},
                "history": []
            }
        return cls._instance

    def update(self, key: str, value: Any):
        self.store["short_term"][key] = value

    def update_many(self, updates: Dict[str, Any]):
        self.store["short_term"].update(updates)

    def get(self, key: str, default=None):
        return self.store["short_term"].get(key, default)

    def add_history(self, entry: str, agent: str = "unknown", operation: str = None):
        self.store["history"].append({
            "timestamp": datetime.now().isoformat(),
            "agent": agent,
            "operation": operation,
            "entry": entry
        })

    def get_context(self) -> Dict:
        return self.store["short_term"].copy()

    def get_history(self, limit: int = 10) -> List[Dict]:
        return self.store["history"][-limit:]

    def clear_short_term(self):
        self.store["short_term"].clear()

    def clear_all(self):
        self.store["short_term"].clear()
        self.store["history"].clear()


class Message(BaseModel):
    sender: str
    recipient: str
    task: str
    context: Optional[Dict[str, Any]] = None
    data: Optional[Any] = None
    timestamp: datetime = Field(default_factory=datetime.now)


class AgentCommunicator:
    """Handles HTTP communication between agents with shared memory."""

    # Ports sourced from CONFIG — not hardcoded
    AGENT_PORTS = {
        "orchestrator": CONFIG.PORT_ORCHESTRATOR,
        "coder":         CONFIG.PORT_CODER,
        "worker":        CONFIG.PORT_WORKER,
        "memory":        CONFIG.PORT_MEMORY,
    }

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self.base_url = f"http://localhost:{self.AGENT_PORTS[agent_name]}"
        self.memory = SessionMemory()

    def send_message(self, recipient: str, task: str,
                     context: Optional[Dict] = None, data: Any = None) -> Dict:
        recipient_port = self.AGENT_PORTS.get(recipient)
        if not recipient_port:
            return {"success": False, "error": f"Unknown recipient: {recipient}"}

        full_context = self.memory.get_context()
        if context:
            full_context.update(context)

        message = Message(
            sender=self.agent_name,
            recipient=recipient,
            task=task,
            context=full_context,
            data=data
        )

        url = f"http://localhost:{recipient_port}/message"
        try:
            response = requests.post(
                url,
                json=message.model_dump(mode='json'),
                timeout=180
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": f"Communication Error: {str(e)}"}

    def get_health(self, agent: str) -> bool:
        port = self.AGENT_PORTS.get(agent)
        if not port:
            return False
        try:
            response = requests.get(f"http://localhost:{port}/health", timeout=2)
            return response.status_code == 200
        except:
            return False


def get_communicator(agent_name: str) -> AgentCommunicator:
    return AgentCommunicator(agent_name)


def get_memory() -> SessionMemory:
    return SessionMemory()
