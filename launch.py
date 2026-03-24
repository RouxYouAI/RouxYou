"""
launch.py — Start all RouxYou services in dependency order.

Each service is started in a new terminal window (Windows) or
background process (macOS/Linux), then health-checked before
the next service launches.

Usage:
    python launch.py              # Start everything
    python launch.py --no-roux    # Skip Roux voice service
    python launch.py --no-dash    # Skip dashboard (headless mode)
    python launch.py --only rag   # Start a single service by key
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
IS_WINDOWS   = os.name == "nt"

# Prefer venv python, fall back to system python
if IS_WINDOWS:
    PYTHON = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"
else:
    PYTHON = PROJECT_ROOT / "venv" / "bin" / "python"

if not PYTHON.exists():
    PYTHON = Path(sys.executable)


# ---------------------------------------------------------------------------
# Service definitions — started in order
# ---------------------------------------------------------------------------

SERVICES = [
    {
        "name":   "Memory Agent",
        "key":    "memory",
        "script": "memory/memory_agent.py",
        "cwd":    "memory",
        "port":   8004,
        "health": "/health",
        "delay":  5,
    },
    {
        "name":   "RAG API",
        "key":    "rag",
        "script": "memory/http_api.py",
        "cwd":    "memory",
        "port":   8011,
        "health": "/health",
        "delay":  3,
    },
    {
        "name":   "Gateway",
        "key":    "gateway",
        "script": "gateway/gateway.py",
        "cwd":    None,
        "port":   8000,
        "health": "/health",
        "delay":  3,
    },
    {
        "name":   "Coder",
        "key":    "coder",
        "script": "coder/coder.py",
        "cwd":    "coder",
        "port":   8002,
        "health": "/health",
        "delay":  5,
    },
    {
        "name":   "Worker",
        "key":    "worker",
        "script": "worker/worker.py",
        "cwd":    "worker",
        "port":   8003,
        "health": "/health",
        "delay":  3,
    },
    {
        "name":   "Orchestrator",
        "key":    "orchestrator",
        "script": "orchestrator/orchestrator.py",
        "cwd":    "orchestrator",
        "port":   8001,
        "health": "/health",
        "delay":  5,
    },
    {
        "name":   "Watchtower Supervisor",
        "key":    "supervisor",
        "script": "orchestrator/watchtower.py",
        "cwd":    None,
        "port":   8010,
        "health": "/health",
        "delay":  5,
    },
    {
        "name":   "Watchtower Cron",
        "key":    "watchtower",
        "script": "services/watchtower/api.py",
        "cwd":    None,
        "port":   8012,
        "health": "/health",
        "delay":  3,
    },
    {
        "name":     "Dashboard",
        "key":      "dashboard",
        "script":   None,
        "cwd":      None,
        "port":     8501,
        "health":   None,
        "delay":    5,
        "streamlit": True,
    },
    {
        "name":     "Roux Voice",
        "key":      "roux",
        "script":   "services/roux/roux_service.py",
        "cwd":      None,
        "port":     8014,
        "health":   "/health",
        "delay":    5,
        "optional": True,
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):    print(f"  {GREEN}[OK]{RESET} {msg}")
def err(msg):   print(f"  {RED}[FAIL]{RESET} {msg}")
def warn(msg):  print(f"  {YELLOW}[WARN]{RESET} {msg}")
def info(msg):  print(f"  {msg}")


def port_open(port: int, host: str = "localhost", timeout: float = 1.0) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        result = s.connect_ex((host, port))
        s.close()
        return result == 0
    except Exception:
        return False


def wait_for_port(port: int, name: str, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(1)
    return False


def http_ok(port: int, path: str = "/health", timeout: float = 3.0) -> bool:
    """Try an HTTP health check. Returns True if 200."""
    try:
        import urllib.request
        url = f"http://localhost:{port}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def launch_service(svc: dict) -> subprocess.Popen:
    """Start a service. Windows: new cmd window. Unix: background process."""
    cwd = str(PROJECT_ROOT / svc["cwd"]) if svc.get("cwd") else str(PROJECT_ROOT)
    script = str(PROJECT_ROOT / svc["script"]) if svc.get("script") else None

    if svc.get("streamlit"):
        cmd = [str(PYTHON), "-m", "streamlit", "run",
               str(PROJECT_ROOT / "dashboard.py"),
               "--server.port", str(svc["port"]),
               "--server.headless", "true",
               "--browser.gatherUsageStats", "false"]
    else:
        cmd = [str(PYTHON), script]

    if IS_WINDOWS:
        title     = svc["name"]
        cmd_str   = " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd)
        full_cmd  = f'start "{title}" cmd /c "cd /d {cwd} && {cmd_str}"'
        proc = subprocess.Popen(full_cmd, shell=True, cwd=cwd)
    else:
        log_dir  = PROJECT_ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"{svc['key']}.log"
        with open(log_file, "a") as log:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=log, stderr=log,
                                    start_new_session=True)
    return proc


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def check_preflight() -> bool:
    passed = True

    # config.yaml
    if not (PROJECT_ROOT / "config.yaml").exists():
        err("config.yaml not found.")
        info("  Fix: cp config.example.yaml config.yaml  (then edit models section)")
        passed = False
    else:
        ok("config.yaml found")

    # venv
    if not Path(PYTHON).exists():
        warn(f"Virtual env not found at expected path. Using system Python: {sys.executable}")
    else:
        ok(f"Python: {PYTHON}")

    # Ollama
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as r:
            if r.status == 200:
                ok("Ollama reachable")
            else:
                warn(f"Ollama returned HTTP {r.status}")
    except Exception:
        err("Ollama not reachable at localhost:11434")
        info("  Fix: run 'ollama serve' in another terminal")
        passed = False

    # RAG index
    index_file = PROJECT_ROOT / "memory" / "memories" / "index.faiss"
    if not index_file.exists():
        warn("RAG index not found — agents will start without self-knowledge")
        info("  Fix: python ingest_self.py  (takes ~2-5 min)")
    else:
        ok("RAG index found")

    return passed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Process tracking & cleanup
# ---------------------------------------------------------------------------
_spawned_procs: list[subprocess.Popen] = []


def shutdown_all():
    """Kill all spawned service processes (and their process trees on Windows)."""
    if not _spawned_procs:
        return
    print(f"\n{YELLOW}Shutting down services...{RESET}")
    for proc in _spawned_procs:
        try:
            if IS_WINDOWS:
                # /T = tree kill (kills cmd window + child python process)
                # /F = force
                subprocess.run(
                    f"taskkill /F /T /PID {proc.pid}",
                    shell=True, capture_output=True
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    _spawned_procs.clear()
    print(f"{GREEN}All services stopped.{RESET}\n")


def _signal_handler(sig, frame):
    shutdown_all()
    sys.exit(0)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def main():
    parser = argparse.ArgumentParser(description="Launch RouxYou services")
    parser.add_argument("--no-roux",   action="store_true", help="Skip Roux voice service")
    parser.add_argument("--no-dash",   action="store_true", help="Skip dashboard")
    parser.add_argument("--only",      type=str,            help="Start only this service key")
    parser.add_argument("--no-check",  action="store_true", help="Skip pre-flight checks")
    args = parser.parse_args()

    print(f"\n{BOLD}{'='*52}{RESET}")
    print(f"{BOLD}  RouxYou — Starting services{RESET}")
    print(f"{BOLD}{'='*52}{RESET}\n")

    if not args.no_check:
        print("Pre-flight checks...")
        if not check_preflight():
            print(f"\n{RED}Pre-flight failed. Fix the issues above and retry.{RESET}")
            print("(Skip checks with --no-check if you know what you're doing)\n")
            sys.exit(1)
        print()

    # Filter services
    services_to_run = SERVICES
    if args.only:
        services_to_run = [s for s in SERVICES if s["key"] == args.only]
        if not services_to_run:
            print(f"Unknown service key '{args.only}'. "
                  f"Valid keys: {', '.join(s['key'] for s in SERVICES)}")
            sys.exit(1)
    else:
        if args.no_roux:
            services_to_run = [s for s in services_to_run if s["key"] != "roux"]
        if args.no_dash:
            services_to_run = [s for s in services_to_run if s["key"] != "dashboard"]

    # Check for already-running services
    already_running = []
    for svc in services_to_run:
        if port_open(svc["port"]):
            already_running.append(svc)

    if already_running:
        print("Already running:")
        for svc in already_running:
            warn(f"{svc['name']} already on :{svc['port']} — skipping")
        services_to_run = [s for s in services_to_run if s not in already_running]
        print()

    if not services_to_run:
        print("All services already running.")
        print(f"\nDashboard: {GREEN}http://localhost:8501{RESET}\n")
        return

    # Launch
    print(f"Starting {len(services_to_run)} service(s)...\n")
    failed = []

    for svc in services_to_run:
        optional = svc.get("optional", False)
        print(f"  [{svc['key']:12s}]  {svc['name']:<20s}", end="", flush=True)

        try:
            proc = launch_service(svc)
            _spawned_procs.append(proc)
        except Exception as e:
            print(f"  {RED}LAUNCH ERROR: {e}{RESET}")
            if not optional:
                failed.append(svc["name"])
            continue

        # Wait for port
        time.sleep(0.5)
        deadline = time.time() + svc.get("delay", 5) + 20

        while time.time() < deadline:
            if port_open(svc["port"]):
                # Optional HTTP health check
                health_path = svc.get("health")
                if health_path and not svc.get("streamlit"):
                    time.sleep(0.5)
                    if http_ok(svc["port"], health_path):
                        print(f"  {GREEN}[OK] :{svc['port']}{RESET}")
                    else:
                        print(f"  {YELLOW}port open, health pending :{svc['port']}{RESET}")
                else:
                    print(f"  {GREEN}[OK] :{svc['port']}{RESET}")
                break
            time.sleep(1)
        else:
            msg = f"TIMEOUT — did not open :{svc['port']} within {svc.get('delay',5)+20}s"
            if optional:
                print(f"  {YELLOW}{msg} (optional){RESET}")
            else:
                print(f"  {RED}{msg}{RESET}")
                failed.append(svc["name"])

    # Summary
    print()
    if failed:
        print(f"{RED}The following services failed to start:{RESET}")
        for name in failed:
            print(f"  - {name}")
        print("\nCheck the terminal windows (Windows) or logs/ directory (Unix) for errors.\n")
    else:
        print(f"{GREEN}{BOLD}All services started successfully.{RESET}")
        if not args.no_dash:
            print(f"\nDashboard: {GREEN}http://localhost:8501{RESET}")
        print(f"\n{BOLD}Press Ctrl+C to stop all services.{RESET}\n")

        # Keep launcher alive so Ctrl+C triggers clean shutdown
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            shutdown_all()


if __name__ == "__main__":
    main()
