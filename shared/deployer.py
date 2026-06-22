"""
DEPLOYER — Blue-Green Deployment Pipeline
------------------------------------------
Handles the staging side of deployments: copy, patch, boot, health-check.

This module is intentionally SEPARATE from watchtower.py so the agent
system can improve its own deploy tooling. However, the Watchtower
enforces immutable HIL gates around the critical swap/kill steps.

Pipeline:
  1. STAGE  → Copy service dir to staging/<service>_v<N>/
  2. PATCH  → Apply code changes to the staging copy
  3. BOOT   → Start staging instance on temp port
  4. TEST   → Hit /health on staging instance, verify OK
  ─── HIL GATE (Watchtower) ───
  5. SWAP   → POST /gateway/swap (only Watchtower can do this)
  6. DRAIN  → Brief pause for in-flight requests
  7. KILL   → Shut down old instance (only Watchtower can do this)
  8. CLEAN  → Archive old dir, promote staging
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional, List
from dataclasses import dataclass, field, asdict
from enum import Enum

import aiohttp

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.logger import get_logger
from shared.blackbox import log_event as _bb_log
from config import CONFIG

logger = get_logger("deployer")

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
STAGING_DIR = PROJECT_ROOT / "staging"
ARCHIVE_DIR = PROJECT_ROOT / "archive"

# Cross-platform venv python path
if os.name == "nt":
    VENV_PYTHON = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"
else:
    VENV_PYTHON = PROJECT_ROOT / "venv" / "bin" / "python"

# Preferred staging ports from CONFIG. Used as the first try when staging;
# if occupied (e.g. mid post-deploy-watchdog window on a previous deploy
# before port-normalization completes), `_find_open_staging_port` falls
# back to scanning a range so consecutive deploys don't port-collide.
STAGING_PORTS = {
    "orchestrator": CONFIG.PORT_STAGING_ORCH,
    "coder":        CONFIG.PORT_STAGING_CODER,
    "worker":       CONFIG.PORT_STAGING_WORKER,
}

# Fallback range when the preferred staging port is occupied.
# Picked to be well clear of any production service ports (8000-8099)
# and ollama/qwen-router/dashboard (8501/9102/11434).
STAGING_FALLBACK_RANGE = (9013, 9100)


def _find_open_staging_port(service: str) -> int:
    """Return an open port for staging this service. Tries the configured
    preferred port first (typical case), falls back to scanning a range
    when the preferred is occupied (e.g. previous deploy still in its
    post-deploy-watchdog window before port-normalization runs).

    Raises RuntimeError if nothing is free in the fallback range.
    """
    import socket

    def _is_free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    preferred = STAGING_PORTS.get(service)
    if preferred and _is_free(preferred):
        return preferred

    lo, hi = STAGING_FALLBACK_RANGE
    for p in range(lo, hi):
        if _is_free(p):
            logger.info(
                f"STAGE: preferred port {preferred} occupied for {service}; "
                f"falling back to {p}"
            )
            return p
    raise RuntimeError(
        f"No open staging port found for {service} (preferred={preferred}, "
        f"range={STAGING_FALLBACK_RANGE})"
    )

# Production ports from CONFIG
SERVICE_SOURCES = {
    "orchestrator": {
        "src_dir": PROJECT_ROOT / "orchestrator",
        "shared_dirs": ["shared"],
        "entry_script": "orchestrator.py",
        "prod_port": CONFIG.PORT_ORCHESTRATOR,
    },
    "coder": {
        "src_dir": PROJECT_ROOT / "coder",
        "shared_dirs": ["shared"],
        "entry_script": "coder.py",
        "prod_port": CONFIG.PORT_CODER,
    },
    "worker": {
        "src_dir": PROJECT_ROOT / "worker",
        "shared_dirs": ["shared"],
        "entry_script": "worker.py",
        "prod_port": CONFIG.PORT_WORKER,
    },
}

HEALTH_CHECK_TIMEOUT = 15
HEALTH_CHECK_INTERVAL = 1


def _find_anchor(anchor: str, source: str):
    """Fuzzy anchor finder for patch application."""
    if anchor in source:
        return anchor
    stripped = anchor.strip()
    if stripped and stripped in source:
        return stripped
    collapsed = ' '.join(anchor.split())
    for line in source.splitlines():
        if ' '.join(line.split()) == collapsed:
            return line
    stripped_anchor = anchor.strip()
    if stripped_anchor and len(stripped_anchor) >= 10:
        anchor_id = stripped_anchor.split('=')[0].strip() if '=' in stripped_anchor else stripped_anchor.split('(')[0].strip()
        if anchor_id and len(anchor_id) >= 3:
            id_matches = [line for line in source.splitlines()
                          if line.strip().startswith(anchor_id) and ('=' in line or '(' in line)]
            if len(id_matches) == 1:
                return id_matches[0]
    return None


class DeployPhase(str, Enum):
    STAGING = "staging"
    BOOTING = "booting"
    TESTING = "testing"
    AWAITING_APPROVAL = "awaiting_approval"
    SWAPPING = "swapping"
    DRAINING = "draining"
    COMPLETING = "completing"
    COMPLETE = "complete"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass
class DeployState:
    deploy_id: str
    service: str
    phase: DeployPhase = DeployPhase.STAGING
    staging_dir: str = ""
    staging_port: int = 0
    staging_pid: int = 0
    prod_port: int = 0
    version: int = 0
    patches: List[dict] = field(default_factory=list)
    health_result: Optional[dict] = None
    error: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["phase"] = self.phase.value
        return d


_VERSION_FILE = PROJECT_ROOT / "state" / "deploy_versions.json"


def _load_versions() -> dict:
    if _VERSION_FILE.exists():
        try:
            with open(_VERSION_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_versions(versions: dict):
    _VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_VERSION_FILE, "w") as f:
        json.dump(versions, f, indent=2)


def _next_version(service: str) -> int:
    versions = _load_versions()
    new_ver = versions.get(service, 0) + 1
    versions[service] = new_ver
    _save_versions(versions)
    return new_ver


def stage_service(service: str, patches: Optional[List[dict]] = None) -> DeployState:
    """
    Copy a service's directory to staging/<service>_v<N>/.
    Optionally apply file patches to the staging copy.
    """
    if service not in SERVICE_SOURCES:
        return DeployState(
            deploy_id=f"deploy_{int(time.time())}",
            service=service,
            phase=DeployPhase.FAILED,
            error=f"Unknown service: {service}. Deployable: {list(SERVICE_SOURCES.keys())}",
            created_at=time.time(), updated_at=time.time(),
        )

    config = SERVICE_SOURCES[service]
    version = _next_version(service)
    try:
        staging_port = _find_open_staging_port(service)
    except RuntimeError as e:
        state = DeployState(
            deploy_id=f"{service}_v{version}_{int(time.time())}",
            service=service,
            version=version,
            staging_dir="",
            staging_port=0,
            prod_port=config.get("prod_port", 0),
            phase=DeployPhase.FAILED,
            error=f"Stage aborted: {e}",
            patches=[], created_at=time.time(), updated_at=time.time(),
        )
        return state
    deploy_id = f"{service}_v{version}_{int(time.time())}"
    staging_path = STAGING_DIR / f"{service}_v{version}"

    logger.info(f"STAGE: {service} v{version} → {staging_path}")

    state = DeployState(
        deploy_id=deploy_id, service=service, phase=DeployPhase.STAGING,
        staging_dir=str(staging_path), staging_port=staging_port,
        prod_port=config["prod_port"], version=version,
        patches=patches or [], created_at=time.time(), updated_at=time.time(),
    )

    try:
        # Clean old staging for this service
        for old_dir in STAGING_DIR.glob(f"{service}_v*"):
            if old_dir.is_dir():
                try:
                    shutil.rmtree(old_dir)
                except (PermissionError, Exception) as e:
                    logger.warning(f"Could not clean {old_dir.name}: {e}")

        STAGING_DIR.mkdir(parents=True, exist_ok=True)

        if "src_dir" in config:
            shutil.copytree(config["src_dir"], staging_path,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
            for shared_name in config.get("shared_dirs", []):
                shared_src = PROJECT_ROOT / shared_name
                shared_dst = staging_path / shared_name
                if shared_src.exists():
                    shutil.copytree(shared_src, shared_dst,
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif "src_files" in config:
            staging_path.mkdir(parents=True, exist_ok=True)
            for name, ftype in config["src_files"]:
                src = PROJECT_ROOT / name
                dst = staging_path / name
                if not src.exists():
                    state.phase = DeployPhase.FAILED
                    state.error = f"Source not found: {src}"
                    state.updated_at = time.time()
                    return state
                if ftype == "dir":
                    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                else:
                    shutil.copy2(src, dst)
        else:
            state.phase = DeployPhase.FAILED
            state.error = f"No src_dir or src_files in config for {service}"
            state.updated_at = time.time()
            return state

        # Apply patches
        if patches:
            for i, patch in enumerate(patches):
                file_path = staging_path / patch["file"]
                if not file_path.exists():
                    state.phase = DeployPhase.FAILED
                    state.error = f"Patch target not found: {patch['file']}"
                    state.updated_at = time.time()
                    return state

                content = file_path.read_text(encoding="utf-8")
                actual = _find_anchor(patch["find"], content)
                if actual is None:
                    state.phase = DeployPhase.FAILED
                    state.error = f"Patch anchor not found in {patch['file']}: {patch['find'][:80]}..."
                    state.updated_at = time.time()
                    return state

                content = content.replace(actual, patch["replace"], 1)
                file_path.write_text(content, encoding="utf-8")

                if file_path.suffix == '.py':
                    import ast
                    try:
                        ast.parse(content)
                    except SyntaxError as e:
                        state.phase = DeployPhase.FAILED
                        state.error = f"Patch caused syntax error in {patch['file']} line {e.lineno}: {e.msg}"
                        state.updated_at = time.time()
                        return state

        # Rewrite entry script for staging port
        entry_file = staging_path / config["entry_script"]
        if entry_file.exists():
            entry_content = entry_file.read_text(encoding="utf-8")
            # Match PORT = <literal>  OR  PORT = CONFIG.PORT_<NAME>
            new_content = re.sub(
                r'^(PORT\s*=\s*)(?:\d+|CONFIG\.PORT_\w+)',
                rf'\g<1>{staging_port}',
                entry_content,
                count=1,
                flags=re.MULTILINE,
            )
            if new_content == entry_content:
                # Fallback: uvicorn.run(..., port=<literal>) — only matches literal
                # ints. Most services use `port=PORT` referring back to the PORT
                # variable rewritten above; if that already fired, we're done.
                new_content = re.sub(r'(uvicorn\.run\(.+?port\s*=\s*)\d+',
                                     rf'\g<1>{staging_port}', entry_content,
                                     count=1, flags=re.DOTALL)

            # Path injection for staging. BOTH inserted lines carry the same
            # marker token so archive_and_promote can strip them cleanly post-
            # promote (otherwise the `import sys` prefix orphans and accumulates
            # one extra copy per deploy). 2026-05-21 fix.
            inject_marker = "# _DEPLOYER_STAGING_INJECT"
            path_inject = f'sys.path.insert(0, r"{staging_path}")  {inject_marker}'
            if inject_marker not in new_content:
                import_match = re.search(r'^(?:import |from )', new_content, re.MULTILINE)
                if import_match:
                    new_content = (new_content[:import_match.start()] +
                                   f"import sys  {inject_marker}\n" + path_inject + "\n\n" +
                                   new_content[import_match.start():])
            entry_file.write_text(new_content, encoding="utf-8")

        state.phase = DeployPhase.STAGING
        state.updated_at = time.time()
        return state

    except Exception as e:
        state.phase = DeployPhase.FAILED
        state.error = f"Staging failed: {str(e)}"
        state.updated_at = time.time()
        return state


def boot_staging(state: DeployState) -> DeployState:
    """Launch the staging instance."""
    if state.phase == DeployPhase.FAILED:
        return state

    state.phase = DeployPhase.BOOTING
    state.updated_at = time.time()

    config = SERVICE_SOURCES[state.service]
    staging_path = Path(state.staging_dir)
    entry_script = staging_path / config["entry_script"]

    if not entry_script.exists():
        state.phase = DeployPhase.FAILED
        state.error = f"Entry script not found: {entry_script}"
        state.updated_at = time.time()
        return state

    try:
        # Set PYTHONPATH so staged services can still import config.py + its
        # config.yaml/.env neighbours from PROJECT_ROOT. Staging copies of
        # shared/, capabilities/, etc. are still preferred because sys.path[0]
        # is set to staging_path via the path-inject during stage_service().
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

        # Redirect subprocess stdout+stderr to a per-deploy log file so we can
        # surface crash details if it dies before serving /health. The fd is
        # inherited by the child; the parent handle is closed after Popen.
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"staging_{state.deploy_id}.log"
        log_f = open(log_path, "w", encoding="utf-8")

        kwargs = {
            "cwd": str(staging_path),
            "env": env,
            "stdout": log_f,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        proc = subprocess.Popen([str(VENV_PYTHON), str(entry_script)], **kwargs)
        log_f.close()  # parent's copy of the fd; subprocess still owns its own
        state.staging_pid = proc.pid
        state.updated_at = time.time()
        logger.info(f"BOOT: {state.deploy_id} on port {state.staging_port} (PID {proc.pid})")

        # Fail-fast detection: poll briefly for early subprocess death. Catches
        # ImportErrors and other startup crashes that would otherwise burn the
        # full /health timeout in test_staging. 500ms is enough for Python to
        # finish module imports and either bind the socket or crash.
        time.sleep(0.5)
        exit_code = proc.poll()
        if exit_code is not None:
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-600:]
            except Exception:
                tail = ""
            state.phase = DeployPhase.FAILED
            state.error = f"Boot crashed early (exit={exit_code}). Log tail:\n{tail}"
            state.staging_pid = 0
            state.updated_at = time.time()
            logger.warning(f"BOOT FAILED EARLY: {state.deploy_id} exited {exit_code}")
        return state
    except Exception as e:
        state.phase = DeployPhase.FAILED
        state.error = f"Boot failed: {str(e)}"
        state.updated_at = time.time()
        return state


async def test_staging(state: DeployState) -> DeployState:
    """Wait for staging to respond to /health."""
    if state.phase == DeployPhase.FAILED:
        return state

    state.phase = DeployPhase.TESTING
    state.updated_at = time.time()

    url = f"http://127.0.0.1:{state.staging_port}/health"
    logger.info(f"TEST: Checking {url} (timeout {HEALTH_CHECK_TIMEOUT}s)")

    start = time.time()
    last_error = None

    while time.time() - start < HEALTH_CHECK_TIMEOUT:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    if resp.status == 200:
                        body = await resp.json()
                        state.health_result = {
                            "status": resp.status, "body": body,
                            "latency_ms": round((time.time() - start) * 1000),
                        }
                        state.phase = DeployPhase.AWAITING_APPROVAL
                        state.updated_at = time.time()
                        logger.info(f"Healthy! Responded in {state.health_result['latency_ms']}ms")
                        return state
                    else:
                        last_error = f"HTTP {resp.status}"
        except Exception as e:
            last_error = str(e)
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)

    state.phase = DeployPhase.FAILED
    state.error = f"Health check timeout after {HEALTH_CHECK_TIMEOUT}s. Last error: {last_error}"
    state.updated_at = time.time()
    kill_staging(state)
    return state


def kill_staging(state: DeployState):
    if not state.staging_pid:
        return
    try:
        import psutil
        if psutil.pid_exists(state.staging_pid):
            proc = psutil.Process(state.staging_pid)
            for child in proc.children(recursive=True):
                try: child.kill()
                except Exception: pass
            proc.kill()
            proc.wait(timeout=5)
    except Exception as e:
        logger.warning(f"Failed to kill staging PID {state.staging_pid}: {e}")


def kill_production(service: str) -> dict:
    pid_file = PROJECT_ROOT / "active_pids.json"
    if not pid_file.exists():
        return {"killed": False, "reason": "No PID file"}
    try:
        import psutil
        with open(pid_file, "r") as f:
            pids = json.load(f)
        pid = pids.get(service)
        if not pid:
            return {"killed": False, "reason": f"No PID for {service}"}
        if not psutil.pid_exists(pid):
            return {"killed": False, "reason": f"PID {pid} already dead"}
        proc = psutil.Process(pid)
        for child in proc.children(recursive=True):
            try: child.kill()
            except Exception: pass
        proc.kill()
        proc.wait(timeout=5)
        return {"killed": True, "pid": pid}
    except Exception as e:
        return {"killed": False, "reason": str(e)}


def archive_and_promote(state: DeployState):
    """Archive old production files and promote staging copy."""
    config = SERVICE_SOURCES[state.service]
    staging_path = Path(state.staging_dir)

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = ARCHIVE_DIR / f"{state.service}_pre_v{state.version}_{int(time.time())}"
    archive_path.mkdir(parents=True, exist_ok=True)

    try:
        if "src_dir" in config:
            prod_dir = config["src_dir"]
            if prod_dir.exists():
                shutil.move(str(prod_dir), str(archive_path / prod_dir.name))

            shared_dirs = set(config.get("shared_dirs", []))
            def ignore_shared(directory, contents):
                if Path(directory) == staging_path:
                    return [c for c in contents if c in shared_dirs]
                return []
            shutil.copytree(str(staging_path), str(prod_dir), ignore=ignore_shared)

            for shared_name in config.get("shared_dirs", []):
                staged_shared = staging_path / shared_name
                prod_shared = PROJECT_ROOT / shared_name
                if staged_shared.exists():
                    if prod_shared.exists():
                        shutil.move(str(prod_shared), str(archive_path / shared_name))
                    shutil.copytree(str(staged_shared), str(prod_shared))

            # Restore production port in promoted copy
            entry_file = prod_dir / config["entry_script"]
            if entry_file.exists():
                content = entry_file.read_text(encoding="utf-8")
                new_content = re.sub(r'^(PORT\s*=\s*)\d+', rf'\g<1>{config["prod_port"]}',
                                     content, count=1, flags=re.MULTILINE)
                if new_content == content:
                    new_content = re.sub(r'(uvicorn\.run\(.+?port\s*=\s*)\d+',
                                         rf'\g<1>{config["prod_port"]}', content,
                                         count=1, flags=re.DOTALL)
                # Strip ALL lines ending in the deployer-staging-inject marker.
                # This catches both the `import sys` prefix and the path_inject
                # line that stage_service adds together. Falls back to the old
                # comment marker for any legacy staging dirs still in flight.
                clean = re.sub(r'^.*?#\s*_DEPLOYER_STAGING_INJECT\s*$\n?',
                               '', new_content, flags=re.MULTILINE)
                clean = re.sub(r'sys\.path\.insert\(0, r".*?"\)  # Injected by deployer for staging\n',
                               '', clean)
                entry_file.write_text(clean, encoding="utf-8")

        elif "src_files" in config:
            for name, ftype in config["src_files"]:
                src = PROJECT_ROOT / name
                if src.exists():
                    if ftype == "dir":
                        shutil.move(str(src), str(archive_path / name))
                    else:
                        shutil.copy2(src, archive_path / name)
                        src.unlink()

            for name, ftype in config["src_files"]:
                staged = staging_path / name
                dest = PROJECT_ROOT / name
                if staged.exists():
                    if ftype == "dir":
                        shutil.copytree(str(staged), str(dest))
                    else:
                        shutil.copy2(staged, dest)

            entry_file = PROJECT_ROOT / config["entry_script"]
            if entry_file.exists():
                content = entry_file.read_text(encoding="utf-8")
                new_content = re.sub(r'(uvicorn\.run\(.+?port\s*=\s*)\d+',
                                     rf'\g<1>{config["prod_port"]}', content,
                                     count=1, flags=re.DOTALL)
                # Same dual-strip as the src_dir branch above (2026-05-21 fix
                # for accumulating `import sys` cruft per deploy cycle).
                clean = re.sub(r'^.*?#\s*_DEPLOYER_STAGING_INJECT\s*$\n?',
                               '', new_content, flags=re.MULTILINE)
                clean = re.sub(r'sys\.path\.insert\(0, r".*?"\)  # Injected by deployer for staging\n',
                               '', clean)
                entry_file.write_text(clean, encoding="utf-8")

    except Exception as e:
        logger.error(f"Archive/promote failed: {e}")


async def execute_rollback(state: DeployState) -> DeployState:
    """Emergency rollback after failed deploy."""
    logger.info(f"ROLLBACK: Rolling back {state.service} v{state.version}")

    swap_result = await swap_gateway(state.service, state.prod_port)
    if not swap_result.get("success"):
        logger.error(f"Gateway rollback failed: {swap_result.get('error')}")

    kill_staging(state)

    try:
        config = SERVICE_SOURCES[state.service]
        if "src_dir" in config:
            entry_script = config["src_dir"] / config["entry_script"]
            cwd = config["src_dir"]
        else:
            entry_script = PROJECT_ROOT / config["entry_script"]
            cwd = PROJECT_ROOT

        if entry_script and entry_script.exists():
            kwargs = {"cwd": str(cwd)}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            proc = subprocess.Popen([str(VENV_PYTHON), str(entry_script)], **kwargs)
            logger.info(f"Relaunched {state.service} from production files (PID {proc.pid})")

            url = f"http://127.0.0.1:{state.prod_port}/health"
            start = time.time()
            while time.time() - start < HEALTH_CHECK_TIMEOUT:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                            if resp.status == 200:
                                logger.info(f"Rollback complete — {state.service} restored on port {state.prod_port}")
                                break
                except:
                    pass
                await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"Relaunch failed: {e}")

    state.phase = DeployPhase.FAILED
    state.error = "Auto-rollback triggered: post-deploy health check failed"
    state.updated_at = time.time()
    return state


async def swap_gateway(service: str, new_port: int) -> dict:
    """Call the Gateway's swap endpoint to redirect traffic."""
    url = f"http://127.0.0.1:{CONFIG.PORT_GATEWAY}/gateway/swap"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={"service": service, "port": new_port},
                                    timeout=aiohttp.ClientTimeout(total=5)) as resp:
                result = await resp.json()
                if result.get("success"):
                    logger.info(f"Gateway swap complete: {service} → port {new_port}")
                else:
                    logger.error(f"Gateway swap failed: {result}")
                return result
    except Exception as e:
        logger.error(f"Gateway swap error: {e}")
        return {"success": False, "error": str(e)}


async def prepare_deployment(service: str, patches: Optional[List[dict]] = None) -> DeployState:
    """Run the staging pipeline (steps 1-4). Returns state ready for human approval."""
    logger.info(f"DEPLOY PIPELINE: {service} ({len(patches) if patches else 0} patches)")

    _bb_log("deploy_staged", {"service": service, "patches": len(patches) if patches else 0},
            source="deployer")

    state = stage_service(service, patches)
    if state.phase == DeployPhase.FAILED:
        return state

    state = boot_staging(state)
    if state.phase == DeployPhase.FAILED:
        return state

    state = await test_staging(state)
    return state


async def execute_swap(state: DeployState, drain_seconds: int = 3) -> DeployState:
    """Execute the swap after human approval. Called ONLY by the Watchtower's HIL gate."""
    state.phase = DeployPhase.SWAPPING
    state.updated_at = time.time()

    swap_result = await swap_gateway(state.service, state.staging_port)
    if not swap_result.get("success"):
        state.phase = DeployPhase.FAILED
        state.error = f"Gateway swap failed: {swap_result.get('error')}"
        state.updated_at = time.time()
        kill_staging(state)
        return state

    state.phase = DeployPhase.DRAINING
    state.updated_at = time.time()
    await asyncio.sleep(drain_seconds)

    state.phase = DeployPhase.COMPLETING
    state.updated_at = time.time()
    kill_production(state.service)
    archive_and_promote(state)

    state.phase = DeployPhase.COMPLETE
    state.updated_at = time.time()
    logger.info(f"DEPLOY COMPLETE: {state.deploy_id} — {state.service} v{state.version} live on port {state.staging_port}")
    return state
