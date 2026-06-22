"""GPU arbiter (lever #2, 2026-06-05) — the 16GB card holds ONE big model at a time.

The companion (v5.3, llama-server :8090) is the always-on default. A coder /plan needs the
card, and the coder (qwen3-coder on llama-server :8091, or qwen2.5 on ollama) CANNOT co-reside
with v5.3. This automates the manual `bin/llama_v53.sh` dance: evict v5.3 → bring up the coder
backend → run the plan → restore v5.3.

History: in the ollama era ollama's MAX_LOADED_MODELS auto-evicted between roux-v53 and the
coder. v5.3 moving to llama-server (2026-05-30) left ollama's management → swap went manual.
This arbiter restores the automation for the llama-server world.

Flag-gated by ROUX_GPU_ARBITER (default OFF → coder_gpu_session is a no-op, manual flow intact).
"""
import asyncio
import os
import re
import subprocess
import time
import urllib.request
from contextlib import asynccontextmanager

from shared.logger import get_logger

logger = get_logger("gpu_arbiter")

_PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LLAMA_V53_SH = os.path.join(_PROJECT, "bin", "llama_v53.sh")
SERVE_CODER_SH = os.environ.get("ROUX_CODER_SERVE_SH", os.path.join(_PROJECT, "bin", "serve_qwen3coder.sh"))
CODER_SERVE_LOG = os.environ.get("ROUX_CODER_SERVE_LOG", "/tmp/llama-qwen3coder.log")
V53_PORT = 8090
CODER_LLAMA_PORT = 8091

_lock = asyncio.Lock()


def _health(port: int, timeout: int = 3) -> bool:
    """True iff the llama-server on `port` answers /health 200 (False during load/503/down)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _port_pid(port: int):
    try:
        out = subprocess.run(["ss", "-lptnH", f"sport = :{port}"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r'pid=(\d+)', out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


async def _wait_health(port: int, up: bool, timeout: int) -> bool:
    """Poll until /health matches `up` (True=ready, False=down) or timeout. Returns success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health(port) == up:
            return True
        await asyncio.sleep(2)
    return _health(port) == up


def _sh(*args) -> None:
    try:
        subprocess.run(list(args), capture_output=True, text=True, timeout=30)
    except Exception as e:
        logger.warning(f"🔀 arbiter shell {' '.join(args)} failed: {e}")


def _coder_alias() -> str:
    """Resolve the configured coder alias (e.g. 'llamacpp_coder/qwen3-coder') from config.yaml."""
    try:
        import yaml
        with open(os.path.join(_PROJECT, "config.yaml")) as f:
            c = yaml.safe_load(f)
        return (c.get("llm", {}).get("aliases", {}) or {}).get("coder", "") or ""
    except Exception:
        return ""


async def _evict_v53() -> None:
    if _port_pid(V53_PORT):
        logger.info("🔀 arbiter: evicting v5.3 (:8090) to free the card")
        _sh("bash", LLAMA_V53_SH, "stop")
        await _wait_health(V53_PORT, up=False, timeout=30)


async def _restore_v53() -> None:
    if not _port_pid(V53_PORT):
        logger.info("🔀 arbiter: restoring v5.3 (:8090)")
        _sh("bash", LLAMA_V53_SH, "start")
    if await _wait_health(V53_PORT, up=True, timeout=180):
        logger.info("🔀 arbiter: v5.3 back up")
    else:
        logger.error("🔀 arbiter: v5.3 did NOT come back up within 180s — companion degraded!")


async def _stop_coder_llama() -> None:
    pid = _port_pid(CODER_LLAMA_PORT)
    if pid:
        logger.info(f"🔀 arbiter: stopping coder llama-server (:8091 pid {pid})")
        try:
            os.kill(pid, 15)
        except Exception:
            pass
        await _wait_health(CODER_LLAMA_PORT, up=False, timeout=30)


async def _start_coder_llama() -> bool:
    if _health(CODER_LLAMA_PORT):
        return True
    if not os.path.exists(SERVE_CODER_SH):
        logger.error(f"🔀 arbiter: coder serve script missing: {SERVE_CODER_SH}")
        return False
    logger.info(f"🔀 arbiter: starting coder llama-server via {SERVE_CODER_SH}")
    subprocess.Popen(["bash", "-c", f"nohup {SERVE_CODER_SH} > {CODER_SERVE_LOG} 2>&1 &"])
    ok = await _wait_health(CODER_LLAMA_PORT, up=True, timeout=180)
    logger.info(f"🔀 arbiter: coder backend {'ready' if ok else 'FAILED to start'}")
    return ok


# ── Coder-holds-GPU signal (2026-06-05) ───────────────────────────────────────────────
# While the arbiter holds the card for the coder, v5.3 (:8090) is evicted — so any cron that
# uses v5.3/a GPU model (researcher, reflection, self-propose, drafter, …) would fail or thrash.
# The arbiter drops a flag for the whole hold window (eviction → restore); GPU-dependent crons
# check coder_holds_gpu() and skip their cycle. Stale flag (a crashed run) ages out, so a crash
# can't wedge the crons off forever.
_CODER_GPU_FLAG = os.path.join(_PROJECT, "state", "coder_gpu_active")
_CODER_FLAG_MAX_AGE_S = 900  # 15 min — a coder run + swap is ~5 min; older = stale/crashed → ignore


def _set_coder_flag() -> None:
    try:
        os.makedirs(os.path.dirname(_CODER_GPU_FLAG), exist_ok=True)
        with open(_CODER_GPU_FLAG, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _clear_coder_flag() -> None:
    try:
        os.remove(_CODER_GPU_FLAG)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def coder_holds_gpu(max_age_s: float = _CODER_FLAG_MAX_AGE_S) -> bool:
    """True iff the arbiter currently holds the card for the coder (fresh flag present).
    Cheap (one stat); safe for any process to call. A stale flag (older than max_age_s, e.g. a
    crashed coder run) is treated as 'not held' so crons recover automatically."""
    try:
        return (time.time() - os.stat(_CODER_GPU_FLAG).st_mtime) < max_age_s
    except FileNotFoundError:
        return False
    except Exception:
        return False


@asynccontextmanager
async def coder_gpu_session(coder_alias: str = ""):
    """Acquire the card for the coder backend, yield, then restore v5.3. No-op unless
    ROUX_GPU_ARBITER=1 (so the existing manual flow is untouched by default). Serialized by a
    lock so concurrent /plan calls share one swap. Fail-soft: v5.3 is restored in `finally`."""
    if os.environ.get("ROUX_GPU_ARBITER", "0") != "1":
        yield
        return
    alias = coder_alias or _coder_alias()
    provider = alias.split("/", 1)[0] if "/" in alias else ""
    model = alias.split("/", 1)[1] if "/" in alias else alias
    is_llama = provider == "llamacpp_coder"
    async with _lock:
        acquired = False
        try:
            logger.info(f"🔀 arbiter: acquiring coder GPU (alias={alias or '?'})")
            _set_coder_flag()  # signal GPU-dependent crons to skip for the whole hold window
            await _evict_v53()
            if is_llama:
                if not await _start_coder_llama():
                    raise RuntimeError("coder llama-server (:8091) failed to start")
            acquired = True
            yield
        finally:
            if acquired or is_llama:
                logger.info("🔀 arbiter: releasing — restoring v5.3")
                if is_llama:
                    await _stop_coder_llama()
                elif model:
                    _sh("ollama", "stop", model)   # free the ollama coder so v5.3 can load
                await _restore_v53()
            _clear_coder_flag()  # GPU is back to v5.3 — crons may resume
