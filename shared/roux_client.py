"""
Roux Client — Shared helper for notifying Roux Voice Service
=============================================================
Import this in any service that needs to speak to Roux.
All calls are fire-and-forget (non-blocking, fail-silent).

Usage:
    from shared.roux_client import roux

    await roux.say("Three tasks done, all clean.")
    await roux.task_complete(agent="coder", summary="Dashboard fix applied")
    await roux.service_crash(service="memory", restarting=True)
    await roux.deploy_complete(service="worker", version="1.4")
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import CONFIG

from shared.redact import redact as _redact, redact_dict as _redact_dict

logger = logging.getLogger("roux_client")

ROUX_URL = f"http://localhost:{CONFIG.PORT_ROUX}"

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
    """Fire-and-forget POST to Roux. Never raises."""
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
                    return await resp.json() if resp.status < 400 else None
        else:
            import requests as _requests
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None, lambda: _requests.post(url, json=payload, timeout=timeout)
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.debug(f"Roux notification failed ({endpoint}): {e}")
        return None


class RouxClient:
    """Ergonomic wrapper around the Roux /event and /speak endpoints."""

    async def event(self, source: str, event_type: str,
                    priority: str = "normal", data: dict = None,
                    message: str = None):
        return await _post("/event", {
            "source": source,
            "event_type": event_type,
            "priority": priority,
            "data": _redact_dict(data) if data else {},
            "message": _redact(message) if message else None,
        })

    async def say(self, text: str, priority: str = "normal"):
        return await _post("/speak", {"text": _redact(text), "priority": priority})

    async def task_complete(self, agent: str = "system", summary: str = ""):
        return await self.event("orchestrator", "task_complete",
                                data={"agent": agent, "summary": summary},
                                message=f"{agent} completed: {summary}")

    async def task_failed(self, agent: str = "system", error: str = ""):
        return await self.event("orchestrator", "task_failed", priority="normal",
                                data={"agent": agent, "error": error},
                                message=f"{agent} failed: {error}")

    async def service_crash(self, service: str, restarting: bool = False):
        msg = f"{service} crashed" + (" — restarting now" if restarting else "")
        return await self.event("watchtower", "service_crash", priority="critical",
                                data={"service": service, "restarting": restarting},
                                message=msg)

    async def service_restarted(self, service: str, took_seconds: int = None):
        msg = f"{service} is back online" + (f" ({took_seconds}s)" if took_seconds else "")
        return await self.event("watchtower", "service_restarted",
                                data={"service": service, "took_seconds": took_seconds},
                                message=msg)

    async def kill_switch(self, engaged: bool, reason: str = ""):
        event_type = "kill_switch_engaged" if engaged else "kill_switch_disengaged"
        priority = "critical" if engaged else "normal"
        return await self.event("watchtower", event_type, priority=priority,
                                data={"engaged": engaged, "reason": reason},
                                message=f"Kill switch {'ON' if engaged else 'OFF'}: {reason}")

    async def deploy_staged(self, service: str, version: str = ""):
        return await self.event("watchtower", "deploy_staged",
                                data={"service": service, "version": version},
                                message=f"{service} v{version} staged for approval")

    async def deploy_complete(self, service: str, version: str = ""):
        return await self.event("watchtower", "deploy_complete",
                                data={"service": service, "version": version},
                                message=f"{service} v{version} deployed successfully")

    async def deploy_rolled_back(self, service: str, reason: str = ""):
        return await self.event("watchtower", "deploy_rolled_back", priority="critical",
                                data={"service": service, "reason": reason},
                                message=f"{service} deploy rolled back: {reason}")

    async def proposal_auto_approved(self, title: str, executor: str = ""):
        return await self.event("watchtower", "proposal_auto_approved",
                                data={"title": title, "executor": executor},
                                message=f"Auto-approved: {title} (via {executor})")

    async def proposal_dispatched(self, title: str, task_id: str = ""):
        return await self.event("orchestrator", "proposal_dispatched",
                                data={"title": title, "task_id": task_id},
                                message=f"Proposal dispatched: {title}")


roux = RouxClient()
