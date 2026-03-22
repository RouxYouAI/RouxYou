"""
Git Auto-Snapshot — create a commit before any code modification.
Gives a rollback point for every self-modification. Local-only, never pushes.

Usage:
    from shared.git_snapshot import pre_deploy_snapshot, manual_snapshot
    result = pre_deploy_snapshot("worker", version=5)
"""

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict
import sys

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.logger import get_logger
from shared.blackbox import log_event as _bb_log

logger = get_logger("git_snapshot")


def _run_git(*args) -> tuple:
    """Run a git command in the project root. Returns (success, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return False, "", "git not found in PATH"
    except subprocess.TimeoutExpired:
        return False, "", "git command timed out"
    except Exception as e:
        return False, "", str(e)


def _is_repo() -> bool:
    ok, _, _ = _run_git("rev-parse", "--is-inside-work-tree")
    return ok


def _has_changes() -> bool:
    ok, stdout, _ = _run_git("status", "--porcelain")
    return ok and len(stdout) > 0


def pre_deploy_snapshot(service: str, version: int = 0, patches_count: int = 0) -> Dict:
    """Snapshot before a deploy begins."""
    if not _is_repo():
        return {"success": False, "commit": None, "message": "Not a git repository"}
    if not _has_changes():
        return {"success": True, "commit": None, "message": "Working tree clean"}

    ok, _, err = _run_git("add", "-A")
    if not ok:
        return {"success": False, "commit": None, "message": f"git add failed: {err}"}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    commit_msg = f"[auto-snapshot] pre-deploy: {service} v{version} ({patches_count} patches) at {now}"

    ok, stdout, err = _run_git("commit", "-m", commit_msg)
    if not ok:
        if "nothing to commit" in (err + stdout):
            return {"success": True, "commit": None, "message": "Nothing to commit"}
        return {"success": False, "commit": None, "message": f"git commit failed: {err}"}

    ok2, commit_hash, _ = _run_git("rev-parse", "--short", "HEAD")
    short_hash = commit_hash if ok2 else "unknown"

    logger.info(f"📸 SNAPSHOT: {short_hash} — {commit_msg}")
    _bb_log("git_snapshot", {"commit": short_hash, "service": service,
                              "version": version, "patches": patches_count}, source="git_snapshot")
    return {"success": True, "commit": short_hash, "message": commit_msg}


def manual_snapshot(message: str = None) -> Dict:
    """Create a manual git snapshot."""
    if not _is_repo():
        return {"success": False, "commit": None, "message": "Not a git repository"}
    if not _has_changes():
        return {"success": True, "commit": None, "message": "Working tree clean"}

    ok, _, err = _run_git("add", "-A")
    if not ok:
        return {"success": False, "commit": None, "message": f"git add failed: {err}"}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    commit_msg = message or f"[auto-snapshot] manual checkpoint at {now}"

    ok, stdout, err = _run_git("commit", "-m", commit_msg)
    if not ok:
        if "nothing to commit" in (err + stdout):
            return {"success": True, "commit": None, "message": "Nothing to commit"}
        return {"success": False, "commit": None, "message": f"git commit failed: {err}"}

    ok2, commit_hash, _ = _run_git("rev-parse", "--short", "HEAD")
    short_hash = commit_hash if ok2 else "unknown"

    logger.info(f"📸 MANUAL SNAPSHOT: {short_hash} — {commit_msg}")
    _bb_log("git_snapshot", {"commit": short_hash, "message": commit_msg, "manual": True}, source="git_snapshot")
    return {"success": True, "commit": short_hash, "message": commit_msg}
