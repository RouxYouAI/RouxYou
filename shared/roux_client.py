"""
Roux Client — Shared helper for notifying Roux Voice Service
=============================================================
Import this in any service that needs to speak to Roux.
All calls are fire-and-forget (non-blocking, fail-silent).

Usage:
    from shared.roux_client import roux

    # Raw event (gets summarized by Ministral)
    await roux.event("watchtower", "service_crash", priority="critical",
                     data={"service": "coder"})

    # Direct speech (skip LLM, speak exactly this)
    await roux.say("Three tasks done, all clean.")

    # Convenience methods
    await roux.task_complete(agent="coder", summary="Dashboard fix applied")
    await roux.task_failed(agent="worker", error="File not found")
    await roux.service_crash(service="memory", restarting=True)
    await roux.service_restarted(service="memory", took_seconds=4)
    await roux.kill_switch(engaged=True, reason="Manual")
    await roux.deploy_staged(service="worker", version="1.4")
    await roux.deploy_complete(service="worker", version="1.4")
    await roux.deploy_rolled_back(service="worker", reason="health failures")
    await roux.proposal_auto_approved(title="Restart memory agent", executor="watchtower")
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("roux_client")

ROUX_URL = "http://localhost:8014"

# Try to use httpx (async), fall back to aiohttp, fall back to requests
_client_lib = None

try:
    import httpx
    _client_lib = "httpx"
except ImportError:
    try:
        import aiohttp
        _client_lib = "aiohttp"
    except ImportError:
        import requests
        _client_lib = "requests"


async def _post(endpoint: str, payload: dict, timeout: float = 10.0):
    """Fire-and-forget POST to Roux. Never raises — just logs on failure."""
    url = f"{ROUX_URL}{endpoint}"
    try:
        if _client_lib == "httpx":
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                return resp.json()

        elif _client_lib == "aiohttp":
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status < 400:
                        return await resp.json()
                    else:
                        logger.warning(f"Roux returned {resp.status}")
                        return None

        else:
            # Sync fallback — run in executor to avoid blocking
            import requests
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: requests.post(url, json=payload, timeout=timeout)
            )
            resp.raise_for_status()
            return resp.json()

    except Exception as e:
        logger.debug(f"Roux notification failed ({endpoint}): {e}")
        return None


class RouxClient:
    """Ergonomic wrapper around the Roux /event and /speak endpoints."""

    # --- Core Methods ---

    async def event(self, source: str, event_type: str,
                    priority: str = "normal", data: dict = None,
                    message: str = None):
        """Send a raw event to Roux for LLM summarization + speech."""
        return await _post("/event", {
            "source": source,
            "event_type": event_type,
            "priority": priority,
            "data": data or {},
            "message": message,
        })

    async def say(self, text: str, priority: str = "normal"):
        """Speak exact text (bypasses Ministral summarization)."""
        return await _post("/speak", {
            "text": text,
            "priority": priority,
        })

    # --- Task Lifecycle ---

    async def task_complete(self, agent: str = "system", summary: str = ""):
        return await self.event("orchestrator", "task_complete",
                                data={"agent": agent, "summary": summary},
                                message=f"{agent} completed: {summary}")

    async def task_failed(self, agent: str = "system", error: str = "",
                          query: str = "", task_id: str = "", intent: str = ""):
        """Phase 29.5: Extended with query/task_id/intent for follow-up dispatch."""
        return await self.event("orchestrator", "task_failed",
                                priority="normal",
                                data={"agent": agent, "error": error,
                                      "query": query, "task_id": task_id, "intent": intent},
                                message=f"{agent} failed: {error}")

    # --- Infrastructure ---

    async def service_crash(self, service: str, restarting: bool = False):
        msg = f"{service} crashed"
        if restarting:
            msg += " — restarting now"
        return await self.event("watchtower", "service_crash",
                                priority="critical",
                                data={"service": service, "restarting": restarting},
                                message=msg)

    async def service_restarted(self, service: str, took_seconds: int = None):
        msg = f"{service} is back online"
        if took_seconds:
            msg += f" ({took_seconds}s)"
        return await self.event("watchtower", "service_restarted",
                                data={"service": service, "took_seconds": took_seconds},
                                message=msg)

    async def kill_switch(self, engaged: bool, reason: str = ""):
        event_type = "kill_switch_engaged" if engaged else "kill_switch_disengaged"
        priority = "critical" if engaged else "normal"
        return await self.event("watchtower", event_type,
                                priority=priority,
                                data={"engaged": engaged, "reason": reason},
                                message=f"Kill switch {'ON' if engaged else 'OFF'}: {reason}")

    # --- Deployments ---

    async def deploy_staged(self, service: str, version: str = ""):
        return await self.event("watchtower", "deploy_staged",
                                data={"service": service, "version": version},
                                message=f"{service} v{version} staged for approval")

    async def deploy_complete(self, service: str, version: str = ""):
        return await self.event("watchtower", "deploy_complete",
                                data={"service": service, "version": version},
                                message=f"{service} v{version} deployed successfully")

    async def deploy_rolled_back(self, service: str, reason: str = ""):
        return await self.event("watchtower", "deploy_rolled_back",
                                priority="critical",
                                data={"service": service, "reason": reason},
                                message=f"{service} deploy rolled back: {reason}")

    # --- Proposals ---

    async def proposal_auto_approved(self, title: str, executor: str = ""):
        return await self.event("watchtower", "proposal_auto_approved",
                                data={"title": title, "executor": executor},
                                message=f"Auto-approved: {title} (via {executor})")

    async def proposal_dispatched(self, title: str, task_id: str = ""):
        return await self.event("orchestrator", "proposal_dispatched",
                                data={"title": title, "task_id": task_id},
                                message=f"Proposal dispatched: {title}")


# Singleton instance — import and use directly
roux = RouxClient()
