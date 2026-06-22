"""
RouxYou — Event Bus with SSE Support (Phase 37)
=================================================
Central publish/subscribe system for real-time events.
Supports in-process subscribers AND Server-Sent Events for remote consumers.

Architecture:
    Agents → event_bus.publish(event) → subscribers get notified
                                      → SSE endpoint streams to dashboard
                                      → activity.json still updated (backward compat)

Usage:
    from shared.event_bus import event_bus
    from shared.stream_events import StreamEvent

    # Publishing (from agents):
    event_bus.publish(StreamEvent.thought("coder", "Planning task..."))

    # Subscribing (in-process):
    async def my_handler(event: StreamEvent):
        print(f"Got event: {event.type}")
    event_bus.subscribe(my_handler)

    # SSE endpoint (in FastAPI):
    from shared.event_bus import create_sse_endpoint
    app.add_api_route("/events", create_sse_endpoint(), methods=["GET"])
"""

import asyncio
import json
import logging
import time
from typing import Callable, Optional, Any
from collections import deque

from shared.stream_events import StreamEvent, EventType

logger = logging.getLogger("roux.events")


class EventBus:
    """
    Central event bus for RouxYou.
    Thread-safe publish, async-safe subscribe.
    Maintains a rolling buffer of recent events for late-joining subscribers.
    """

    def __init__(self, buffer_size: int = 100):
        self._subscribers: list[Callable] = []
        self._async_queues: list[asyncio.Queue] = []
        self._buffer: deque[StreamEvent] = deque(maxlen=buffer_size)
        self._event_count: int = 0

    def publish(self, event: StreamEvent):
        """
        Publish an event to all subscribers.
        Safe to call from sync or async context.
        """
        self._buffer.append(event)
        self._event_count += 1

        # Notify sync subscribers
        for handler in self._subscribers:
            try:
                handler(event)
            except Exception as e:
                logger.error(f"Event handler error: {e}")

        # Notify async subscribers (SSE queues)
        dead_queues = []
        for queue in self._async_queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Queue full — drop oldest events (consumer is slow)
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except Exception:
                    dead_queues.append(queue)
            except Exception:
                dead_queues.append(queue)

        # Clean up dead queues
        for q in dead_queues:
            self._async_queues.remove(q)

    def subscribe(self, handler: Callable):
        """Register a synchronous event handler."""
        self._subscribers.append(handler)

    def unsubscribe(self, handler: Callable):
        """Remove a synchronous event handler."""
        try:
            self._subscribers.remove(handler)
        except ValueError:
            pass

    def subscribe_async(self, max_queue_size: int = 64) -> asyncio.Queue:
        """
        Create an async queue for SSE consumers.
        Returns a queue that receives StreamEvent objects.
        Call unsubscribe_async(queue) when done.
        """
        queue = asyncio.Queue(maxsize=max_queue_size)
        self._async_queues.append(queue)
        return queue

    def unsubscribe_async(self, queue: asyncio.Queue):
        """Remove an async subscriber queue."""
        try:
            self._async_queues.remove(queue)
        except ValueError:
            pass

    def recent(self, n: int = 20) -> list[dict]:
        """Get the N most recent events as dicts."""
        events = list(self._buffer)[-n:]
        return [e.to_dict() for e in events]

    def status(self) -> dict:
        """Bus status for health checks."""
        return {
            "total_events": self._event_count,
            "buffer_size": len(self._buffer),
            "sync_subscribers": len(self._subscribers),
            "async_subscribers": len(self._async_queues),
        }


# ============================================================
# Module-level singleton
# ============================================================

event_bus = EventBus()


# ============================================================
# SSE Endpoint Factory (for FastAPI)
# ============================================================

def create_sse_endpoint():
    """
    Create a FastAPI SSE endpoint function.

    Usage in orchestrator.py:
        from shared.event_bus import create_sse_endpoint
        app.add_api_route("/events/stream", create_sse_endpoint(), methods=["GET"])

    Client side (JavaScript):
        const es = new EventSource("http://localhost:8001/events/stream");
        es.onmessage = (e) => { const event = JSON.parse(e.data); ... };
    """
    async def sse_endpoint():
        from starlette.responses import StreamingResponse

        queue = event_bus.subscribe_async()

        async def event_generator():
            try:
                # Send recent events as initial burst
                for event_dict in event_bus.recent(10):
                    yield f"data: {json.dumps(event_dict)}\n\n"

                # Stream live events
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=30)
                        yield f"data: {event.to_json()}\n\n"
                    except asyncio.TimeoutError:
                        # Send heartbeat to keep connection alive
                        heartbeat = StreamEvent.heartbeat()
                        yield f"data: {heartbeat.to_json()}\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                event_bus.unsubscribe_async(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )

    return sse_endpoint


# ============================================================
# REST Endpoint Factory (for polling fallback)
# ============================================================

def create_events_rest_endpoint():
    """
    Create a FastAPI REST endpoint for event polling (fallback for SSE).

    Usage:
        app.add_api_route("/events/recent", create_events_rest_endpoint(), methods=["GET"])
    """
    async def events_endpoint(n: int = 20):
        return {
            "events": event_bus.recent(n),
            "bus_status": event_bus.status(),
        }
    return events_endpoint
