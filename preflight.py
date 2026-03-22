"""
preflight.py -- RouxYou Setup Validator & First-Run Helper

Run this before launching RouxYou for the first time, or whenever
something feels broken. It checks every dependency, config value,
and external service, then tells you exactly what to fix.

Usage:
    python preflight.py          # full check
    python preflight.py --fix    # attempt to auto-fix what it can
"""

import sys
import os
import shutil
import subprocess
import importlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
PASS = "[OK]"
FAIL = "[X]"
WARN = "[!]"
SKIP = "[--]"


def header(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def check(label: str, ok: bool, fix_hint: str = ""):
    status = PASS if ok else FAIL
    print(f"  {status:6s} {label}")
    if not ok and fix_hint:
        print(f"         -> {fix_hint}")
    return ok


# ─── Python version ────────────────────────────────────────────────────────

def check_python():
    header("Python")
    v = sys.version_info
    ok = v.major == 3 and v.minor >= 11
    check(f"Python {v.major}.{v.minor}.{v.micro}",
          ok, "Python 3.11+ required. Download from python.org")
    return ok


# ─── Pip packages ──────────────────────────────────────────────────────────

REQUIRED_PACKAGES = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn[standard]"),
    ("aiohttp", "aiohttp"),
    ("requests", "requests"),
    ("yaml", "pyyaml"),
    ("dotenv", "python-dotenv"),
    ("pydantic", "pydantic"),
    ("streamlit", "streamlit"),
    ("faiss", "faiss-cpu"),
    ("numpy", "numpy"),
    ("filelock", "filelock"),
    ("psutil", "psutil"),
]

OPTIONAL_PACKAGES = [
    ("duckduckgo_search", "duckduckgo-search", "Web search (DuckDuckGo)"),
    ("faster_whisper", "faster-whisper", "Voice STT (requires CUDA GPU)"),
    ("sounddevice", "sounddevice", "Microphone capture"),
    ("webrtcvad", "webrtcvad", "Voice activity detection"),
    ("scipy", "scipy", "Audio resampling for VAD"),
    ("pyautogui", "pyautogui", "Screen capture (vision)"),
    ("PIL", "Pillow", "Image handling (vision)"),
    ("ollama", "ollama", "Ollama Python client (vision)"),
    ("reportlab", "reportlab", "PDF generation (invoicing)"),
]


def check_packages(auto_fix: bool = False):
    header("Python Packages")
    all_ok = True

    for import_name, pip_name in REQUIRED_PACKAGES:
        try:
            importlib.import_module(import_name)
            check(pip_name, True)
        except ImportError:
            if auto_fix:
                print(f"  ...    Installing {pip_name}...")
                subprocess.run([sys.executable, "-m", "pip", "install", pip_name, "-q"],
                               capture_output=True)
                try:
                    importlib.import_module(import_name)
                    check(pip_name, True)
                    continue
                except ImportError:
                    pass
            check(pip_name, False, f"pip install {pip_name}")
            all_ok = False

    print()
    for import_name, pip_name, desc in OPTIONAL_PACKAGES:
        try:
            importlib.import_module(import_name)
            check(f"{pip_name} ({desc})", True)
        except ImportError:
            print(f"  {SKIP:6s} {pip_name} ({desc}) — not installed, optional")

    return all_ok


# ─── Config files ──────────────────────────────────────────────────────────

def check_config(auto_fix: bool = False):
    header("Configuration")
    all_ok = True

    config_yaml = PROJECT_ROOT / "config.yaml"
    config_example = PROJECT_ROOT / "config.example.yaml"
    env_file = PROJECT_ROOT / ".env"
    env_example = PROJECT_ROOT / ".env.example"

    if config_yaml.exists():
        check("config.yaml exists", True)
        # Validate it loads
        try:
            import yaml
            with open(config_yaml) as f:
                raw = yaml.safe_load(f)
            check("config.yaml is valid YAML", True)

            # Check critical fields
            base_dir = raw.get("base_dir", "")
            if "YOUR_USERNAME" in base_dir or not base_dir:
                check("config.yaml base_dir is set", False,
                      "Edit config.yaml and set base_dir to your RouxYou path")
                all_ok = False
            else:
                check(f"base_dir: {base_dir}", True)

        except Exception as e:
            check("config.yaml is valid YAML", False, str(e))
            all_ok = False
    else:
        if auto_fix and config_example.exists():
            shutil.copy(config_example, config_yaml)
            print(f"  ...    Copied config.example.yaml -> config.yaml")
            check("config.yaml created from template", True)
            print(f"         -> EDIT config.yaml and set base_dir to your path!")
            all_ok = False  # Still needs editing
        else:
            check("config.yaml exists", False,
                  "cp config.example.yaml config.yaml  (then edit it)")
            all_ok = False

    if env_file.exists():
        check(".env exists", True)
    else:
        if auto_fix and env_example.exists():
            shutil.copy(env_example, env_file)
            check(".env created from template", True)
        else:
            check(".env exists", False,
                  "cp .env.example .env  (add secrets if needed)")
            all_ok = False

    return all_ok


# ─── Ollama ────────────────────────────────────────────────────────────────

def check_ollama():
    header("Ollama")
    all_ok = True

    # Is Ollama reachable?
    try:
        from config import CONFIG
        host = CONFIG.OLLAMA_HOST
    except Exception:
        host = "http://localhost:11434"

    try:
        import requests
        r = requests.get(f"{host}/api/tags", timeout=5)
        check(f"Ollama reachable at {host}", r.status_code == 200)
        if r.status_code != 200:
            all_ok = False
            return all_ok

        models = [m["name"] for m in r.json().get("models", [])]

        # Check required models
        try:
            from config import CONFIG
            needed = {
                "Router": CONFIG.MODEL_ROUTER,
                "Reasoning": CONFIG.MODEL_REASON,
                "Embeddings": CONFIG.MODEL_EMBED,
            }
        except Exception:
            needed = {
                "Router": "ministral:3b",
                "Reasoning": "qwen3:14b-q4_K_M",
                "Embeddings": "nomic-embed-text",
            }

        for role, model_name in needed.items():
            base = model_name.split(":")[0]
            found = any(base in m for m in models)
            check(f"{role}: {model_name}", found,
                  f"ollama pull {model_name}")
            if not found:
                all_ok = False

    except Exception as e:
        check("Ollama reachable", False,
              "Is Ollama running? Start with: ollama serve")
        all_ok = False

    return all_ok


# ─── RAG Index ─────────────────────────────────────────────────────────────

def check_rag():
    header("RAG Memory Index")

    index_path = PROJECT_ROOT / "memory" / "memories" / "index.faiss"
    meta_path = PROJECT_ROOT / "memory" / "memories" / "metadata.json"

    if index_path.exists() and meta_path.exists():
        # Check if it has any chunks
        try:
            import json
            with open(meta_path) as f:
                meta = json.load(f)
            count = len(meta) if isinstance(meta, list) else 0
            if count > 0:
                check(f"FAISS index: {count} chunks", True)
                return True
            else:
                check("FAISS index exists but is empty", False,
                      "python ingest_self.py")
                return False
        except Exception:
            check("FAISS index exists", True)
            return True
    else:
        check("FAISS index not found", False,
              "python ingest_self.py")
        return False


# ─── Directories ───────────────────────────────────────────────────────────

def check_directories(auto_fix: bool = False):
    header("Directory Structure")
    all_ok = True

    needed = [
        PROJECT_ROOT / "state",
        PROJECT_ROOT / "state" / "conversations",
        PROJECT_ROOT / "logs",
        PROJECT_ROOT / "memory" / "memories",
        PROJECT_ROOT / "memory" / "memories" / "raw",
    ]

    for d in needed:
        if d.exists():
            check(str(d.relative_to(PROJECT_ROOT)), True)
        elif auto_fix:
            d.mkdir(parents=True, exist_ok=True)
            check(f"{d.relative_to(PROJECT_ROOT)} (created)", True)
        else:
            check(str(d.relative_to(PROJECT_ROOT)), False, f"mkdir -p {d}")
            all_ok = False

    return all_ok


# ─── Main ──────────────────────────────────────────────────────────────────

def main():
    auto_fix = "--fix" in sys.argv

    print()
    print("  RouxYou Preflight Check")
    if auto_fix:
        print("  Mode: AUTO-FIX (will attempt to resolve issues)")
    print()

    results = {}
    results["python"] = check_python()
    results["packages"] = check_packages(auto_fix)
    results["config"] = check_config(auto_fix)
    results["directories"] = check_directories(auto_fix)
    results["ollama"] = check_ollama()
    results["rag"] = check_rag()

    # Summary
    header("Summary")
    all_pass = True
    for name, passed in results.items():
        status = PASS if passed else FAIL
        print(f"  {status:6s} {name}")
        if not passed:
            all_pass = False

    print()
    if all_pass:
        print("  All checks passed. Launch with: python launch.py")
    else:
        print("  Some checks failed. Fix the issues above and re-run:")
        print("    python preflight.py")
        if not auto_fix:
            print()
            print("  Tip: run with --fix to auto-install packages and create config files:")
            print("    python preflight.py --fix")

    print()
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
