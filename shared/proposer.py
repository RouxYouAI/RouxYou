"""
TASK PROPOSER — Agent-Initiated Tasks
=======================================
Pure Python heuristics that observe system state and propose tasks.
Every proposal lands as PENDING_APPROVAL — no auto-execution.

Observers:
  - health: service responsiveness
  - memory: episodic memory size, staleness, avg utility
  - codebase: parse errors, drift detection
  - tasks: failure patterns, stuck tasks
  - resources: disk, RAM pressure
  - skills: unused skills, low-success skills

Wired into: services/watchtower/api.py as a cron job.
"""

import json
import os
import time
import requests
from pathlib import Path
from typing import List, Dict, Any
from dataclasses import dataclass

import sys
_BASE = Path(__file__).parent.parent
sys.path.insert(0, str(_BASE))

from shared.logger import get_logger
from config import CONFIG

logger = get_logger("proposer")

BASE_DIR = Path(__file__).parent.parent
MEMORY_FILE = BASE_DIR / "memory.json"
SKILLS_FILE = BASE_DIR / "skills.json"
TASKS_FILE = BASE_DIR / "tasks.json"
STATE_DIR = BASE_DIR / "state"
CODEBASE_INDEX = STATE_DIR / "codebase_index.json"
QUEUE_HISTORY = STATE_DIR / "queue_history.json"
PROPOSALS_FILE = STATE_DIR / "proposals.json"

# Service ports from CONFIG
SERVICES = {
    "gateway":      CONFIG.PORT_GATEWAY,
    "orchestrator": CONFIG.PORT_ORCHESTRATOR,
    "coder":        CONFIG.PORT_CODER,
    "worker":       CONFIG.PORT_WORKER,
    "memory":       CONFIG.PORT_MEMORY,
}

# Thresholds
MEMORY_SIZE_WARN_KB = 500
MEMORY_LOW_AVG_UTILITY = 0.3
MEMORY_HIGH_COUNT = 50
DISK_FREE_WARN_GB = 5.0
RAM_AVAILABLE_WARN_GB = 2.0
TASK_FAILURE_STREAK = 3
HEALTH_FAIL_COUNT = 2


@dataclass
class Proposal:
    title: str
    description: str
    category: str
    priority: int
    proposed_action: str
    evidence: str
    reversible: bool = True

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "priority": self.priority,
            "proposed_action": self.proposed_action,
            "evidence": self.evidence,
            "reversible": self.reversible,
            "proposed_at": time.time(),
        }


class ProposalTracker:
    COOLDOWN_HOURS = 12

    def __init__(self):
        self.history: Dict[str, float] = {}
        self._load()

    def _load(self):
        if PROPOSALS_FILE.exists():
            try:
                with open(PROPOSALS_FILE, "r") as f:
                    self.history = json.load(f)
            except Exception:
                self.history = {}

    def _save(self):
        try:
            PROPOSALS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(PROPOSALS_FILE, "w") as f:
                json.dump(self.history, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save proposal history: {e}")

    def already_proposed(self, title: str) -> bool:
        last = self.history.get(title)
        if not last:
            return False
        return (time.time() - last) / 3600 < self.COOLDOWN_HOURS

    def mark_proposed(self, title: str):
        self.history[title] = time.time()
        cutoff = time.time() - (7 * 86400)
        self.history = {k: v for k, v in self.history.items() if v > cutoff}
        self._save()


def observe_health(tracker: ProposalTracker) -> List[Proposal]:
    proposals = []
    for name, port in SERVICES.items():
        try:
            r = requests.get(f"http://localhost:{port}/health", timeout=3)
            if r.status_code != 200:
                title = f"Service unhealthy: {name} (HTTP {r.status_code})"
                if not tracker.already_proposed(title):
                    proposals.append(Proposal(
                        title=title,
                        description=f"{name} on port {port} returned HTTP {r.status_code}.",
                        category="health", priority=8,
                        proposed_action=f"Restart {name} service via Watchtower",
                        evidence=f"GET /health → HTTP {r.status_code}",
                    ))
        except requests.ConnectionError:
            title = f"Service offline: {name} (port {port})"
            if not tracker.already_proposed(title):
                proposals.append(Proposal(
                    title=title,
                    description=f"{name} is not responding on port {port}.",
                    category="health", priority=9,
                    proposed_action=f"Restart {name} service via Watchtower",
                    evidence=f"GET /health → ConnectionError (port {port})",
                ))
        except requests.Timeout:
            title = f"Service slow: {name} (timeout on health check)"
            if not tracker.already_proposed(title):
                proposals.append(Proposal(
                    title=title,
                    description=f"{name} health check timed out (>3s).",
                    category="health", priority=6,
                    proposed_action=f"Investigate {name} — check logs, consider restart",
                    evidence=f"GET /health → Timeout after 3s",
                ))
        except Exception:
            pass
    return proposals


def observe_memory(tracker: ProposalTracker) -> List[Proposal]:
    proposals = []
    if not MEMORY_FILE.exists():
        return proposals
    try:
        size_kb = MEMORY_FILE.stat().st_size / 1024
        with open(MEMORY_FILE, "r") as f:
            episodes = json.load(f)
    except Exception:
        return proposals

    count = len(episodes)
    now = time.time()

    if size_kb > MEMORY_SIZE_WARN_KB:
        title = f"Episodic memory bloated ({size_kb:.0f}KB, {count} episodes)"
        if not tracker.already_proposed(title):
            proposals.append(Proposal(
                title=title,
                description=f"memory.json is {size_kb:.0f}KB with {count} episodes. "
                            "Large files slow retrieval and increase stale pattern risk.",
                category="memory", priority=5,
                proposed_action="Run memory decay with aggressive pruning",
                evidence=f"File size: {size_kb:.0f}KB, episode count: {count}",
            ))

    if count > MEMORY_HIGH_COUNT:
        title = f"Episode count high ({count} episodes)"
        if not tracker.already_proposed(title):
            proposals.append(Proposal(
                title=title,
                description=f"Episodic memory has {count} episodes. Consider running decay.",
                category="memory", priority=4,
                proposed_action="Run memory decay cycle",
                evidence=f"Episode count: {count} (threshold: {MEMORY_HIGH_COUNT})",
            ))

    if count > 5:
        utilities = [ep.get("utility", 0.5) for ep in episodes]
        avg_utility = sum(utilities) / len(utilities)
        if avg_utility < MEMORY_LOW_AVG_UTILITY:
            title = f"Low average memory utility ({avg_utility:.2f})"
            if not tracker.already_proposed(title):
                proposals.append(Proposal(
                    title=title,
                    description=f"Average episodic memory utility is {avg_utility:.2f}. "
                                "Many episodes may be low-quality or stale.",
                    category="memory", priority=5,
                    proposed_action="Run aggressive decay, review low-utility episodes",
                    evidence=f"Avg utility: {avg_utility:.2f} across {count} episodes",
                ))

    return proposals


def observe_codebase(tracker: ProposalTracker) -> List[Proposal]:
    proposals = []
    if not CODEBASE_INDEX.exists():
        return proposals
    try:
        with open(CODEBASE_INDEX, "r") as f:
            index = json.load(f)
    except Exception:
        return proposals

    modules = index.get("modules", {})
    error_modules = [(n, d["error"]) for n, d in modules.items() if d.get("error")]

    if error_modules:
        names = ", ".join(m[0] for m in error_modules)
        title = f"Parse errors in {len(error_modules)} module(s): {names}"
        if not tracker.already_proposed(title):
            proposals.append(Proposal(
                title=title,
                description=f"Modules with Python parse errors: "
                            + "; ".join(f"{m}: {e}" for m, e in error_modules),
                category="codebase", priority=7,
                proposed_action="Investigate and fix syntax errors",
                evidence=f"Parse errors: {error_modules}",
            ))

    return proposals


def observe_task_patterns(tracker: ProposalTracker) -> List[Proposal]:
    proposals = []
    if not QUEUE_HISTORY.exists():
        return proposals
    try:
        with open(QUEUE_HISTORY, "r") as f:
            data = json.load(f)
        tasks = data.get("tasks", [])
    except Exception:
        return proposals

    if not tasks:
        return proposals

    recent = [t for t in tasks if t.get("completed_at", 0) > time.time() - 86400]
    recent_failures = [t for t in recent if t.get("state") == "failed"]

    if len(recent_failures) >= TASK_FAILURE_STREAK:
        errors = [t.get("error", "")[:100] for t in recent_failures if t.get("error")]
        title = f"High failure rate: {len(recent_failures)} tasks failed in last 24h"
        if not tracker.already_proposed(title):
            proposals.append(Proposal(
                title=title,
                description=f"{len(recent_failures)} of {len(recent)} recent tasks failed. "
                            f"Errors: {'; '.join(errors[:3])}",
                category="tasks", priority=7,
                proposed_action="Analyze failure patterns, check Worker/Coder logs",
                evidence=f"Failed: {len(recent_failures)}/{len(recent)} in last 24h",
            ))

    if TASKS_FILE.exists():
        try:
            with open(TASKS_FILE, "r") as f:
                registry_tasks = json.load(f)
            stale_pending = [
                t for t in registry_tasks
                if t.get("status") == "pending"
                and (time.time() - t.get("created_at", time.time())) > 86400
            ]
            if stale_pending:
                all_names = ", ".join(t.get("title", "?")[:40] for t in stale_pending)
                title = f"Stale pending tasks ({len(stale_pending)} older than 24h)"
                if not tracker.already_proposed(title):
                    proposals.append(Proposal(
                        title=title,
                        description=f"{len(stale_pending)} tasks pending >24h: {all_names}.",
                        category="tasks", priority=4,
                        proposed_action="Human: review stale tasks in Task Queue tab",
                        evidence=f"Stale tasks: {all_names}",
                    ))
        except Exception:
            pass

    return proposals


def observe_resources(tracker: ProposalTracker) -> List[Proposal]:
    proposals = []
    try:
        import psutil
    except ImportError:
        return proposals

    try:
        # Cross-platform: use root on Linux/Mac, C: on Windows
        check_path = "C:\\" if os.name == "nt" else "/"
        disk = psutil.disk_usage(check_path)
        free_gb = disk.free / (1024 ** 3)
        if free_gb < DISK_FREE_WARN_GB:
            title = f"Low disk space ({free_gb:.1f}GB free)"
            if not tracker.already_proposed(title):
                proposals.append(Proposal(
                    title=title,
                    description=f"Only {free_gb:.1f}GB free. Logs, archives accumulate over time.",
                    category="resources", priority=6,
                    proposed_action="Clean up old logs, deploy archives, temp files",
                    evidence=f"Disk: {free_gb:.1f}GB free / {disk.total / (1024**3):.0f}GB total",
                    reversible=False,
                ))

        ram = psutil.virtual_memory()
        available_gb = ram.available / (1024 ** 3)
        if available_gb < RAM_AVAILABLE_WARN_GB:
            title = f"Low available RAM ({available_gb:.1f}GB)"
            if not tracker.already_proposed(title):
                proposals.append(Proposal(
                    title=title,
                    description=f"Only {available_gb:.1f}GB RAM available. LLM inference may slow.",
                    category="resources", priority=7,
                    proposed_action="Check for runaway processes, consider reducing model size",
                    evidence=f"RAM: {available_gb:.1f}GB available / {ram.total / (1024**3):.1f}GB total",
                ))
    except Exception as e:
        logger.warning(f"Resource observation failed: {e}")

    return proposals


def observe_skills(tracker: ProposalTracker) -> List[Proposal]:
    proposals = []
    if not SKILLS_FILE.exists():
        return proposals
    try:
        with open(SKILLS_FILE, "r") as f:
            skills = json.load(f)
    except Exception:
        return proposals

    unused = [s for s in skills if s.get("times_used", 0) == 0]
    if unused and len(unused) > len(skills) * 0.5:
        names = ", ".join(s.get("name", "?")[:30] for s in unused[:5])
        title = f"Many unused skills ({len(unused)}/{len(skills)})"
        if not tracker.already_proposed(title):
            proposals.append(Proposal(
                title=title,
                description=f"{len(unused)} of {len(skills)} skills never used: {names}.",
                category="skills", priority=2,
                proposed_action="Review skill extraction quality, prune irrelevant skills",
                evidence=f"Unused: {len(unused)}/{len(skills)} skills",
            ))

    bad_skills = [
        s for s in skills
        if s.get("times_used", 0) >= 3
        and s.get("times_succeeded", 0) / max(s.get("times_used", 1), 1) < 0.5
    ]
    if bad_skills:
        names = ", ".join(s.get("name", "?")[:30] for s in bad_skills)
        title = f"Low-success skills detected ({len(bad_skills)})"
        if not tracker.already_proposed(title):
            proposals.append(Proposal(
                title=title,
                description=f"Skills with <50% success rate: {names}.",
                category="skills", priority=5,
                proposed_action="Review and update or remove low-success skills",
                evidence=f"Bad skills: {names}",
            ))

    return proposals


ALL_OBSERVERS = [
    ("health", observe_health),
    ("memory", observe_memory),
    ("codebase", observe_codebase),
    ("tasks", observe_task_patterns),
    ("resources", observe_resources),
    ("skills", observe_skills),
]


def run_proposer() -> Dict[str, Any]:
    """Run all observers, return NEW proposals only (cooldown-filtered)."""
    tracker = ProposalTracker()
    all_proposals: List[Proposal] = []
    observer_stats = {}

    for name, observer_fn in ALL_OBSERVERS:
        try:
            proposals = observer_fn(tracker)
            observer_stats[name] = len(proposals)
            for p in proposals:
                if not tracker.already_proposed(p.title):
                    all_proposals.append(p)
                    tracker.mark_proposed(p.title)
        except Exception as e:
            logger.error(f"Observer '{name}' failed: {e}")
            observer_stats[name] = f"error: {e}"

    all_proposals.sort(key=lambda p: p.priority, reverse=True)

    stats = {
        "observers_run": len(ALL_OBSERVERS),
        "observer_stats": observer_stats,
        "proposals_generated": len(all_proposals),
        "proposals": [p.to_dict() for p in all_proposals],
        "timestamp": time.time(),
    }

    if all_proposals:
        logger.info(f"PROPOSER: Generated {len(all_proposals)} proposal(s)")
    else:
        logger.info("PROPOSER: No new proposals. System looks healthy.")

    return stats


def run_proposer_full() -> Dict[str, Any]:
    """Run all observers — returns both all active issues and new ones."""
    tracker = ProposalTracker()
    all_active: List[Proposal] = []
    new_proposals: List[Proposal] = []
    observer_stats = {}

    for name, observer_fn in ALL_OBSERVERS:
        try:
            dummy = ProposalTracker.__new__(ProposalTracker)
            dummy.history = {}
            dummy.already_proposed = lambda title: False

            proposals = observer_fn(dummy)
            observer_stats[name] = len(proposals)

            for p in proposals:
                all_active.append(p)
                if not tracker.already_proposed(p.title):
                    new_proposals.append(p)
                    tracker.mark_proposed(p.title)
        except Exception as e:
            logger.error(f"Observer '{name}' failed: {e}")
            observer_stats[name] = f"error: {e}"

    all_active.sort(key=lambda p: p.priority, reverse=True)
    new_proposals.sort(key=lambda p: p.priority, reverse=True)

    return {
        "observers_run": len(ALL_OBSERVERS),
        "observer_stats": observer_stats,
        "all_active": [p.to_dict() for p in all_active],
        "new_proposals": [p.to_dict() for p in new_proposals],
        "proposals_generated": len(new_proposals),
        "total_active": len(all_active),
        "timestamp": time.time(),
    }
