"""
THE WATCHTOWER (System Supervisor)
------------------------
24/7 loop that manages the task backlog with human-in-the-loop approval.
Service restart endpoint — the Watchtower is the Hypervisor.
Blue-green deploy pipeline with post-deploy watchdog and auto-rollback.

THIS FILE IS IMMUTABLE — the agent cannot modify it.
The supervisor must never be supervised by the thing it supervises.
"""
import time
import sys
import os
import aiohttp
import asyncio
import json
import subprocess
import psutil
from pathlib import Path

# Fix imports
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import CONFIG
from shared.task_registry import TaskRegistry
from shared.schemas import TaskType, TaskStatus
from shared.infrastructure_monitor import InfrastructureMonitor
from shared.lifecycle import register_process
from shared.logger import get_logger
from shared.activity import set_active_task, set_thought, clear_activity, set_status
from shared.deployer import (
    prepare_deployment,
    execute_swap,
    execute_rollback,
    kill_staging,
    DeployState,
    DeployPhase,
)
from shared.kill_switch import is_engaged as _kill_switch_engaged
from shared.blackbox import log_event as _bb_log
from shared.roux_client import roux as _roux

from fastapi import FastAPI, Body
import uvicorn

# Initialize logger
logger = get_logger("watchtower")

# Configuration — all from CONFIG
ORCHESTRATOR_URL = f"http://localhost:{CONFIG.PORT_ORCHESTRATOR}/chat"
CODER_URL = f"http://localhost:{CONFIG.PORT_CODER}/diagnose"
CHECK_INTERVAL = 10
MAX_RETRIES = 3
WATCHTOWER_PORT = CONFIG.PORT_WATCHTOWER

# --- PROJECT PATHS ---
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
PID_FILE = PROJECT_ROOT / "active_pids.json"

# Cross-platform Python executable detection
_VENV_DIR = PROJECT_ROOT / "venv"
if (_VENV_DIR / "Scripts" / "python.exe").exists():
    VENV_PYTHON = _VENV_DIR / "Scripts" / "python.exe"   # Windows
elif (_VENV_DIR / "bin" / "python").exists():
    VENV_PYTHON = _VENV_DIR / "bin" / "python"           # Linux/Mac
else:
    VENV_PYTHON = sys.executable                          # Fallback: current interpreter

# Service launch configurations: name -> (script_path, working_dir, port)
SERVICE_CONFIG = {
    "orchestrator": {
        "script": PROJECT_ROOT / "orchestrator" / "orchestrator.py",
        "cwd": PROJECT_ROOT / "orchestrator",
        "port": CONFIG.PORT_ORCHESTRATOR,
    },
    "coder": {
        "script": PROJECT_ROOT / "coder" / "coder.py",
        "cwd": PROJECT_ROOT / "coder",
        "port": CONFIG.PORT_CODER,
    },
    "worker": {
        "script": PROJECT_ROOT / "worker" / "worker.py",
        "cwd": PROJECT_ROOT,
        "port": CONFIG.PORT_WORKER,
    },
    "gateway": {
        "script": PROJECT_ROOT / "gateway" / "gateway.py",
        "cwd": PROJECT_ROOT / "gateway",
        "port": CONFIG.PORT_GATEWAY,
    },
}
# Note: watchtower is NOT in this list — it cannot restart itself
# Note: gateway CAN be restarted but CANNOT be blue-green deployed


# =====================================================
#  SERVICE MANAGEMENT (The Hypervisor)
# =====================================================

def _read_pids() -> dict:
    """Read the active PIDs registry"""
    if not PID_FILE.exists():
        return {}
    try:
        with open(PID_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return {}

def _kill_service(service_name: str) -> dict:
    """Kill a service by name using its registered PID"""
    pids = _read_pids()
    pid = pids.get(service_name)

    if not pid:
        return {"killed": False, "reason": f"No PID found for {service_name}"}

    try:
        if not psutil.pid_exists(pid):
            return {"killed": False, "reason": f"{service_name} (PID {pid}) already dead"}

        proc = psutil.Process(pid)
        for child in proc.children(recursive=True):
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        proc.kill()
        proc.wait(timeout=5)
        logger.info(f"Killed {service_name} (PID {pid})")
        return {"killed": True, "pid": pid}
    except Exception as e:
        logger.error(f"Failed to kill {service_name}: {e}")
        return {"killed": False, "reason": str(e)}

def _launch_service(service_name: str) -> dict:
    """Launch a service in a new process"""
    config = SERVICE_CONFIG.get(service_name)
    if not config:
        return {"launched": False, "reason": f"Unknown service: {service_name}"}

    script = config["script"]
    cwd = config["cwd"]

    if not script.exists():
        return {"launched": False, "reason": f"Script not found: {script}"}

    try:
        # Cross-platform launch
        kwargs = {"cwd": str(cwd)}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            [str(VENV_PYTHON), str(script)],
            **kwargs,
        )
        logger.info(f"Launched {service_name} (new PID {proc.pid})")
        return {"launched": True, "pid": proc.pid}
    except Exception as e:
        logger.error(f"Failed to launch {service_name}: {e}")
        return {"launched": False, "reason": str(e)}

async def _wait_for_service(service_name: str, timeout: int = 15) -> bool:
    """Wait for a service to respond on its port"""
    config = SERVICE_CONFIG.get(service_name)
    if not config:
        return False

    port = config["port"]
    start = time.time()

    while time.time() - start < timeout:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://localhost:{port}/health", timeout=2) as resp:
                    if resp.status == 200:
                        logger.info(f"{service_name} is healthy on port {port}")
                        return True
        except:
            pass
        await asyncio.sleep(1)

    logger.warning(f"{service_name} did not respond within {timeout}s")
    return False


# =====================================================
#  TASK RESULT ESCROW
#  When a task restarts the Orchestrator, the Worker deposits
#  the result here so the new Orchestrator can pick it up.
# =====================================================

_pending_results = []
_queue_snapshot = {}


# =====================================================
#  FASTAPI APP (Restart + Escrow + Queue Snapshot + Deploy)
# =====================================================

wt_app = FastAPI(title="Watchtower Supervisor — Blue-Green Deploy")

@wt_app.get("/health")
async def wt_health():
    return {"status": "ok", "role": "supervisor"}

@wt_app.post("/task_result")
async def deposit_task_result(result: dict = Body(...)):
    """Worker deposits task results here when Orchestrator will be restarted."""
    _pending_results.append({
        "result": result,
        "timestamp": time.time()
    })
    logger.info(f"ESCROW: Stored task result ({len(_pending_results)} pending)")
    return {"stored": True, "pending_count": len(_pending_results)}

@wt_app.get("/pending_results")
async def get_pending_results():
    """Orchestrator calls this on startup to retrieve escrowed results."""
    results = list(_pending_results)
    _pending_results.clear()
    if results:
        logger.info(f"ESCROW: Delivered {len(results)} pending result(s) to Orchestrator")
    return {"results": results}

@wt_app.post("/queue_snapshot")
async def store_queue_snapshot(data: dict = Body(...)):
    """Orchestrator sends queue state here on every change."""
    global _queue_snapshot
    _queue_snapshot = data
    return {"stored": True}

@wt_app.get("/queue_snapshot")
async def get_queue_snapshot():
    """Orchestrator retrieves queue state on startup."""
    snapshot = _queue_snapshot.copy()
    if snapshot:
        logger.info(f"Queue snapshot delivered ({len(snapshot.get('pending', []))} pending)")
    return {"snapshot": snapshot}


# =====================================================
#  BLUE-GREEN DEPLOY (HIL GATES)
#  The deployer module handles staging (copy, patch, boot, test).
#  These endpoints are the IMMUTABLE gates that require human
#  approval before swapping live traffic or killing production.
#  The agent CANNOT bypass these gates.
# =====================================================

_pending_deploys: dict = {}
_deploy_history: list = []

# Post-deploy watchdog configuration
POST_DEPLOY_WATCH_SECONDS = 60
POST_DEPLOY_CHECK_INTERVAL = 5
POST_DEPLOY_MAX_FAILURES = 3
_active_watchdogs: dict = {}


@wt_app.post("/deploy/{service_name}")
async def start_deploy(service_name: str, body: dict = Body(default={})):
    """
    Start a blue-green deployment pipeline.
    Runs staging (copy, patch, boot, health-check), then
    parks the deploy in AWAITING_APPROVAL for human review.
    """
    if _kill_switch_engaged():
        logger.warning(f"DEPLOY BLOCKED: Kill switch engaged — rejecting deploy for {service_name}")
        return {"success": False, "error": "Kill switch engaged. Disengage to allow deployments."}

    if service_name == "watchtower":
        return {"success": False, "error": "The Watchtower is immutable. It cannot be deployed."}
    if service_name == "gateway":
        return {"success": False, "error": "Gateway is infrastructure. Manual upgrades only."}

    for d in _pending_deploys.values():
        if d.service == service_name and d.phase == DeployPhase.AWAITING_APPROVAL:
            return {
                "success": False,
                "error": f"Deploy already pending for {service_name}: {d.deploy_id}",
            }

    patches = body.get("patches", [])
    logger.info(f"DEPLOY: Starting pipeline for {service_name} ({len(patches)} patches)")

    state = await prepare_deployment(service_name, patches)

    if state.phase == DeployPhase.FAILED:
        logger.error(f"DEPLOY FAILED during staging: {state.error}")
        return {
            "success": False,
            "deploy_id": state.deploy_id,
            "phase": state.phase.value,
            "error": state.error,
        }

    _pending_deploys[state.deploy_id] = state
    logger.info(f"DEPLOY STAGED: {state.deploy_id} — awaiting human approval")
    await _roux.deploy_staged(state.service, state.version)

    return {
        "success": True,
        "deploy_id": state.deploy_id,
        "phase": state.phase.value,
        "service": state.service,
        "version": state.version,
        "staging_port": state.staging_port,
        "health": state.health_result,
        "message": "Deploy staged. Approve in the dashboard to swap live traffic.",
    }


@wt_app.get("/deploy/pending")
async def get_pending_deploys():
    """Dashboard polls this to show the approval card."""
    pending = [
        d.to_dict() for d in _pending_deploys.values()
        if d.phase == DeployPhase.AWAITING_APPROVAL
    ]
    return {"pending": pending, "count": len(pending)}


async def _post_deploy_watchdog(state: DeployState):
    """
    Post-deploy health monitoring.
    Runs for POST_DEPLOY_WATCH_SECONDS after a successful swap.
    If health checks fail POST_DEPLOY_MAX_FAILURES times in a row,
    triggers automatic rollback.
    """
    deploy_id = state.deploy_id
    port = state.staging_port
    url = f"http://127.0.0.1:{port}/health"

    logger.info(f"WATCHDOG: Monitoring {state.service} v{state.version} for {POST_DEPLOY_WATCH_SECONDS}s")

    consecutive_failures = 0
    checks_done = 0
    start = time.time()

    while time.time() - start < POST_DEPLOY_WATCH_SECONDS:
        await asyncio.sleep(POST_DEPLOY_CHECK_INTERVAL)
        checks_done += 1

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status == 200:
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                        logger.warning(f"WATCHDOG: Health check returned {resp.status} ({consecutive_failures}/{POST_DEPLOY_MAX_FAILURES})")
        except Exception as e:
            consecutive_failures += 1
            logger.warning(f"WATCHDOG: Health check failed: {e} ({consecutive_failures}/{POST_DEPLOY_MAX_FAILURES})")

        if consecutive_failures >= POST_DEPLOY_MAX_FAILURES:
            logger.error(f"WATCHDOG: {POST_DEPLOY_MAX_FAILURES} consecutive failures — triggering AUTO-ROLLBACK")
            await _roux.deploy_rolled_back(state.service, reason="watchdog health failures")
            _bb_log("deploy_rolled_back", {
                "deploy_id": deploy_id, "service": state.service,
                "version": state.version, "reason": "watchdog_health_failures",
            }, source="watchtower")

            rolled_back = await execute_rollback(state)

            for i, h in enumerate(_deploy_history):
                if h.get("deploy_id") == deploy_id:
                    _deploy_history[i] = rolled_back.to_dict()
                    break

            _active_watchdogs.pop(deploy_id, None)
            logger.info(f"WATCHDOG: Rollback complete for {deploy_id}")
            return

    # Monitoring window complete — all good
    _active_watchdogs.pop(deploy_id, None)
    elapsed = round(time.time() - start)
    logger.info(f"WATCHDOG: {state.service} v{state.version} stable after {elapsed}s ({checks_done} checks passed). All clear.")

    # Post-deploy port normalization
    prod_port = state.prod_port
    staging_port = state.staging_port
    service = state.service

    if prod_port != staging_port:
        logger.info(f"WATCHDOG: Normalizing {service} from staging port {staging_port} -> production port {prod_port}")

        from shared.deployer import swap_gateway
        swap_result = await swap_gateway(service, prod_port)
        if not swap_result.get("success"):
            logger.error(f"Gateway swap to prod port failed: {swap_result.get('error')}")
            logger.info(f"Service remains on staging port {staging_port} — manual restart needed")
            return

        kill_staging(state)
        logger.info(f"Killed staging process (PID {state.staging_pid})")

        await asyncio.sleep(2)

        launch_result = _launch_service(service)
        if not launch_result.get("launched"):
            logger.error(f"Failed to relaunch on prod port: {launch_result.get('reason')}")
            logger.info(f"Service DOWN — manual intervention needed")
            return

        healthy = await _wait_for_service(service, timeout=15)
        if healthy:
            logger.info(f"{service} normalized: live on port {prod_port} (PID {launch_result['pid']})")
        else:
            logger.error(f"{service} not healthy on port {prod_port} after relaunch")


@wt_app.get("/deploy/watchdog")
async def get_watchdog_status():
    """Dashboard can poll this to show watchdog status."""
    active = {}
    for did, task in _active_watchdogs.items():
        active[did] = {"running": not task.done(), "deploy_id": did}
    return {"watchdogs": active, "count": len(active)}


@wt_app.post("/deploy/approve/{deploy_id}")
async def approve_deploy(deploy_id: str):
    """
    IMMUTABLE HIL GATE: Human approves the swap.
    This is the ONLY path to redirect live traffic.
    """
    state = _pending_deploys.get(deploy_id)
    if not state:
        return {"success": False, "error": f"Deploy {deploy_id} not found"}
    if state.phase != DeployPhase.AWAITING_APPROVAL:
        return {"success": False, "error": f"Deploy is in phase {state.phase.value}, not awaiting approval"}

    logger.info(f"DEPLOY APPROVED by human: {deploy_id}")
    _bb_log("deploy_approved", {
        "deploy_id": deploy_id, "service": state.service,
        "version": state.version, "staging_port": state.staging_port,
    }, source="watchtower")

    state = await execute_swap(state)

    _pending_deploys.pop(deploy_id, None)
    _deploy_history.append(state.to_dict())
    if len(_deploy_history) > 20:
        _deploy_history.pop(0)

    if state.phase == DeployPhase.COMPLETE:
        logger.info(f"DEPLOY COMPLETE: {deploy_id}")
        await _roux.deploy_complete(state.service, state.version)

        watchdog_task = asyncio.create_task(_post_deploy_watchdog(state))
        _active_watchdogs[deploy_id] = watchdog_task

        return {
            "success": True,
            "deploy_id": deploy_id,
            "phase": state.phase.value,
            "message": f"{state.service} v{state.version} is now live! Monitoring for {POST_DEPLOY_WATCH_SECONDS}s.",
            "watchdog": True,
        }
    else:
        logger.error(f"DEPLOY SWAP FAILED: {state.error}")
        return {
            "success": False,
            "deploy_id": deploy_id,
            "phase": state.phase.value,
            "error": state.error,
        }


@wt_app.post("/deploy/reject/{deploy_id}")
async def reject_deploy(deploy_id: str):
    """Human rejects the deploy — tear down staging, keep production."""
    state = _pending_deploys.get(deploy_id)
    if not state:
        return {"success": False, "error": f"Deploy {deploy_id} not found"}

    logger.info(f"DEPLOY REJECTED by human: {deploy_id}")
    _bb_log("deploy_rejected", {
        "deploy_id": deploy_id, "service": state.service,
    }, source="watchtower")

    kill_staging(state)
    state.phase = DeployPhase.REJECTED
    state.updated_at = time.time()

    _pending_deploys.pop(deploy_id, None)
    _deploy_history.append(state.to_dict())
    if len(_deploy_history) > 20:
        _deploy_history.pop(0)

    return {
        "success": True,
        "deploy_id": deploy_id,
        "phase": "rejected",
        "message": f"Deploy rejected. {state.service} production unchanged.",
    }


@wt_app.get("/deploy/history")
async def get_deploy_history():
    """Recent deploy history (last 20)."""
    return {"deploys": _deploy_history, "count": len(_deploy_history)}


@wt_app.get("/services")
async def list_services():
    """Show status of all managed services"""
    pids = _read_pids()
    status = {}
    for name in SERVICE_CONFIG:
        pid = pids.get(name)
        alive = psutil.pid_exists(pid) if pid else False
        status[name] = {"pid": pid, "alive": alive, "port": SERVICE_CONFIG[name]["port"]}
    return status

@wt_app.post("/restart/{service_name}")
async def restart_service(service_name: str):
    """Kill and relaunch a service. The core hypervisor capability."""
    if service_name == "watchtower":
        return {"success": False, "error": "The Watchtower cannot restart itself. Nice try."}

    if service_name not in SERVICE_CONFIG:
        return {"success": False, "error": f"Unknown service: {service_name}. Available: {list(SERVICE_CONFIG.keys())}"}

    logger.info(f"RESTART requested for: {service_name}")
    await _roux.service_crash(service_name, restarting=True)

    kill_result = _kill_service(service_name)
    logger.info(f"   Kill: {kill_result}")

    await asyncio.sleep(2)

    launch_result = _launch_service(service_name)
    logger.info(f"   Launch: {launch_result}")

    if not launch_result.get("launched"):
        await _roux.say(f"{service_name} restart failed. Could not relaunch.", priority="critical")
        return {"success": False, "error": f"Failed to launch: {launch_result.get('reason')}"}

    healthy = await _wait_for_service(service_name)

    if healthy:
        await _roux.service_restarted(service_name)
    else:
        await _roux.say(f"{service_name} launched but not responding. Might need a look.", priority="critical")

    return {
        "success": healthy,
        "service": service_name,
        "kill": kill_result,
        "launch": launch_result,
        "healthy": healthy
    }

async def execute_task(task):
    """Execute a task and return (success, error_details)"""
    logger.info(f"Executing '{task.title}'...")

    context = task.description
    if task.user_response:
        context += f"\n\nUSER PROVIDED: {task.user_response}"

    async with aiohttp.ClientSession() as session:
        payload = {"query": f"TASK: {task.title}. \nCONTEXT: {context}"}
        try:
            async with session.post(ORCHESTRATOR_URL, json=payload, timeout=None) as resp:
                result = await resp.json()
                if result.get("success"):
                    logger.info("Task Success!")
                    return True, None
                else:
                    error = result.get('error', 'Unknown error')
                    logger.error(f"Task Failed: {error}")
                    return False, error
        except Exception as e:
            logger.error(f"Connection Error: {e}")
            return False, str(e)

async def diagnose_failure(task, error_details):
    """Send the error to Coder's /diagnose endpoint for analysis"""
    logger.info(f"Diagnosing failure for '{task.title}'...")

    diagnosis_prompt = f"""A task failed with the following error. Analyze it and respond ONLY with valid JSON.

TASK: {task.title}
DESCRIPTION: {task.description}
ERROR: {error_details}

Respond with JSON in this exact format:
{{
    "needs_user_input": true/false,
    "question": "The specific question to ask the user (if needs_user_input is true)",
    "diagnosis": "Brief explanation of what went wrong"
}}

Respond ONLY with the JSON object, no markdown, no explanation."""

    async with aiohttp.ClientSession() as session:
        payload = {"prompt": diagnosis_prompt}
        try:
            async with session.post(CODER_URL, json=payload, timeout=60) as resp:
                if resp.status != 200:
                    logger.warning(f"Coder returned {resp.status}")
                    return {"needs_user_input": False, "question": None, "diagnosis": "Coder error"}

                result = await resp.json()
                response_text = result.get('response', '{}')

                import re
                try:
                    if '```json' in response_text:
                        response_text = response_text.split('```json')[1].split('```')[0]
                    elif '```' in response_text:
                        response_text = response_text.split('```')[1].split('```')[0]

                    if '<think>' in response_text:
                        response_text = response_text.split('</think>')[-1]

                    match = re.search(r'\{[\s\S]*\}', response_text)
                    if match:
                        response_text = match.group(0)

                    diagnosis = json.loads(response_text.strip())
                    return diagnosis
                except json.JSONDecodeError:
                    return {"needs_user_input": False, "question": None, "diagnosis": "Could not parse diagnosis"}
        except Exception as e:
            return {"needs_user_input": False, "question": None, "diagnosis": str(e)}

def check_system_health(registry):
    return False

async def run_loop():
    registry = TaskRegistry()
    monitor = InfrastructureMonitor()

    logger.info("THE WATCHTOWER: Online and observing...")
    logger.info(f"Polling interval: {CHECK_INTERVAL}s")

    tick_counter = 0

    while True:
        registry._load()
        tick_counter += 1

        if check_system_health(registry):
            logger.warning("Critical Health Issue Detected! Auto-escalating...")

        # Memory decay (every ~1 hour / 360 ticks)
        if tick_counter % 360 == 0:
            try:
                from shared.memory import run_decay
                run_decay(apply=True, force=True)
                logger.info("MEMORY DECAY: Automatic maintenance complete")
            except Exception as e:
                logger.warning(f"Memory decay error: {e}")

        # Infrastructure scan (every 10 mins / 60 ticks)
        if tick_counter == 1 or tick_counter % 60 == 0:
            try:
                local_res = monitor.get_local_resources()
                network_devs = monitor.scan_network()
                opportunities = monitor.identify_opportunities(local_res, network_devs)

                for opp in opportunities:
                    registry.add_task(
                        title=opp['title'],
                        description=opp['description'],
                        type=opp['type'],
                        priority=opp['priority']
                    )
            except Exception as e:
                logger.warning(f"Infra scan error: {e}")

        # Execute tasks
        next_task = registry.get_next_approved_task()

        if next_task:
            logger.info(f"Picked up '{next_task.title}'")

            set_active_task(next_task.id, next_task.title, "watchtower")
            set_thought(f"Starting: {next_task.title[:60]}...")

            retry_count = getattr(next_task, 'retry_count', 0) or 0
            if retry_count > 0:
                logger.info(f"Retry attempt #{retry_count}")
                set_thought(f"Retrying (attempt #{retry_count}): {next_task.title[:50]}...")

            registry.update_status(next_task.id, TaskStatus.IN_PROGRESS)
            success, error_details = await execute_task(next_task)

            if success:
                registry.update_status(next_task.id, TaskStatus.COMPLETED)
            else:
                if retry_count >= MAX_RETRIES:
                    logger.error(f"Max retries ({MAX_RETRIES}) exceeded. Marking FAILED.")
                    registry.update_status(next_task.id, TaskStatus.FAILED)
                else:
                    logger.info("Analyzing failure...")
                    diagnosis = await diagnose_failure(next_task, error_details)

                    if diagnosis.get("needs_user_input"):
                        question = diagnosis.get("question", "What information do you need to provide?")
                        logger.info(f"Task needs user input: {question}")
                        set_status("blocked")
                        set_thought(f"Waiting for input: {question[:80]}...")
                        registry.block_task(
                            next_task.id,
                            reason=error_details[:500] if error_details else "Unknown error",
                            question=question
                        )
                    else:
                        logger.error("Task failed (no user input can fix it)")
                        set_status("failed")
                        set_thought(f"Task failed: {diagnosis.get('diagnosis', 'Unknown')[:80]}")
                        registry.update_status(next_task.id, TaskStatus.FAILED)
        else:
            blocked_tasks = [t for t in registry.tasks if t.status == TaskStatus.BLOCKED]

            if blocked_tasks:
                if tick_counter % 30 == 0:
                    logger.info(f"{len(blocked_tasks)} task(s) waiting for your input in the dashboard")
                    set_status("blocked")
                    set_thought(f"{len(blocked_tasks)} task(s) blocked - awaiting your input...")
            else:
                clear_activity()

            sys.stdout.write(".")
            sys.stdout.flush()

        await asyncio.sleep(CHECK_INTERVAL)

async def start_api_server():
    """Run the FastAPI server alongside the main loop"""
    config = uvicorn.Config(wt_app, host="0.0.0.0", port=WATCHTOWER_PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    """Run both the Watchtower loop and the API server concurrently"""
    logger.info(f"Supervisor API on port {WATCHTOWER_PORT}")
    await asyncio.gather(
        run_loop(),
        start_api_server(),
    )

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info(" THE WATCHTOWER — System Supervisor")
    logger.info("=" * 60)
    register_process("watchtower")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Watchtower stopped by user.")
