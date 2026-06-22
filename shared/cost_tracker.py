"""
RouxYou — Cost & Token Usage Tracker (Phase 35)
================================================
Records token usage per LLM call, aggregates by task/session/day.
Essential for monitoring costs when Claude Big Brain is active.

Usage:
    from shared.cost_tracker import tracker, record_usage

    # Record from an LLMResponse (automatic via shared/llm.py)
    record_usage(llm_response.usage)

    # Get stats
    tracker.session_total()   # {"input_tokens": ..., "output_tokens": ..., "cost_usd": ...}
    tracker.daily_total()     # Same, filtered to today
    tracker.by_provider()     # Breakdown per provider
"""

import json
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from datetime import datetime

logger = logging.getLogger("roux.cost")


# ============================================================
# Pricing Table (per 1M tokens, April 2026)
# ============================================================

PRICING = {
    # Anthropic Claude
    "claude-opus-4-6":    {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6":  {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5":   {"input": 0.80,  "output": 4.00},
    # Local models (free)
    "ollama/*":           {"input": 0.00,  "output": 0.00},
    "vllm/*":             {"input": 0.00,  "output": 0.00},
}


def get_pricing(model: str) -> dict:
    """Get pricing for a model. Falls back to free for unknown models."""
    if model in PRICING:
        return PRICING[model]
    # Check wildcard matches
    for pattern, price in PRICING.items():
        if pattern.endswith("/*"):
            prefix = pattern[:-2]
            if model.startswith(prefix) or prefix in ("ollama", "vllm"):
                return price
    return {"input": 0.00, "output": 0.00}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for a single LLM call."""
    pricing = get_pricing(model)
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return input_cost + output_cost


def format_usd(amount: float) -> str:
    """Format a USD amount for display."""
    if amount == 0:
        return "free"
    if amount < 0.01:
        return f"${amount:.4f}"
    return f"${amount:.2f}"


# ============================================================
# Usage Event
# ============================================================

@dataclass
class UsageEvent:
    """A single LLM usage record."""
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    timestamp: float = 0.0
    task_id: str = ""
    caller: str = ""  # "coder", "companion", "coach", etc.

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.cost_usd:
            self.cost_usd = estimate_cost(self.model, self.input_tokens, self.output_tokens)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "caller": self.caller,
        }


# ============================================================
# Cost Tracker (Singleton)
# ============================================================

class CostTracker:
    """
    In-memory token usage tracker with periodic persistence.
    Survives service restarts via JSON log file.
    """

    def __init__(self, log_dir: Optional[str] = None):
        self.events: list[UsageEvent] = []
        self._session_start = time.time()

        if log_dir:
            self._log_path = Path(log_dir) / "usage_log.json"
        else:
            self._log_path = Path(__file__).parent.parent / "state" / "usage_log.json"

    def record(self, event: UsageEvent):
        """Record a usage event."""
        self.events.append(event)

        # Log if it cost something
        if event.cost_usd > 0:
            logger.info(
                f"LLM cost: {format_usd(event.cost_usd)} "
                f"({event.model}, {event.input_tokens}in/{event.output_tokens}out)"
            )

    def session_total(self) -> dict:
        """Aggregate totals for the current session."""
        return self._aggregate(self.events)

    def daily_total(self) -> dict:
        """Aggregate totals for today."""
        today = datetime.now().strftime("%Y-%m-%d")
        daily_events = [
            e for e in self.events
            if datetime.fromtimestamp(e.timestamp).strftime("%Y-%m-%d") == today
        ]
        return self._aggregate(daily_events)

    def by_provider(self) -> dict:
        """Breakdown by provider."""
        providers = {}
        for e in self.events:
            if e.provider not in providers:
                providers[e.provider] = []
            providers[e.provider].append(e)
        return {p: self._aggregate(events) for p, events in providers.items()}

    def by_caller(self) -> dict:
        """Breakdown by caller (coder, companion, etc.)."""
        callers = {}
        for e in self.events:
            key = e.caller or "unknown"
            if key not in callers:
                callers[key] = []
            callers[key].append(e)
        return {c: self._aggregate(events) for c, events in callers.items()}

    def recent(self, n: int = 10) -> list[dict]:
        """Last N usage events."""
        return [e.to_dict() for e in self.events[-n:]]

    def save(self):
        """Persist events to disk."""
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)

            # Append-style: load existing, merge, save
            existing = []
            if self._log_path.exists():
                try:
                    existing = json.loads(self._log_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, Exception):
                    existing = []

            all_events = existing + [e.to_dict() for e in self.events]

            # Keep last 7 days max
            cutoff = time.time() - (7 * 86400)
            all_events = [e for e in all_events if e.get("timestamp", 0) > cutoff]

            self._log_path.write_text(
                json.dumps(all_events, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Failed to save usage log: {e}")

    def status(self) -> dict:
        """Summary for dashboard/health checks."""
        session = self.session_total()
        daily = self.daily_total()
        return {
            "session": session,
            "daily": daily,
            "events_count": len(self.events),
            "session_uptime_minutes": int((time.time() - self._session_start) / 60),
        }

    @staticmethod
    def _aggregate(events: list) -> dict:
        """Aggregate a list of UsageEvents."""
        total_in = sum(e.input_tokens for e in events)
        total_out = sum(e.output_tokens for e in events)
        total_cost = sum(e.cost_usd for e in events)
        return {
            "input_tokens": total_in,
            "output_tokens": total_out,
            "total_tokens": total_in + total_out,
            "cost_usd": total_cost,
            "cost_display": format_usd(total_cost),
            "calls": len(events),
        }


# ============================================================
# Module-level singleton & convenience functions
# ============================================================

tracker = CostTracker()


def record_usage(token_usage, caller: str = "", task_id: str = ""):
    """
    Record usage from a TokenUsage object (from shared/llm.py).

    Usage:
        from shared.cost_tracker import record_usage
        response = await llm_generate("coder", prompt="...")
        if response.usage:
            record_usage(response.usage, caller="coder")
    """
    if token_usage is None:
        return

    event = UsageEvent(
        provider=getattr(token_usage, "provider", ""),
        model=getattr(token_usage, "model", ""),
        input_tokens=getattr(token_usage, "input_tokens", 0),
        output_tokens=getattr(token_usage, "output_tokens", 0),
        task_id=task_id,
        caller=caller,
    )
    tracker.record(event)
