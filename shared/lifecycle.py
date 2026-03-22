"""
Lifecycle Manager — PID registration, cleanup, and graceful shutdown.
"""
import os
import sys
import json
import psutil
import atexit
import time
from pathlib import Path
from typing import Dict, Optional

BASE_DIR = Path(__file__).parent.parent
PID_FILE = BASE_DIR / "active_pids.json"
LOCK_FILE = BASE_DIR / ".pid_lock"


class PIDRegistry:

    def __init__(self):
        self.pid_file = PID_FILE
        self.lock_file = LOCK_FILE

    def _acquire_lock(self, timeout: float = 5.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            try:
                self.lock_file.touch(exist_ok=False)
                return True
            except FileExistsError:
                time.sleep(0.1)
        return False

    def _release_lock(self):
        try:
            self.lock_file.unlink()
        except FileNotFoundError:
            pass

    def _read_registry(self) -> Dict[str, int]:
        if not self.pid_file.exists():
            return {}
        try:
            with open(self.pid_file, 'r') as f:
                data = json.load(f)
            return {k: int(v) for k, v in data.items()}
        except (json.JSONDecodeError, ValueError):
            return {}

    def _write_registry(self, data: Dict[str, int]):
        temp_file = self.pid_file.with_suffix('.tmp')
        with open(temp_file, 'w') as f:
            json.dump(data, f, indent=2)
        temp_file.replace(self.pid_file)

    def cleanup_stale_pids(self):
        if not self._acquire_lock():
            return
        try:
            data = self._read_registry()
            cleaned = {}
            for name, pid in data.items():
                if psutil.pid_exists(pid):
                    try:
                        proc = psutil.Process(pid)
                        if 'python' in proc.name().lower():
                            cleaned[name] = pid
                    except psutil.NoSuchProcess:
                        pass
            if len(cleaned) < len(data):
                self._write_registry(cleaned)
        finally:
            self._release_lock()

    def register_process(self, agent_name: str):
        pid = os.getpid()
        self.cleanup_stale_pids()
        if not self._acquire_lock():
            return
        try:
            data = self._read_registry()
            if agent_name in data:
                old_pid = data[agent_name]
                if psutil.pid_exists(old_pid):
                    return
            data[agent_name] = pid
            self._write_registry(data)
            atexit.register(self._cleanup_on_exit, agent_name)
        finally:
            self._release_lock()

    def _cleanup_on_exit(self, agent_name: str):
        if not self._acquire_lock(timeout=2.0):
            return
        try:
            data = self._read_registry()
            if agent_name in data:
                del data[agent_name]
                self._write_registry(data)
        finally:
            self._release_lock()

    def kill_all_processes(self):
        if not self.pid_file.exists():
            return
        data = self._read_registry()
        for name, pid in data.items():
            self._kill_process(name, pid)
        try:
            self.pid_file.unlink()
        except FileNotFoundError:
            pass

    def _kill_process(self, name: str, pid: int) -> bool:
        try:
            if not psutil.pid_exists(pid):
                return False
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            parent.kill()
            parent.wait(timeout=3)
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired, Exception):
            return False


_registry = PIDRegistry()


def register_process(agent_name: str):
    _registry.register_process(agent_name)


def kill_all_processes():
    _registry.kill_all_processes()


def cleanup_stale_pids():
    _registry.cleanup_stale_pids()
