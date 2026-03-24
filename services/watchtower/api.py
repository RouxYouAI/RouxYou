"""
Watchtower — Cron Service & Proposal Bus
=========================================
Runs scheduled jobs, manages proposals, handles service restarts.

Port: CONFIG.PORT_WATCHTOWER_CRON

Built-in jobs:
  - health_check    : every 5 minutes
  - memory_decay    : 3 AM + 3 PM daily
  - task_proposer   : every 30 minutes
  - web_researcher  : 10 AM daily

Custom schedule sync:
  Add your own job functions below the "# ADD YOUR OWN JOBS HERE" comment,
  then register them in the cron_jobs list at the bottom of this section.

Endpoints:
  /health
  /watchtower/jobs
  /watchtower/history
  /watchtower/run/{job_name}
  /watchtower/toggle/{job_name}
  /restart/{service_name}
  /proposals + proposal sub-routes
  /auto-approve/config
  /kill-switch/engage + disengage
  /budget + budget sub-routes
  /research + /research/state
  /snapshot
  /queue_snapshot, /pending_results, /task_result  (Orchestrator durability)
"""

import os
import sys
import time
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Callable, Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel
import requests

_HERE = Path(__file__).parent
_PROJECT_ROOT = _HERE.parent.parent
for _p in [str(_PROJECT_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared.logger import get_logger
from shared.kill_switch import (is_engaged as _kill_switch_engaged,
                                engage as _engage_kill_switch,
                                disengage as _disengage_kill_switch,
                                get_status as _kill_switch_status)
from shared.execution_budget import (get_status as _budget_status,
                                     update_config as _budget_update,
                                     reset_counter as _budget_reset)
from shared.blackbox import log_event as _bb_log
from shared.roux_client import roux as _roux
from config import CONFIG

logger = get_logger("watchtower")

app = FastAPI(title="Watchtower", version="1.1.0")

PORT         = CONFIG.PORT_WATCHTOWER_CRON
GATEWAY_URL  = f"http://localhost:{CONFIG.PORT_GATEWAY}"
SERVICES     = {
    "gateway":      CONFIG.PORT_GATEWAY,
    "orchestrator": CONFIG.PORT_ORCHESTRATOR,
    "coder":        CONFIG.PORT_CODER,
    "worker":       CONFIG.PORT_WORKER,
    "memory":       CONFIG.PORT_MEMORY,
}

job_history: List[Dict] = []

# ==========================================
# Service restart commands — cross-platform
# ==========================================

def _launch_cmd(service: str, root: str) -> str:
    """Build a platform-appropriate service launch command."""
    scripts = {
        "gateway":      ("gateway/gateway.py",       None),
        "orchestrator": ("orchestrator/orchestrator.py", "orchestrator"),
        "coder":        ("coder/coder.py",            "coder"),
        "worker":       ("worker/worker.py",          "worker"),
        "memory":       ("memory/memory_agent.py",    "memory"),
    }
    entry, subdir = scripts.get(service, (None, None))
    if not entry:
        return None

    cwd = os.path.join(root, subdir) if subdir else root

    if os.name == "nt":
        venv_python = os.path.join(root, "venv", "Scripts", "python.exe")
        script = os.path.join(root, entry)
        return f'start "{service.title()}" cmd /k "cd /d {cwd} && {venv_python} {script}"'
    else:
        venv_python = os.path.join(root, "venv", "bin", "python")
        script = os.path.join(root, entry)
        return f'cd "{cwd}" && "{venv_python}" "{script}" &'


ROOT_DIR = str(_PROJECT_ROOT)


# ==========================================
# Built-in job functions
# ==========================================

def health_check_all():
    results = {}
    for name, port in SERVICES.items():
        try:
            r = requests.get(f"http://localhost:{port}/health", timeout=5)
            results[name] = "healthy" if r.status_code == 200 else f"unhealthy ({r.status_code})"
        except Exception:
            results[name] = "offline"

    online = sum(1 for v in results.values() if v == "healthy")
    logger.info(f"Health: {online}/{len(SERVICES)} online")

    crashed = [n for n, s in results.items() if s == "offline"]
    if crashed:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            for svc in crashed:
                if loop.is_running():
                    asyncio.create_task(_roux.service_crash(svc, restarting=False))
                else:
                    loop.run_until_complete(_roux.service_crash(svc, restarting=False))
        except Exception:
            pass

    return results


def run_memory_decay():
    logger.info("CRON: Running memory decay...")
    try:
        from shared.memory import memory
        stats = memory.run_decay()
        logger.info(f"  Decay: {stats['initial']} → {stats['remaining']} episodes")
        return {"success": True, **stats}
    except Exception as e:
        logger.error(f"  Decay failed: {e}")
        return {"success": False, "error": str(e)}


def run_researcher():
    logger.info("CRON: Running web researcher...")
    try:
        from shared.researcher import run_research
        stats = run_research()
        logger.info(f"  Research: {stats.get('focus')} — {stats.get('findings', 0)} finding(s)")
        return stats
    except Exception as e:
        logger.error(f"  Researcher failed: {e}")
        return {"success": False, "error": str(e)}


def _dispatch_to_orchestrator(proposal: Dict) -> Dict:
    payload = {
        "proposal_id": proposal["id"],
        "title":       proposal.get("title", ""),
        "description": proposal.get("description", ""),
        "category":    proposal.get("category", ""),
        "priority":    proposal.get("priority", 5),
        "proposed_action": proposal.get("proposed_action", ""),
        "executor":    proposal.get("executor", "manual"),
        "executor_meta": proposal.get("executor_meta", {}),
    }
    try:
        r = requests.post(f"{GATEWAY_URL}/orch/queue/proposal", json=payload, timeout=60)
        result = r.json()
        if result.get("success"):
            _bb_log("proposal_dispatched", {"id": proposal["id"], "title": proposal.get("title", ""),
                     "approved_by": proposal.get("approved_by", "?"),
                     "task_id": result.get("task_id")}, source="watchtower_cron")
        return result
    except requests.exceptions.ConnectionError:
        logger.error(f"DISPATCH: Orchestrator offline — cannot execute {proposal['id']}")
        return {"success": False, "error": "Orchestrator offline"}
    except Exception as e:
        return {"success": False, "error": str(e)}


_last_proposals_result: Dict = {"proposals": [], "observer_stats": {}, "timestamp": 0}


def run_proposer():
    global _last_proposals_result
    logger.info("CRON: Running task proposer...")
    try:
        from shared.proposer import run_proposer_full as _run_full
        from shared.proposal_bus import sync_from_proposer, auto_approve_eligible_batch

        stats = _run_full()

        raw_proposals = stats.get("all_active", [])
        if raw_proposals:
            try:
                from shared.coach import enrich_proposals
                enriched = enrich_proposals(raw_proposals)
                stats["all_active"] = enriched
            except Exception as e:
                logger.warning(f"  Coach enrichment failed: {e}")

        bus_result = sync_from_proposer(
            all_active_proposals=stats.get("all_active", []),
            observer_stats=stats.get("observer_stats", {}),
        )

        new_count = bus_result.get("published", 0)
        total     = bus_result.get("total_active", 0)
        if new_count: logger.info(f"  {new_count} new proposal(s)")
        if total:     logger.info(f"  {total} active proposal(s)")
        if not new_count and not total: logger.info("  No proposals. System healthy.")

        if _kill_switch_engaged():
            logger.warning("  Kill switch engaged — auto-approve blocked")
            auto_approved = []
            auto_dispatched = 0
        else:
            auto_approved = auto_approve_eligible_batch()
            auto_dispatched = 0
            if auto_approved:
                for proposal in auto_approved:
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.create_task(_roux.proposal_auto_approved(
                                title=proposal.get("title", ""),
                                executor=proposal.get("executor", "")))
                    except Exception:
                        pass
                    if _dispatch_to_orchestrator(proposal).get("success"):
                        auto_dispatched += 1
                if auto_dispatched:
                    logger.info(f"  Auto-dispatched: {auto_dispatched} proposal(s)")

        bus_result["auto_approved"]   = len(auto_approved)
        bus_result["auto_dispatched"] = auto_dispatched
        _last_proposals_result = bus_result
        return {"success": True, **bus_result}

    except Exception as e:
        logger.error(f"  Proposer failed: {e}")
        return {"success": False, "error": str(e)}


# ==========================================
# ADD YOUR OWN SCHEDULE SYNC JOBS HERE
# ==========================================
# Example pattern:
#
# def sync_my_calendar():
#     """Sync external calendar to RouxYou state."""
#     logger.info("CRON: Syncing my calendar...")
#     # ... your sync logic ...
#     return {"success": True}
#
# Then add to cron_jobs below:
#   CronJob(name="my_calendar_sync", func=sync_my_calendar, hours=[7, 19])
# ==========================================


# ==========================================
# Cron engine
# ==========================================

class CronJob:
    def __init__(self, name: str, func: Callable,
                 hours: list = None, interval_minutes: int = None, enabled: bool = True):
        self.name = name
        self.func = func
        self.hours = hours or []
        self.interval_minutes = interval_minutes
        self.enabled = enabled
        self.last_run = None
        self.last_result = None
        self.run_count = 0

    def should_run(self, now: datetime) -> bool:
        if not self.enabled:
            return False
        if self.interval_minutes:
            if not self.last_run:
                return True
            return (now - self.last_run).total_seconds() / 60 >= self.interval_minutes
        if self.hours:
            if not self.last_run or self.last_run.date() != now.date():
                return now.hour in self.hours
            return self.last_run.hour != now.hour and now.hour in self.hours
        return False

    def run(self):
        now = datetime.now()
        logger.info(f"Running job: {self.name}")
        try:
            result = self.func()
            self.last_run = now
            self.last_result = {"success": True, "data": result, "time": now.isoformat()}
            self.run_count += 1
            job_history.append({"job": self.name, "time": now.isoformat(),
                                 "success": True, "result": str(result)[:200]})
            if len(job_history) > 100:
                job_history.pop(0)
            return result
        except Exception as e:
            self.last_result = {"success": False, "error": str(e), "time": now.isoformat()}
            job_history.append({"job": self.name, "time": now.isoformat(),
                                 "success": False, "error": str(e)})
            logger.error(f"Job {self.name} failed: {e}")
            return None


cron_jobs = [
    CronJob(name="health_check",    func=health_check_all,  interval_minutes=5),
    CronJob(name="memory_decay",    func=run_memory_decay,  hours=[3, 15]),
    CronJob(name="task_proposer",   func=run_proposer,      interval_minutes=30),
    CronJob(name="web_researcher",  func=run_researcher,    hours=[10]),
    # Add your custom schedule sync jobs here (see comment block above)
]


def cron_loop():
    logger.info("Watchtower cron loop started")
    while True:
        now = datetime.now()
        for job in cron_jobs:
            if job.should_run(now):
                job.run()
        time.sleep(30)


# ==========================================
# Queue snapshot / Orchestrator durability
# ==========================================

_queue_snapshot: Optional[Dict] = None
_pending_results: List[Dict] = []


@app.post("/queue_snapshot")
async def save_queue_snapshot(data: Dict):
    global _queue_snapshot
    _queue_snapshot = data.get("snapshot")
    return {"saved": True}


@app.get("/queue_snapshot")
async def get_queue_snapshot():
    return {"snapshot": _queue_snapshot}


@app.post("/task_result")
async def escrow_result(result: Dict):
    _pending_results.append({"result": result, "received_at": time.time()})
    return {"escrowed": True}


@app.get("/pending_results")
async def get_pending_results():
    results = list(_pending_results)
    _pending_results.clear()
    return {"results": results}


# ==========================================
# Core endpoints
# ==========================================

@app.on_event("startup")
async def startup():
    thread = threading.Thread(target=cron_loop, daemon=True)
    thread.start()
    logger.info(f"Watchtower started on port {PORT}")


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "watchtower",
            "port": PORT, "jobs": len(cron_jobs),
            "active_jobs": sum(1 for j in cron_jobs if j.enabled)}


@app.get("/watchtower/jobs")
async def list_jobs():
    return {"jobs": [{"name": j.name, "enabled": j.enabled, "hours": j.hours,
                       "interval_minutes": j.interval_minutes,
                       "last_run": j.last_run.isoformat() if j.last_run else None,
                       "last_result": j.last_result, "run_count": j.run_count}
                      for j in cron_jobs]}


@app.get("/watchtower/history")
async def get_history():
    return {"history": list(reversed(job_history[-20:]))}


@app.post("/watchtower/run/{job_name}")
async def run_job(job_name: str):
    for job in cron_jobs:
        if job.name == job_name:
            result = job.run()
            return {"success": True, "job": job_name, "result": result}
    return {"success": False, "error": f"Job '{job_name}' not found"}


@app.post("/watchtower/toggle/{job_name}")
async def toggle_job(job_name: str):
    for job in cron_jobs:
        if job.name == job_name:
            job.enabled = not job.enabled
            return {"job": job_name, "enabled": job.enabled}
    return {"success": False, "error": f"Job '{job_name}' not found"}


@app.post("/decay")
async def trigger_decay():
    return run_memory_decay()


# ==========================================
# Service restart
# ==========================================

@app.post("/restart/{service_name}")
async def restart_service(service_name: str):
    if service_name not in SERVICES:
        return {"success": False, "error": f"Unknown service: {service_name}. Known: {list(SERVICES.keys())}"}

    port = SERVICES[service_name]
    try:
        r = requests.get(f"http://localhost:{port}/health", timeout=2)
        if r.status_code == 200:
            return {"success": False, "error": f"{service_name} is already running on port {port}"}
    except Exception:
        pass  # Good — it's down

    cmd = _launch_cmd(service_name, ROOT_DIR)
    if not cmd:
        return {"success": False, "error": f"No launch command for {service_name}"}

    logger.info(f"RESTART: Launching {service_name}")
    try:
        if os.name == "nt":
            subprocess.Popen(cmd, shell=True, cwd=ROOT_DIR)
        else:
            subprocess.Popen(cmd, shell=True, cwd=ROOT_DIR,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        for i in range(15):
            time.sleep(1)
            try:
                r = requests.get(f"http://localhost:{port}/health", timeout=2)
                if r.status_code == 200:
                    logger.info(f"RESTART: {service_name} online in {i+1}s")
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.create_task(_roux.service_restarted(service_name, took_seconds=i+1))
                    except Exception:
                        pass
                    return {"success": True, "message": f"{service_name} restarted",
                            "port": port, "startup_time": i+1}
            except Exception:
                continue

        return {"success": False, "error": f"{service_name} launched but not responding after 15s"}

    except Exception as e:
        return {"success": False, "error": str(e)}


# ==========================================
# Proposals
# ==========================================

@app.post("/proposals")
async def trigger_proposer():
    return run_proposer()


@app.get("/proposals")
async def get_proposals():
    from shared.proposal_bus import get_active
    return {"success": True, "proposals": get_active(),
            "observer_stats": _last_proposals_result.get("observer_stats", {}),
            "timestamp": _last_proposals_result.get("timestamp", 0)}


@app.get("/proposals/history")
async def get_proposal_history(limit: int = 50):
    from shared.proposal_bus import get_history
    return {"success": True, "history": get_history(limit)}


@app.post("/proposals/{proposal_id}/approve")
async def approve_proposal_endpoint(proposal_id: str):
    from shared.proposal_bus import approve_proposal
    result = approve_proposal(proposal_id)
    if result:
        return {"success": True, "proposal": result}
    return {"success": False, "error": f"{proposal_id} not found or not pending"}


@app.post("/proposals/{proposal_id}/dismiss")
async def dismiss_proposal_endpoint(proposal_id: str):
    from shared.proposal_bus import dismiss_proposal
    result = dismiss_proposal(proposal_id)
    if result:
        return {"success": True, "proposal": result}
    return {"success": False, "error": f"{proposal_id} not found or not pending"}


@app.post("/proposals/{proposal_id}/state")
async def update_proposal_state(proposal_id: str, new_state: str, result: Any = None):
    from shared.proposal_bus import update_state
    updated = update_state(proposal_id, new_state, result)
    if updated:
        return {"success": True, "proposal": updated}
    return {"success": False, "error": f"Failed to update {proposal_id}"}


@app.get("/proposals/stats")
async def proposal_stats_endpoint():
    from shared.proposal_bus import get_proposal_stats
    return get_proposal_stats()


# ==========================================
# Auto-approve config
# ==========================================

class AutoApproveConfigUpdate(BaseModel):
    enabled: bool = None
    allowed_categories: list = None
    max_priority: int = None
    min_confidence: float = None
    require_reversible: bool = None
    blocked_executors: list = None
    daily_limit: int = None


@app.get("/auto-approve/config")
async def get_auto_approve_config():
    from shared.proposal_bus import load_auto_approve_config
    return {"success": True, "config": load_auto_approve_config()}


@app.post("/auto-approve/config")
async def update_auto_approve_config(update: AutoApproveConfigUpdate):
    from shared.proposal_bus import load_auto_approve_config, save_auto_approve_config
    config = load_auto_approve_config()
    for k, v in update.dict(exclude_none=True).items():
        config[k] = v
    save_auto_approve_config(config)
    return {"success": True, "config": config}


@app.post("/auto-approve/reset-counter")
async def reset_auto_approve_counter():
    from shared.proposal_bus import load_auto_approve_config, save_auto_approve_config
    config = load_auto_approve_config()
    config["today_count"] = 0
    save_auto_approve_config(config)
    return {"success": True, "config": config}


# ==========================================
# Research
# ==========================================

@app.post("/research")
async def trigger_research(topic: str = None):
    from shared.researcher import run_research, RESEARCH_TOPICS
    result = run_research(topic_override=topic)
    result["available_topics"] = [t["focus"] for t in RESEARCH_TOPICS]
    return result


@app.get("/research/state")
async def research_state():
    from shared.researcher import _load_state, RESEARCH_TOPICS
    state = _load_state()
    next_idx = state.get("topic_index", 0) % len(RESEARCH_TOPICS)
    state["next_topic"] = RESEARCH_TOPICS[next_idx]["focus"]
    state["available_topics"] = [t["focus"] for t in RESEARCH_TOPICS]
    return state


@app.get("/coach/status")
async def coach_status():
    try:
        r = requests.get(f"{CONFIG.OLLAMA_HOST}/api/tags", timeout=3)
        if r.status_code == 200:
            models = [m["name"] for m in r.json().get("models", [])]
            return {"status": "online", "models": models}
    except Exception:
        pass
    return {"status": "offline", "models": []}


# ==========================================
# Kill switch
# ==========================================

class KillSwitchRequest(BaseModel):
    reason: str = "Manual kill switch"
    engaged_by: str = "human"


@app.get("/kill-switch")
async def get_kill_switch():
    return {"success": True, **_kill_switch_status()}


@app.post("/kill-switch/engage")
async def engage_kill_switch(req: KillSwitchRequest = None):
    reason     = req.reason if req else "Manual kill switch"
    engaged_by = req.engaged_by if req else "human"
    state = _engage_kill_switch(reason=reason, engaged_by=engaged_by)
    await _roux.kill_switch(engaged=True, reason=reason)
    return {"success": True, **state}


@app.post("/kill-switch/disengage")
async def disengage_kill_switch():
    state = _disengage_kill_switch(disengaged_by="human")
    await _roux.kill_switch(engaged=False, reason="Disengaged by human")
    return {"success": True, **state}


# ==========================================
# Execution budget
# ==========================================

class BudgetConfigUpdate(BaseModel):
    max_per_hour: int = None
    enabled: bool = None
    auto_kill_switch: bool = None


@app.get("/budget")
async def get_budget():
    return {"success": True, **_budget_status()}


@app.post("/budget/config")
async def update_budget_config(update: BudgetConfigUpdate):
    result = _budget_update(**update.dict(exclude_none=True))
    return {"success": True, **result}


@app.post("/budget/reset")
async def reset_budget():
    return {"success": True, **_budget_reset()}


# ==========================================
# Git snapshot
# ==========================================

class SnapshotRequest(BaseModel):
    message: str = None


@app.post("/snapshot")
async def create_snapshot(req: SnapshotRequest = None):
    from shared.git_snapshot import manual_snapshot as _git_manual_snapshot
    result = _git_manual_snapshot(message=req.message if req else None)
    return {"success": result["success"], **result}


if __name__ == "__main__":
    import uvicorn
    logger.info(f"Watchtower Cron starting on port {PORT}...")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
