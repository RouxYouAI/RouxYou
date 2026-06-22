"""
Lightweight distributed tracing for RouxYou.
No external dependencies — just a trace_id header propagated through services.

Usage:
  - Gateway generates trace_id on each request
  - Services extract it from X-Trace-Id header
  - Logger includes it when available
  - Enables correlating logs across gateway → orchestrator → coder → worker
"""

import uuid
import contextvars

# Context variable for the current trace ID (async-safe)
_current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default=""
)

HEADER_NAME = "X-Trace-Id"


def generate_trace_id() -> str:
    """Generate a short, readable trace ID."""
    return uuid.uuid4().hex[:12]


def get_trace_id() -> str:
    """Get the current trace ID (empty string if none set)."""
    return _current_trace_id.get()


def set_trace_id(trace_id: str):
    """Set the trace ID for the current async context."""
    _current_trace_id.set(trace_id)
