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

import json
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

# Auto-restart policy state (2026-05-21).
# Sliding 1-hour window per service. After AUTO_RESTART_MAX_PER_HOUR attempts
# the gate trips and we stop auto-restarting that service — escalation goes
# to Roux/DJ instead of crash-looping forever. Counters live in cron-watchtower
# process memory; restart of this process gives every service a fresh window
# (intentional — a watchtower bounce is a reasonable "try again" signal).
AUTO_RESTART_ENABLED = True
AUTO_RESTART_MAX_PER_HOUR = 3
AUTO_RESTART_WINDOW_SECONDS = 3600
SUPERVISOR_RESTART_URL = "http://localhost:8010/restart"
_restart_attempts: dict = {}        # service -> list of attempt timestamps
_escalation_notified: set = set()   # services we've already escalated for; cleared on success


def _can_auto_restart(service: str) -> tuple:
    """Return (allowed, attempts_in_window). Prunes old attempts in-place."""
    now = time.time()
    attempts = _restart_attempts.setdefault(service, [])
    attempts[:] = [t for t in attempts if now - t < AUTO_RESTART_WINDOW_SECONDS]
    return len(attempts) < AUTO_RESTART_MAX_PER_HOUR, len(attempts)


def _record_restart_attempt(service: str):
    _restart_attempts.setdefault(service, []).append(time.time())


def _trigger_supervisor_restart(service: str) -> dict:
    """POST to supervisor watchtower /restart/{service}.

    Returns dict with:
      dispatch_ok: True if we reached the supervisor and got SOME response
                   (whatever the supervisor said); False if we couldn't reach
                   the supervisor at all (ConnectionError). Used by the caller
                   to distinguish "supervisor said restart failed" (real
                   failure, count toward attempt budget) from "couldn't even
                   try" (don't count toward budget — we didn't actually
                   attempt a service-level restart). Fix from 2026-05-24
                   after spurious-at-startup AUTO-RESTART attempts were
                   counted against the budget.
      success:     True only if the supervisor confirmed restart worked.
      error:       error string if any.
    """
    try:
        r = requests.post(f"{SUPERVISOR_RESTART_URL}/{service}", timeout=45)
        # Got a response — dispatch reached the supervisor regardless of outcome
        out = {"dispatch_ok": True}
        if r.headers.get("content-type", "").startswith("application/json"):
            try:
                out.update(r.json())
            except Exception as e:
                out["success"] = False
                out["error"] = f"JSON parse error: {e}"
        else:
            out["success"] = False
            out["error"] = f"HTTP {r.status_code}"
        return out
    except requests.ConnectionError as e:
        # Supervisor unreachable — we never actually attempted the restart
        return {"dispatch_ok": False, "success": False,
                "error": f"ConnectionError: {e}"}
    except Exception as e:
        # Timeout / other: ambiguous whether supervisor received it.
        # Conservative — count the attempt against the budget.
        return {"dispatch_ok": True, "success": False,
                "error": f"{type(e).__name__}: {e}"}


# Disk-space guard (2026-06-18): a full disk mid-write corrupts state (how memory.json
# truncated on 06-14; a 14:51–15:04 root-fill failed proposal-history writes). Root sits
# ~75% used (~48G free) so a model-load spike can tip it to 100%. Warn DJ
# BEFORE that. Once-per-breach + hysteresis so it never flaps/spams.
DISK_LOW_GB = 15.0       # root free-space floor → alert below this
DISK_RECOVER_GB = 25.0   # clear the alert once back above this
_disk_low_alerted = False


def _check_disk_space():
    global _disk_low_alerted
    try:
        import shutil
        free_gb = shutil.disk_usage("/").free / (1024 ** 3)
    except Exception as e:
        logger.warning(f"disk check failed: {e}")
        return
    if free_gb < DISK_LOW_GB and not _disk_low_alerted:
        logger.warning(f"DISK LOW: root {free_gb:.1f}G free (< {DISK_LOW_GB:.0f}G floor)")
        try:
            from shared.notify import notify_dj
            notify_dj(f"Disk low: root has {free_gb:.1f}G free (floor {DISK_LOW_GB:.0f}G). "
                      f"A full disk mid-write can corrupt state — free some space soon.", "critical")
        except Exception:
            pass
        _disk_low_alerted = True
    elif free_gb >= DISK_RECOVER_GB and _disk_low_alerted:
        logger.info(f"DISK recovered: root {free_gb:.1f}G free")
        _disk_low_alerted = False


def health_check_all():
    _check_disk_space()
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
    if not crashed:
        # Any previously-escalated service that's now healthy: clear its escalation
        # marker so a future crash gets a fresh notification.
        for svc in list(_escalation_notified):
            if results.get(svc) == "healthy":
                _escalation_notified.discard(svc)
        return results

    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except Exception:
        loop = None

    def _notify_roux(coro):
        # 2026-05-31 fix: the old code returned early when `loop is None` (the case in the
        # SYNC cron thread), which SILENTLY DROPPED the crash notification AND left the
        # coroutine un-awaited (RuntimeWarning). So Roux never learned about service crashes
        # — a real gap for her guardian role. Now: schedule on a running loop if one exists,
        # else fire-and-forget in a daemon thread (notification actually fires, coroutine is
        # awaited, and the single-threaded cron loop is NEVER blocked by it).
        if coro is None:
            return
        try:
            if loop is not None and loop.is_running():
                asyncio.create_task(coro)
                return
        except Exception:
            pass
        import threading
        def _run():
            try:
                asyncio.run(coro)
            except Exception:
                try:
                    coro.close()
                except Exception:
                    pass
        threading.Thread(target=_run, daemon=True, name="roux-notify").start()

    for svc in crashed:
        if not AUTO_RESTART_ENABLED:
            _notify_roux(_roux.service_crash(svc, restarting=False))
            continue

        allowed, attempts = _can_auto_restart(svc)
        if not allowed:
            # Gate tripped — escalate (once per breach until success)
            if svc not in _escalation_notified:
                logger.warning(
                    f"AUTO-RESTART suppressed for {svc}: {attempts} attempts in last hour "
                    f"(cap {AUTO_RESTART_MAX_PER_HOUR}). Crash-looping — needs human look."
                )
                _notify_roux(_roux.say(
                    f"{svc} keeps crashing — {attempts} restart attempts in the last hour. "
                    "Auto-restart paused, needs eyes on it.",
                    priority="critical",
                ))
                # notification too — voice puck isn't reliably housed yet (2026-06-18).
                try:
                    from shared.notify import notify_dj
                    notify_dj(f"{svc} is crash-looping ({attempts} restarts/hr) — auto-restart "
                              f"paused, needs your eyes.", "critical")
                except Exception:
                    pass
                _escalation_notified.add(svc)
            else:
                # Already escalated; silently log to avoid spamming on every tick
                logger.info(f"AUTO-RESTART still suppressed for {svc} (already escalated)")
            continue

        # Eligible — tell Roux we're restarting, then trigger.
        # NOTE: only RECORD the attempt if dispatch actually reached the
        # supervisor. Spurious failures (ConnectionError on a not-yet-bound
        # supervisor port at startup) burn budget for no work otherwise.
        _notify_roux(_roux.service_crash(svc, restarting=True))
        result = _trigger_supervisor_restart(svc)
        if not result.get("dispatch_ok", True):
            logger.warning(
                f"AUTO-RESTART: supervisor unreachable for {svc} dispatch "
                f"({result.get('error', '?')}). Attempt NOT counted against "
                f"hourly budget — retrying next cron tick."
            )
            continue

        _record_restart_attempt(svc)
        attempts_now = attempts + 1
        logger.info(
            f"AUTO-RESTART: dispatched restart for {svc} "
            f"(attempt {attempts_now}/{AUTO_RESTART_MAX_PER_HOUR} in window)"
        )
        if result.get("success"):
            logger.info(f"AUTO-RESTART: {svc} recovered (took {result.get('startup_time', '?')}s)")
            _escalation_notified.discard(svc)
        else:
            logger.warning(
                f"AUTO-RESTART: {svc} restart failed: {result.get('error', '?')}. "
                f"Will retry next cron tick if attempts remaining."
            )

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
    if os.environ.get("ROUX_WEB_RESEARCHER", "1").lower() in ("0", "false", "no"):
        return {"skipped": True, "reason": "ROUX_WEB_RESEARCHER env flag is 0"}
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
    if os.environ.get("ROUX_TASK_PROPOSER", "1").lower() in ("0", "false", "no"):
        return {"skipped": True, "reason": "ROUX_TASK_PROPOSER env flag is 0"}
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
                 hours: list = None, interval_minutes: int = None, enabled: bool = True,
                 autonomy: bool = False, gpu: bool = False):
        self.name = name
        self.func = func
        self.hours = hours or []
        self.interval_minutes = interval_minutes
        self.enabled = enabled
        # autonomy=True => this job is part of the self-improvement pipe (reflection,
        # proposer, researcher, drafter, executor, ...). The master autonomy switch
        # (shared/autonomy_switch.py) gates these as a group. autonomy=False => infra
        # (health_check, memory_decay) that must keep running even when paused.
        self.autonomy = autonomy
        # gpu=True => uses v5.3 / a GPU model. SKIPPED while the coder holds the 16GB card
        # (arbiter flag) — running during a coder /plan would fail/thrash. Resumes when released.
        self.gpu = gpu
        self.last_run = None
        self.last_result = None
        self.run_count = 0

    def should_run(self, now: datetime) -> bool:
        if not self.enabled:
            return False
        if self.gpu:
            try:
                from shared.gpu_arbiter import coder_holds_gpu
                if coder_holds_gpu():
                    return False  # coder owns the card; skip this tick, retry next
            except Exception:
                pass
        if self.interval_minutes:
            if not self.last_run:
                return True
            return (now - self.last_run).total_seconds() / 60 >= self.interval_minutes
        if self.hours:
            if not self.last_run or self.last_run.date() != now.date():
                return now.hour in self.hours
            return self.last_run.hour != now.hour and now.hour in self.hours
        return False

    def set_interval(self, minutes: int) -> dict:
        """Update polling interval. Clamped to [1, 60] minutes for safety.
        Returns dict with prior/new/clamped fields for audit."""
        clamped = max(1, min(60, int(minutes)))
        prior = self.interval_minutes
        self.interval_minutes = clamped
        # Switch from hours-based to interval-based scheduling if needed
        self.hours = []
        return {
            "job": self.name,
            "prior_interval_minutes": prior,
            "new_interval_minutes": clamped,
            "requested_minutes": int(minutes),
            "clamped": clamped != int(minutes),
        }

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


# Hard wall-clock cap for bigbrain-dependent cron jobs. The cron loop
# (cron_loop) runs jobs SYNCHRONOUSLY in ONE daemon thread — a job that blocks
# freezes EVERY other cron (health_check, auto_restart, the lot). The 2026-05-30
# freeze (~10 min) was traced 2026-05-30 to NOT be the bigbrain call (that's
# ~19s, healthy) but a DOWNSTREAM step: roux_reflection, on a PROPOSE decision,
# does a synchronous POST orchestrator/propose with a 600s timeout (the full
# draft pipeline), which blocked the cron thread. roux_self_propose has the same
# shape. The prior guard used `with ThreadPoolExecutor() as ex: ...result(
# timeout=600)` — but the `with` exit calls shutdown(wait=True), which RE-BLOCKS
# on the hung worker, so the loop never actually got released. This helper caps
# the WHOLE worker (bigbrain + any downstream /propose) and uses shutdown(
# wait=False), freeing the scheduler the instant we time out regardless of which
# sub-step blocks. The orphaned worker finishes or dies on its own; it can't hold
# the loop. (Separate, still-open: make /propose's own timeout sane / async so
# reflection can actually FILE proposals within the cap.) Tunable via env.
CRON_BIGBRAIN_TIMEOUT_S = int(os.getenv("ROUX_CRON_BIGBRAIN_TIMEOUT", "150"))


def _run_bounded(worker: Callable, timeout: float, job_name: str):
    """Run a blocking worker with a hard wall-clock cap that RELEASES the cron
    loop even if the worker hangs. Critically does NOT wait on the worker thread
    (no shutdown(wait=True)) — the scheduler must never block on a hung call."""
    import concurrent.futures
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(worker).result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        logger.error(
            f"{job_name}: bigbrain call exceeded {timeout}s wall-clock — "
            f"releasing cron loop (orphaned worker dies when HTTP retries exhaust)"
        )
        return {"skipped": True, "timed_out": True,
                "reason": f"bigbrain call exceeded {timeout}s; cron loop protected"}
    finally:
        # wait=False so we NEVER block the scheduler waiting on a hung worker.
        ex.shutdown(wait=False)


def run_roux_self_propose_sync():
    """Sync wrapper for the async roux_self_propose cycle. Works from both
    cron (no existing loop) and FastAPI's /watchtower/run/{job} endpoint (a
    loop IS running, so we offload to a fresh thread — see same pattern in
    run_shadow_sweeper_sync / run_post_exec_reflection_sync). Wall-clock capped
    so a degraded-API hang can't freeze the cron loop (see _run_bounded)."""
    from shared.roux_self_propose import run_self_propose_cycle, AUTONOMOUS_ENABLED
    if not AUTONOMOUS_ENABLED:
        return {"skipped": True, "reason": "ROUX_AUTONOMOUS_PROPOSE env flag is 0 (opt-in feature)"}
    def _worker():
        import asyncio
        return asyncio.run(run_self_propose_cycle(force=False))
    return _run_bounded(_worker, CRON_BIGBRAIN_TIMEOUT_S, "roux_self_propose")


def run_roux_reflection_sync():
    """Sync wrapper for the active reflection coach cycle. Uses bigbrain
    (Opus 4.8) to analyze Roux's behavior + draft meta-proposals. Works from
    both cron and FastAPI's HTTP trigger via worker-thread offload. Wall-clock
    capped so a degraded-API hang can't freeze the cron loop (see _run_bounded)."""
    from shared.reflection_coach import run_reflection_cycle, REFLECTION_ENABLED
    if not REFLECTION_ENABLED:
        return {"skipped": True, "reason": "ROUX_REFLECTION env flag is 0"}
    def _worker():
        import asyncio
        return asyncio.run(run_reflection_cycle(force=False))
    return _run_bounded(_worker, CRON_BIGBRAIN_TIMEOUT_S, "roux_reflection")


def run_proposal_executor_sync():
    """Sync wrapper for the proposal-executor dispatcher (shipped 2026-05-19).
    Drains approved proposals into typed handlers (meta/code/config/manual)."""
    from shared.proposal_executor import run_dispatch_cycle, EXECUTOR_ENABLED
    if not EXECUTOR_ENABLED:
        return {"skipped": True, "reason": "ROUX_EXECUTOR_ENABLED env flag is 0"}
    return run_dispatch_cycle(max_per_tick=1)


def run_authoring_corpus_extractor_sync():
    """Stage 1 of the self-train loop (2026-05-20 stub). Walks proposals_history
    + shadow_verifications.jsonl, extracts positive examples + FAIL-with-coaching
    pairs into /home/user/TheSeed/authoring_lora/corpus/authoring_pairs.jsonl.

    Default mode: positives-only (NO bigbrain gold gen). Set
    ROUX_AUTO_GOLD_GEN=1 to also call bigbrain on FAILs — gates by budget,
    not currently enabled to keep API spend predictable.

    Env: ROUX_AUTHORING_EXTRACTOR (default on).
    """
    if os.environ.get("ROUX_AUTHORING_EXTRACTOR", "1") == "0":
        return {"skipped": True, "reason": "ROUX_AUTHORING_EXTRACTOR env flag is 0"}
    cmd_args = ["/home/user/RouxYou/venv/bin/python",
                "/home/user/RouxYou/bin/build_authoring_corpus.py"]
    if os.environ.get("ROUX_AUTO_GOLD_GEN", "0") == "1":
        cmd_args.append("--with-gold")
        gold_limit = os.environ.get("ROUX_AUTO_GOLD_LIMIT", "10")
        cmd_args.extend(["--limit-gold", gold_limit])
    try:
        r = subprocess.run(cmd_args, capture_output=True, text=True, timeout=600)
        out_tail = (r.stdout or "")[-400:]
        return {"ok": r.returncode == 0, "returncode": r.returncode, "output_tail": out_tail}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "extractor timed out (>10min)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_corpus_watcher_sync():
    """Stage 2 of the self-train loop (2026-05-20 stub). Watches the authoring
    corpus + last-train timestamp. When Δpairs since last train >= threshold,
    emits a SELF-TRAIN proposal via /propose for DJ to approve.

    The proposal carries kind=code in the executor (since training is a code-shape
    action). When approved, executor surfaces it as queued_for_coder_pipeline;
    DJ then kicks training off explicitly. Roux does NOT auto-train without human
    approval — meta-policy lock spirit.

    Env: ROUX_CORPUS_WATCHER (default on).
    """
    if os.environ.get("ROUX_CORPUS_WATCHER", "1") == "0":
        return {"skipped": True, "reason": "ROUX_CORPUS_WATCHER env flag is 0"}

    corpus = Path("/home/user/TheSeed/authoring_lora/corpus/authoring_pairs.jsonl")
    state_file = Path("/home/user/TheSeed/authoring_lora/_watcher_state.json")
    threshold = int(os.environ.get("ROUX_CORPUS_WATCHER_THRESHOLD", "30"))

    if not corpus.exists():
        return {"ok": True, "reason": "corpus not yet built", "pair_count": 0}

    pair_count = 0
    with corpus.open() as f:
        for _ in f:
            pair_count += 1

    last_seen_count = 0
    if state_file.exists():
        try:
            s = json.loads(state_file.read_text())
            last_seen_count = int(s.get("last_seen_count", 0))
        except Exception:
            pass

    delta = pair_count - last_seen_count
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "last_seen_count": pair_count,
        "last_check_at": time.time(),
        "last_delta": delta,
    }, indent=2))

    if delta < threshold:
        return {"ok": True, "pair_count": pair_count, "delta": delta, "threshold": threshold,
                "action": "skip_under_threshold"}

    # Authorize a SELF-TRAIN proposal — sets up DJ to approve a training run
    try:
        from shared.proposal_bus import publish_proposal
        proposal = publish_proposal(
            title=f"Self-train: authoring LoRA — {pair_count} pairs available (+{delta} new)",
            description=(
                "Corpus watcher detected sufficient new pairs to warrant a retrain of the "
                "authoring LoRA. Proposes kicking off a training run via the existing "
                "train_authoring_ministral.py path. Requires DJ approval + explicit kickoff."
            ),
            category="codebase",
            priority=5,
            proposed_action=(
                "1. Stop v3 to free VRAM: kill the :9101 process\n"
                "2. Run: LD_LIBRARY_PATH=/home/user/merge-venv/lib/python3.13/site-packages/nvidia/cu13/lib "
                "/home/user/merge-venv/bin/python /home/user/TheSeed/authoring_lora/train_authoring_ministral.py\n"
                "3. Eval outputs land at /home/user/TheSeed/authoring_lora/eval/authoring_ministral_outputs.md\n"
                "4. If clean → stage adapter for serving + update propose endpoint to use 'authoring' alias\n"
                "5. Restart v3 on :9101 after training completes\n"
            ),
            evidence=(
                f"Corpus at /home/user/TheSeed/authoring_lora/corpus/authoring_pairs.jsonl has {pair_count} pairs "
                f"({delta} new since last training). Training script + schema already in place. "
                f"Path mirrors router_lora's 96.6% eval ship."
            ),
            reversible=True,
            source="authored_by_roux_self_train_watcher",
            confidence=0.9,
            executor="manual",  # DJ must explicitly approve + kick off
            disclosure={
                "source_url": "/home/user/TheSeed/authoring_lora/SCHEMA.md",
                "authored_at": time.time(),
                "reasoning": (
                    f"Self-train loop stage 2 detected {delta} new training pairs accumulating in the "
                    f"authoring_lora corpus since last training run. Total corpus: {pair_count}. "
                    f"Threshold for proposal: {threshold}. The four-stage Roux-self-trains loop (extractor → "
                    f"watcher → trainer → swap) wants this to surface for DJ review. Training itself is "
                    f"NOT auto-fired — explicit human approval + kickoff required per meta-policy spirit."
                ),
                "observer_verification": {"status": "pending", "comment": "", "verified_at": None, "verified_by": None},
            },
        )
        return {
            "ok": True, "pair_count": pair_count, "delta": delta, "threshold": threshold,
            "action": "proposed_self_train", "proposal_id": (proposal or {}).get("id"),
        }
    except Exception as e:
        return {"ok": False, "error": f"self-train proposal publish failed: {e}", "pair_count": pair_count}


def run_shadow_sweeper_sync():
    """Sync wrapper for the shadow-sweeper (2026-05-20). Re-fires shadow
    verification on authored proposals whose shadow task was lost (orchestrator
    restart, 529 outage, etc.). Lives in watchtower because it talks to the
    orchestrator's shadow_verifier via direct import — must be in-process to
    not need an HTTP layer. Env-gated via ROUX_SHADOW_SWEEPER."""
    import concurrent.futures
    from shared.shadow_sweeper import sweep_pending_shadows, SWEEPER_ENABLED
    if not SWEEPER_ENABLED:
        return {"skipped": True, "reason": "ROUX_SHADOW_SWEEPER env flag is 0"}
    def _worker():
        import asyncio
        return asyncio.run(sweep_pending_shadows())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(_worker).result(timeout=600)


def run_post_exec_reflection_sync():
    """Sync wrapper for post-execution per-proposal reflection (D3 stub 2026-05-20).
    Quality-judges recently completed proposals using v3 (offline-first).
    Writes to state/reflection_log.jsonl.

    Uses a worker thread + asyncio.run so it works both from cron (daemon thread,
    no existing loop) and from FastAPI's async endpoint (a loop is running, so
    asyncio.run can't fire inline — we offload to a clean thread)."""
    import concurrent.futures
    from shared.post_exec_reflection import reflect_on_completed, ENABLED
    if not ENABLED:
        return {"skipped": True, "reason": "ROUX_POST_EXEC_REFLECTION env flag is 0"}
    def _worker():
        import asyncio
        return asyncio.run(reflect_on_completed(max_per_run=5))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(_worker).result(timeout=600)


def run_auto_dismiss_stale_failed_sync():
    """Sweep FAIL-attested proposals older than ROUX_AUTO_DISMISS_HOURS (default 24h)
    into history. Env-gated by ROUX_AUTO_DISMISS_FAILED (default on)."""
    if os.environ.get("ROUX_AUTO_DISMISS_FAILED", "1") == "0":
        return {"skipped": True, "reason": "ROUX_AUTO_DISMISS_FAILED env flag is 0"}
    try:
        hours = float(os.environ.get("ROUX_AUTO_DISMISS_HOURS", "24"))
    except (TypeError, ValueError):
        hours = 24.0
    from shared.proposal_bus import auto_dismiss_stale_failed
    dismissed = auto_dismiss_stale_failed(stale_hours=hours)
    return {
        "dismissed_count": len(dismissed),
        "stale_hours_threshold": hours,
        "dismissed_ids": [p["id"] for p in dismissed],
    }


# Proposal drafter (2026-05-30 decouple). Drains the pending-draft queue that
# roux_reflection (and later roux_self_propose) write to, and runs the SLOW draft
# (POST /propose → authoring-cpu, ~7 min on CPU) in a BACKGROUND daemon thread so it
# never blocks the synchronous cron loop. Single-flight is enforced by the queue
# (shared/draft_queue.py::claim_next). See that module for the full rationale.
_ORCHESTRATOR_URL = f"http://localhost:{getattr(CONFIG, 'PORT_ORCHESTRATOR', 8001)}"


def _draft_worker(item: dict):
    """Background daemon thread: does the slow POST /propose OFF the cron loop, then
    records the outcome on the queue item. Never raises into the scheduler."""
    from shared.draft_queue import complete_draft, fail_draft
    item_id = item.get("id")
    topic = item.get("topic", "")
    try:
        resp = requests.post(
            f"{_ORCHESTRATOR_URL}/propose",
            json={"topic": topic, "author": item.get("author", "proposal_drafter"), "dry_run": False},
            timeout=900,  # authoring-cpu can take ~7 min — fine, this is OFF the cron loop
        )
        data = resp.json()
        if data.get("success"):
            complete_draft(item_id, data.get("proposal_id"))
            logger.info(f"proposal_drafter: published {data.get('proposal_id')} for draft {item_id}")
        else:
            fail_draft(item_id, str(data.get("error", "propose returned success=false")))
            logger.warning(f"proposal_drafter: /propose failed for {item_id}: {data.get('error')}")
    except Exception as e:
        fail_draft(item_id, str(e))
        logger.warning(f"proposal_drafter: draft {item_id} raised: {e}")


def run_proposal_drafter_sync():
    """Cron entry: claim ONE pending draft (single-flight) and run it in a background
    daemon thread. Returns IMMEDIATELY so the cron loop is never blocked by drafting."""
    if os.environ.get("ROUX_PROPOSAL_DRAFTER", "1").lower() in ("0", "false", "no"):
        return {"skipped": True, "reason": "ROUX_PROPOSAL_DRAFTER env flag is 0"}
    from shared.draft_queue import claim_next, queue_summary
    item = claim_next()
    if not item:
        return {"idle": True, "queue": queue_summary()}
    t = threading.Thread(target=_draft_worker, args=(item,), daemon=True,
                         name=f"draft-{item.get('id')}")
    t.start()
    return {"started": item.get("id"), "topic": (item.get("topic") or "")[:80],
            "queue": queue_summary()}


def run_knowledge_ingester_sync():
    """Cron entry: ingest any new files dropped in state/knowledge_inbox/ into the memory
    archive (era external_knowledge) so they become seed material for reflection. The
    repurposed-web_researcher path (2026-05-31). Env-gated ROUX_KNOWLEDGE_INGEST inside."""
    try:
        from shared.knowledge_ingest import run_knowledge_ingest_sync
        result = run_knowledge_ingest_sync()
        if result.get("ingested_count"):
            logger.info(f"knowledge_ingester: ingested {result['ingested_count']} file(s): {result.get('ingested')}")
        return result
    except Exception as e:
        logger.warning(f"knowledge_ingester failed: {e}")
        return {"error": str(e)}


def run_adjudication():
    """Infra cron (2026-06-18): promote APPROVE'd changes that SURVIVED the holding period
    (ADJUDICATION_DAYS, default 3) to confirmed_good — grows the trust-ledger clean-streak from
    changes that actually held. Promote-ONLY + tier-0-safe: never demotes; requires 3-day no-revert
    + (for .py) still-parses, else survival-only. We're automating the CHECK (was manual = the
    ledger bottleneck); the 3-day hold — the real confirmation bar — is UNCHANGED. Log-only
    (trust accrual is progress, not a 'needs your eyes' notification event)."""
    try:
        from shared.trust_ledger import adjudicate
        proms = adjudicate(apply=True)
    except Exception as e:
        logger.warning(f"ADJUDICATION error: {e}")
        return {"error": str(e)}
    if proms:
        by_tier = {}
        for p in proms:
            by_tier[p["tier"]] = by_tier.get(p["tier"], 0) + 1
        logger.info(f"ADJUDICATION: promoted {len(proms)} change(s) → confirmed_good {by_tier}")
    else:
        logger.info("ADJUDICATION: nothing eligible for promotion this run")
    return {"promotions": len(proms) if proms else 0}


def run_codebase_reindex():
    """Infra cron (2026-06-18): keep Roux's structural self-map (state/codebase_index.json) CURRENT.
    The index auto-discovers the tree now, but each process only rebuilds at startup — so when code
    changes (a proposal lands, we ship a fix), the persisted cache that proposer/authoring READ goes
    stale. This refreshes it on a schedule so she plans edits from an up-to-date map. READ-ONLY (AST
    parse + write a JSON map) = safe even at current trust; it's exactly the 'index your own codebase
    when idle' task DJ asked for. No-op when nothing changed."""
    try:
        from shared.codebase_index import codebase_index
        changed = codebase_index.refresh_if_stale()
        n = len(codebase_index.files)
        logger.info(f"CODEBASE REINDEX: {'rebuilt — ' if changed else 'up to date ('}{n} modules{'' if changed else ')'} in self-map")
        return {"rebuilt": bool(changed), "modules": n}
    except Exception as e:
        logger.warning(f"CODEBASE REINDEX error: {e}")
        return {"error": str(e)}


# Roux autonomous self-propose. Gated by ROUX_AUTONOMOUS_PROPOSE env flag inside the function.
# Cron registration is enabled here; the env check inside the wrapper is the real gate.
cron_jobs = [
    CronJob(name="health_check",       func=health_check_all,           interval_minutes=5),
    CronJob(name="memory_decay",       func=run_memory_decay,           hours=[3, 15]),
    CronJob(name="adjudication",       func=run_adjudication,           hours=[6, 18]),  # 2026-06-18 infra: promote-only ledger adjudication (twice daily); grows clean-streak safely
    CronJob(name="codebase_reindex",   func=run_codebase_reindex,       interval_minutes=30),  # 2026-06-18 infra: keep self-map current (read-only AST; no-op when unchanged)
    # 2026-05-17 watch mode: bumped task_proposer (which calls coach) and web_researcher
    # from their conservative defaults (30min / daily) to 5min — DJ wants live cadence on
    # Roux's full pipeline. Restore defaults when watching is done.
    # --- autonomy pipe (all autonomy=True) — gated as a group by the master autonomy
    #     switch (shared/autonomy_switch.py). Each still keeps its own per-job toggle +
    #     ROUX_* env flag; the master switch is the global big-red-button on top.
    CronJob(name="task_proposer",      func=run_proposer,               interval_minutes=5, autonomy=True, gpu=True),  # calls coach=v5.3
    CronJob(name="web_researcher",     func=run_researcher,             interval_minutes=5, autonomy=True, gpu=True),  # v5.3 digest
    # Knowledge ingester (2026-05-31, repurposed-web_researcher path): ingest external
    # knowledge dropped in state/knowledge_inbox/ → memory archive (era external_knowledge)
    # → feeds reflection's exogenous_seed. Env-gated ROUX_KNOWLEDGE_INGEST. autonomy pipe.
    CronJob(name="knowledge_ingester", func=run_knowledge_ingester_sync, interval_minutes=5, autonomy=True),
    CronJob(name="roux_self_propose",  func=run_roux_self_propose_sync, interval_minutes=5, autonomy=True, gpu=True),  # reasoning+companion=v5.3
    # Reflection coach: slower cadence, uses bigbrain/Sonnet for meta-analysis.
    # ROUX_REFLECTION env flag gates execution inside the sync wrapper.
    CronJob(name="roux_reflection",    func=run_roux_reflection_sync,   interval_minutes=30, autonomy=True, gpu=True),  # reasoning=v5.3 (+bigbrain)
    # Proposal drafter (2026-05-30 decouple): drains the pending-draft queue that
    # roux_reflection enqueues to, running the slow authoring-cpu draft in a
    # background thread so it never freezes the cron loop. 2-min cadence picks up
    # queued topics promptly; single-flight keeps it to one draft at a time.
    CronJob(name="proposal_drafter",   func=run_proposal_drafter_sync,  interval_minutes=2, autonomy=True, gpu=True),  # draft thread uses reasoning=v5.3
    # Proposal executor: drains approved proposals into typed handlers.
    # 1-min cadence — quick to react when DJ approves something on the dashboard.
    # ROUX_EXECUTOR_ENABLED env flag gates execution inside the sync wrapper.
    CronJob(name="proposal_executor",  func=run_proposal_executor_sync, interval_minutes=1, autonomy=True),
    # Post-execution reflection (D3 stub 2026-05-20). Hourly. Uses v3 (offline-first).
    # ROUX_POST_EXEC_REFLECTION env flag gates execution.
    CronJob(name="post_exec_reflection", func=run_post_exec_reflection_sync, interval_minutes=60, autonomy=True, gpu=True),  # reflection=v5.3
    # Shadow-sweeper (2026-05-20). 10-min cadence. Re-fires shadow verification
    # on authored proposals whose shadow task was lost (orchestrator restart,
    # Anthropic 529 outage). Backstop for the fire-and-forget /propose path.
    CronJob(name="shadow_sweeper", func=run_shadow_sweeper_sync, interval_minutes=10, autonomy=True),
    # Auto-dismiss stale FAIL-attested proposals (2026-05-20). 60-min cadence
    # is plenty since the threshold itself is hours, not minutes.
    # ROUX_AUTO_DISMISS_FAILED env flag gates execution inside the sync wrapper.
    CronJob(name="auto_dismiss_stale_failed", func=run_auto_dismiss_stale_failed_sync, interval_minutes=60, autonomy=True),
    # Self-train loop stage 1: extract training pairs from observed FAILs +
    # completed proposals (2026-05-20 stub). 6-hour cadence — pairs accumulate
    # slowly. Env-gated: ROUX_AUTHORING_EXTRACTOR.
    CronJob(name="authoring_corpus_extractor", func=run_authoring_corpus_extractor_sync, interval_minutes=360, autonomy=True),
    # Self-train loop stage 2: corpus watcher — when Δpairs >= threshold, file
    # a SELF-TRAIN proposal for DJ approval. 12-hour cadence. Env-gated:
    # ROUX_CORPUS_WATCHER. Threshold: ROUX_CORPUS_WATCHER_THRESHOLD (default 30).
    CronJob(name="corpus_watcher", func=run_corpus_watcher_sync, interval_minutes=720, autonomy=True),
    # Add your custom schedule sync jobs here (see comment block above)
]


_autonomy_pause_logged = False  # log the paused-skip transition once, not every tick


def _autonomy_paused() -> bool:
    """Read the master switch, never throwing — a broken switch must not be able to
    freeze the loop, so on any error we fail ON (not paused). Cheap (mtime-cached)."""
    try:
        from shared.autonomy_switch import is_paused
        return is_paused()
    except Exception:
        return False


def cron_loop():
    global _autonomy_pause_logged
    logger.info("Watchtower cron loop started")
    while True:
        now = datetime.now()
        for job in cron_jobs:
            # Re-read the master switch PER JOB, not once per outer tick. A single tick
            # runs all jobs synchronously and the slow autonomy jobs (task_proposer,
            # web_researcher, reflection) can stretch it to minutes — if we only checked
            # once at the top, a mid-tick pause wouldn't bite until the NEXT tick (a
            # multi-minute lag; caught live 2026-05-31). Per-job check makes the pause
            # take effect at the very next autonomy job. Infra jobs (autonomy=False)
            # always run, even while paused.
            if getattr(job, "autonomy", False):
                if _autonomy_paused():
                    if not _autonomy_pause_logged:
                        logger.info("AUTONOMY PAUSED (master switch) — skipping autonomy "
                                    "jobs; infra (health_check, memory_decay) still runs.")
                        _autonomy_pause_logged = True
                    continue
                elif _autonomy_pause_logged:
                    logger.info("AUTONOMY RESUMED (master switch) — autonomy jobs re-enabled.")
                    _autonomy_pause_logged = False
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


# --- Master autonomy switch (2026-05-31). One durable, graceful pause for the whole
#     autonomy pipe — the hook a dashboard flag POSTs to. Persisted in state/autonomy.json
#     (survives reboot); the cron loop reads it each tick. Infra jobs keep running.
@app.get("/watchtower/autonomy")
async def get_autonomy_state():
    from shared.autonomy_switch import get_state
    state = get_state()
    auto_jobs = [j.name for j in cron_jobs if getattr(j, "autonomy", False)]
    infra_jobs = [j.name for j in cron_jobs if not getattr(j, "autonomy", False)]
    return {**state, "autonomy_jobs": auto_jobs, "infra_jobs": infra_jobs}


class AutonomyBody(BaseModel):
    paused: bool
    by: str = "dashboard"
    reason: str = ""


@app.post("/watchtower/autonomy")
async def set_autonomy_state(body: AutonomyBody):
    from shared.autonomy_switch import set_paused
    state = set_paused(body.paused, by=body.by, reason=body.reason)
    logger.info(f"AUTONOMY {'PAUSED' if body.paused else 'RESUMED'} via API "
                f"(by={body.by!r}, reason={body.reason!r})")
    return {"success": True, **state}


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


@app.post("/proposals/auto-dismiss-stale")
async def auto_dismiss_stale_endpoint(stale_hours: Optional[float] = None):
    """Manual trigger for the stale-FAIL sweep. If stale_hours omitted, uses
    ROUX_AUTO_DISMISS_HOURS env var (default 24h). Useful for testing + manual
    cleanup between cron ticks."""
    from shared.proposal_bus import auto_dismiss_stale_failed
    if stale_hours is None:
        try:
            stale_hours = float(os.environ.get("ROUX_AUTO_DISMISS_HOURS", "24"))
        except (TypeError, ValueError):
            stale_hours = 24.0
    dismissed = auto_dismiss_stale_failed(stale_hours=stale_hours)
    return {
        "success": True,
        "dismissed_count": len(dismissed),
        "stale_hours_threshold": stale_hours,
        "dismissed_ids": [p["id"] for p in dismissed],
    }


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


@app.get("/ledger")
async def ledger_endpoint():
    """Trust-ledger graduation readiness per tier + autonomy status (dashboard ledger panel, 2026-06-20)."""
    from shared.trust_ledger import readiness_report, MIN_CLEAN_EVENTS, MAX_FALSE_BLOCK_RATE
    from shared.autonomy_switch import is_paused
    return {
        "tiers": readiness_report(),
        "paused": is_paused(),
        "min_clean": MIN_CLEAN_EVENTS,
        "max_false_block": MAX_FALSE_BLOCK_RATE,
    }


class ApproveBody(BaseModel):
    approved_by: str = "human"  # 'human' | 'claude' — governance always requires 'human'


@app.post("/proposals/{proposal_id}/approve")
async def approve_proposal_endpoint(proposal_id: str, body: ApproveBody = None):
    """Approve a pending proposal. Body is optional for backward compatibility
    (dashboard sends no body, defaults to 'human'). Claude can POST with
    body {approved_by: 'claude'} to approve non-governance authored proposals.
    Per 2026-05-17 amendment: governance category remains human-only."""
    from shared.proposal_bus import approve_proposal
    approved_by = body.approved_by if body else "human"
    result = approve_proposal(proposal_id, approved_by=approved_by)
    if result:
        return {"success": True, "proposal": result}
    return {
        "success": False,
        "error": f"{proposal_id} not found, not pending, blocked by disclosure rule, or governance proposal attempted approval by non-human",
    }


class DismissBody(BaseModel):
    reason: str = ""   # WHY it's dismissed — fed to roux_coaching so Roux learns (2026-06-18)
    by: str = "human"


@app.post("/proposals/{proposal_id}/dismiss")
async def dismiss_proposal_endpoint(proposal_id: str, body: DismissBody = None):
    from shared.proposal_bus import dismiss_proposal
    reason = body.reason if body else ""
    by = body.by if body else "human"
    result = dismiss_proposal(proposal_id, reason=reason, by=by)
    if not result:
        return {"success": False, "error": f"{proposal_id} not found or not pending"}
    coached = None
    if reason:
        # Teach Roux why — write the lesson to the roux_coaching archive she retrieves.
        try:
            from shared.shadow_verifier import coach_on_dismissal
            coached = await coach_on_dismissal(result, reason, by)
        except Exception as e:
            coached = {"written": False, "error": str(e)}
    return {"success": True, "proposal_id": proposal_id, "coached": coached}


@app.post("/proposals/{proposal_id}/state")
async def update_proposal_state(proposal_id: str, new_state: str, result: Any = None):
    from shared.proposal_bus import update_state
    updated = update_state(proposal_id, new_state, result)
    if updated:
        return {"success": True, "proposal": updated}
    return {"success": False, "error": f"Failed to update {proposal_id}"}


class ObserverVerifyBody(BaseModel):
    status: str  # "pass" | "fail" | "pending"
    comment: str = ""
    verified_by: str = "human"


class CronIntervalBody(BaseModel):
    minutes: int
    changed_by: str = "human"  # 'human' | 'claude' | 'roux'
    reasoning: str = ""


@app.post("/cron/{job_name}/interval")
async def set_cron_interval_endpoint(job_name: str, body: CronIntervalBody):
    """Update the polling interval of a registered cron job at runtime.
    Per 2026-05-17 amendment-shaped extension: Roux can call this to schedule
    her own subsystems. Clamped to [1, 60] minutes. Audit logged via blackbox.
    """
    if body.changed_by not in ("human", "claude", "roux"):
        return {"success": False, "error": f"invalid changed_by: {body.changed_by!r}"}

    target = next((j for j in cron_jobs if j.name == job_name), None)
    if not target:
        available = [j.name for j in cron_jobs]
        return {"success": False, "error": f"unknown job: {job_name!r}", "available": available}

    audit = target.set_interval(body.minutes)
    audit["changed_by"] = body.changed_by
    audit["reasoning"] = body.reasoning[:200]

    # Blackbox audit
    try:
        from shared.blackbox import log_event
        log_event("cron_interval_changed", audit, source="watchtower")
    except Exception:
        pass

    logger.info(
        f"CRON INTERVAL CHANGE: {job_name} {audit['prior_interval_minutes']}min "
        f"-> {audit['new_interval_minutes']}min (by {body.changed_by})"
    )
    return {"success": True, **audit}


@app.get("/cron/jobs")
async def list_cron_jobs():
    """Lightweight listing of cron jobs + their current intervals for Roux's gather_context."""
    return {
        "jobs": [
            {
                "name": j.name,
                "interval_minutes": j.interval_minutes,
                "hours": j.hours,
                "enabled": j.enabled,
                "last_run": j.last_run.isoformat() if j.last_run else None,
                "run_count": j.run_count,
            }
            for j in cron_jobs
        ]
    }


@app.get("/roux/audit")
async def roux_audit_endpoint(hours: int = 24):
    """Diagnostic audit of Roux's self-propose loop + adjacent state.
    Per approved prop_42afda55f25f — read-only report of queue health,
    observer flag persistence, recent failure patterns, Roux's own behavior.
    No state changes; safe to call frequently."""
    import json as _json
    import time as _time
    from pathlib import Path as _Path
    from datetime import datetime as _dt
    cutoff = _time.time() - hours * 3600

    audit = {
        "audit_at": _time.time(),
        "audit_at_str": _dt.now().isoformat(timespec="seconds"),
        "window_hours": hours,
        "queue_health": {},
        "observer_flag_persistence": {},
        "coder_failure_patterns": [],
        "roux_self_propose_behavior": {},
        "reflection_coach_behavior": {},
        "diagnostics": [],
    }

    # Queue health
    try:
        from shared.proposal_bus import get_active, get_history
        active = list(get_active())
        history = list(get_history(200))
        recent_hist = [p for p in history if p.get("created_at", 0) >= cutoff]
        audit["queue_health"] = {
            "active_count": len(active),
            "active_by_state": {},
            "active_by_source": {},
            "recent_history_count": len(recent_hist),
            "recent_completed": sum(1 for p in recent_hist if p.get("state") == "completed"),
            "recent_dismissed": sum(1 for p in recent_hist if p.get("state") == "dismissed"),
        }
        for p in active:
            s = p.get("state", "?")
            src = p.get("source", "?")
            audit["queue_health"]["active_by_state"][s] = audit["queue_health"]["active_by_state"].get(s, 0) + 1
            audit["queue_health"]["active_by_source"][src] = audit["queue_health"]["active_by_source"].get(src, 0) + 1
        # Persistent flags = active proposals that have been pending >12h
        persistent = []
        for p in active:
            created = p.get("created_at", 0)
            if created and (_time.time() - created) > 12 * 3600 and p.get("state") in ("pending", "executing"):
                persistent.append({
                    "id": p["id"],
                    "title": p.get("title", "")[:60],
                    "age_hours": round((_time.time() - created) / 3600, 1),
                    "state": p.get("state"),
                    "category": p.get("category"),
                })
        audit["observer_flag_persistence"]["persistent_proposals"] = persistent
        if persistent:
            audit["diagnostics"].append(f"⚠️ {len(persistent)} proposal(s) persisting >12h without resolution")
    except Exception as e:
        audit["queue_health_error"] = str(e)

    # Coder failure patterns from task_queue log
    try:
        tq_log = _Path("/home/user/RouxYou/logs/task_queue.log")
        if tq_log.exists():
            recent_lines = tq_log.read_text(errors="replace").splitlines()[-200:]
            patterns = {}
            for line in recent_lines:
                if "AUTO-RETRY" in line and "Coder could not generate plan" in line:
                    patterns["coder_plan_failures"] = patterns.get("coder_plan_failures", 0) + 1
                elif "AUTO-RETRY" in line:
                    patterns["other_auto_retry"] = patterns.get("other_auto_retry", 0) + 1
            audit["coder_failure_patterns"] = [{"pattern": k, "count": v} for k, v in patterns.items()]
            if patterns.get("coder_plan_failures", 0) > 5:
                audit["diagnostics"].append(f"⚠️ {patterns['coder_plan_failures']} 'Coder could not generate plan' failures in last 200 task-queue log lines")
    except Exception as e:
        audit["coder_failure_patterns_error"] = str(e)

    # Roux self-propose behavior
    try:
        from shared.roux_self_propose import _load_state as load_sp_state
        sp = load_sp_state()
        h = sp.get("history") or []
        recent_h = [c for c in h if c.get("run_at", 0) >= cutoff]
        skips = sum(1 for c in recent_h if c.get("decision") == "SKIP")
        proposes = sum(1 for c in recent_h if c.get("decision") == "PROPOSE")
        throttled = sum(1 for c in recent_h if c.get("decision") in ("THROTTLED", "SKIP_COOLDOWN"))
        interval_changes = sum(1 for c in recent_h if c.get("next_interval_seconds"))
        audit["roux_self_propose_behavior"] = {
            "config": sp.get("config", {}),
            "today_count": sp.get("today_count", 0),
            "history_total": len(h),
            "history_window": len(recent_h),
            "cycles_window": {
                "skip": skips,
                "propose": proposes,
                "throttled_or_cooldown": throttled,
                "interval_changes_requested": interval_changes,
            },
        }
        if skips > 20 and proposes == 0:
            audit["diagnostics"].append(f"⚠️ Self-propose: {skips} SKIPs and 0 PROPOSEs in window — possible local minimum")
    except Exception as e:
        audit["roux_self_propose_behavior_error"] = str(e)

    # Reflection coach behavior
    try:
        from shared.reflection_coach import _load_state as load_refl_state
        refl = load_refl_state()
        rh = refl.get("history") or []
        recent_rh = [c for c in rh if c.get("run_at", 0) >= cutoff]
        audit["reflection_coach_behavior"] = {
            "config": refl.get("config", {}),
            "today_count": refl.get("today_count", 0),
            "history_total": len(rh),
            "history_window": len(recent_rh),
            "decisions_window": {
                "propose_meta": sum(1 for c in recent_rh if c.get("decision") == "PROPOSE_META"),
                "skip": sum(1 for c in recent_rh if c.get("decision") == "SKIP"),
                "errors": sum(1 for c in recent_rh if c.get("decision") in ("ERROR", "PROPOSE_FAILED", "PROPOSE_REJECTED")),
            },
        }
    except Exception as e:
        audit["reflection_coach_behavior_error"] = str(e)

    if not audit["diagnostics"]:
        audit["diagnostics"].append("✅ no specific concerns surfaced in this window")

    return audit


@app.post("/roux/reflection")
async def roux_reflection_endpoint(force: bool = True):
    """Manual trigger for Roux's reflection coach cycle (bigbrain meta-analysis)."""
    from shared.reflection_coach import run_reflection_cycle
    result = await run_reflection_cycle(force=force)
    return {"success": True, "outcome": result}


@app.get("/roux/reflection/state")
async def roux_reflection_state():
    from shared.reflection_coach import _load_state, REFLECTION_ENABLED
    return {"reflection_enabled": REFLECTION_ENABLED, "state": _load_state()}


@app.post("/roux/self-propose")
async def roux_self_propose_endpoint(force: bool = True):
    """Manual trigger for Roux's autonomous self-propose cycle.
    Default force=True so testers can fire it on demand without waiting for cron cadence.
    Returns the full cycle outcome including decision, reasoning, and proposal_id if proposed."""
    from shared.roux_self_propose import run_self_propose_cycle
    result = await run_self_propose_cycle(force=force)
    return {"success": True, "outcome": result}


@app.get("/roux/self-propose/state")
async def roux_self_propose_state():
    """Inspect Roux's autonomous self-propose throttle state + history."""
    from shared.roux_self_propose import _load_state, AUTONOMOUS_ENABLED
    return {
        "autonomous_enabled": AUTONOMOUS_ENABLED,
        "state": _load_state(),
    }


@app.post("/roux/executor/dispatch")
async def proposal_executor_dispatch(max_per_tick: int = 1):
    """Manually run one proposal-executor dispatch cycle (shipped 2026-05-19).
    Returns summary of what was dispatched + per-proposal results."""
    from shared.proposal_executor import run_dispatch_cycle, EXECUTOR_ENABLED
    if not EXECUTOR_ENABLED:
        return {"success": False, "reason": "ROUX_EXECUTOR_ENABLED env flag is 0"}
    return {"success": True, "summary": run_dispatch_cycle(max_per_tick=max_per_tick)}


@app.get("/roux/executor/state")
async def proposal_executor_state():
    """Inspect proposal-executor state: kind distribution, dispatched counts,
    pending eligibility, and recent dispatch failures."""
    from collections import Counter
    from shared.proposal_executor import (
        _load_active, classify_kind, _is_eligible, EXECUTOR_ENABLED,
        _EXECUTOR_LOG,
    )
    rows = _load_active()
    by_kind: Counter = Counter()
    by_dispatch: Counter = Counter()
    eligible = 0
    failed: list = []
    for p in rows:
        kind, _ = classify_kind(p)
        by_kind[kind] += 1
        em = p.get("executor_meta") or {}
        ds = em.get("dispatch_state") or "unset"
        by_dispatch[ds] += 1
        if _is_eligible(p):
            eligible += 1
        if ds == "dispatch_failed":
            failed.append({
                "id": p.get("id"),
                "kind": kind,
                "attempts": em.get("dispatch_attempts"),
                "last_error": em.get("last_error"),
                "title": (p.get("title") or "")[:120],
            })
    recent_log: list = []
    if _EXECUTOR_LOG.exists():
        try:
            lines = _EXECUTOR_LOG.read_text().strip().splitlines()[-15:]
            for ln in lines:
                try:
                    recent_log.append(json.loads(ln))
                except Exception:
                    continue
        except Exception:
            pass
    return {
        "executor_enabled": EXECUTOR_ENABLED,
        "total_active": len(rows),
        "eligible_for_dispatch": eligible,
        "by_kind": dict(by_kind),
        "by_dispatch_state": dict(by_dispatch),
        "dispatch_failed": failed,
        "recent_log": recent_log,
    }


@app.post("/proposals/{proposal_id}/observer-verify")
async def observer_verify_endpoint(proposal_id: str, body: ObserverVerifyBody):
    from shared.proposal_bus import set_observer_verification
    result = set_observer_verification(
        proposal_id,
        status=body.status,
        comment=body.comment,
        verified_by=body.verified_by,
    )
    if result:
        # Mirror the verdict into the shadow log so Roux's shadow verdict
        # can be compared against the real (Claude or human) verdict
        try:
            from shared.shadow_verifier import log_claude_verdict, log_human_verdict
            if body.verified_by == "claude":
                log_claude_verdict(proposal_id, body.status, body.comment)
            elif body.verified_by == "human":
                log_human_verdict(proposal_id, body.status, body.comment)
        except Exception:
            pass
        return {"success": True, "proposal": result}
    return {"success": False, "error": f"{proposal_id} not found, not in active queue, or not an authored proposal"}


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
    from shared.researcher import run_research, _load_research_topics
    result = run_research(topic_override=topic)
    result["available_topics"] = [t["focus"] for t in _load_research_topics()]
    return result


@app.get("/research/state")
async def research_state():
    from shared.researcher import _load_state, _load_research_topics
    state = _load_state()
    topics = _load_research_topics()
    next_idx = state.get("topic_index", 0) % len(topics)
    state["next_topic"] = topics[next_idx]["focus"]
    state["available_topics"] = [t["focus"] for t in topics]
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
