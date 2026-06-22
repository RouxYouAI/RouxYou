"""
notify_dj — best-effort, soft-fail notification hook for events that need attention.
====================================================================
SOFT-FAIL by design: a notification must NEVER break the thing it reports on
(wrapped in try/except, returns bool). Keep messages to one line — these are
pings, not reports.

Default implementation logs only. Wire `_send()` to your own channel
(Slack / ntfy / webhook / etc.) to actually deliver notifications.
"""

from shared.logger import get_logger

logger = get_logger("notify")

_PREFIX = {"critical": "🚨", "warn": "⚠️", "normal": "🔔"}


def notify_dj(message: str, priority: str = "normal") -> bool:
    """Post a one-line attention notification. Returns True on success, else False.
    NEVER raises. priority ∈ {critical, warn, normal} (just sets an emoji prefix)."""
    try:
        emoji = _PREFIX.get(priority, "🔔")
        logger.info(f"notify ({priority}): {message[:80]}")
        return _send(f"{emoji} [Roux] {message}")
    except Exception as e:
        logger.warning(f"notify failed (soft): {e}")
        return False


def _send(line: str) -> bool:
    """Deliver `line` to your notification channel. Stub: logs only, returns True.
    Implement against your preferred service (Slack / ntfy / webhook / etc.)."""
    return True
