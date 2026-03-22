"""
RouxYou — Central Configuration Loader
Reads config.yaml and .env, exposes a single CONFIG object.
Import with: from config import CONFIG
"""

import os
from pathlib import Path
from dotenv import load_dotenv

try:
    import yaml
except ImportError:
    raise ImportError("PyYAML required: pip install pyyaml")

# --- Locate root and load secrets ---
ROOT = Path(__file__).parent.resolve()
load_dotenv(ROOT / ".env")

# --- Load config.yaml ---
config_path = ROOT / "config.yaml"
if not config_path.exists():
    raise FileNotFoundError(
        f"config.yaml not found at {config_path}\n"
        "Copy config.example.yaml to config.yaml and fill in your values."
    )

with open(config_path, "r") as f:
    _raw = yaml.safe_load(f)

# --- Build CONFIG object ---
class _Config:
    # Paths
    base_dir = Path(_raw.get("base_dir", ROOT))

    # Ports
    ports = _raw.get("ports", {})
    PORT_GATEWAY          = ports.get("gateway", 8000)
    PORT_ORCHESTRATOR     = ports.get("orchestrator", 8001)
    PORT_CODER            = ports.get("coder", 8002)
    PORT_WORKER           = ports.get("worker", 8003)
    PORT_MEMORY           = ports.get("memory", 8004)
    PORT_WATCHTOWER       = ports.get("watchtower_supervisor", 8010)
    PORT_RAG              = ports.get("rag_api", 8011)
    PORT_WATCHTOWER_CRON  = ports.get("watchtower_cron", 8012)
    PORT_ROUX             = ports.get("roux_voice", 8014)
    PORT_DASHBOARD        = ports.get("dashboard", 8501)
    PORT_STAGING_ORCH     = ports.get("staging_orchestrator", 9001)
    PORT_STAGING_CODER    = ports.get("staging_coder", 9002)
    PORT_STAGING_WORKER   = ports.get("staging_worker", 9003)

    # Ollama / LLM
    OLLAMA_HOST    = _raw.get("ollama_host", "http://localhost:11434")
    MODEL_ROUTER   = _raw.get("models", {}).get("router", "ministral:3b")
    MODEL_REASON   = _raw.get("models", {}).get("reasoning", "qwen3:14b-q4_K_M")
    MODEL_EMBED    = _raw.get("models", {}).get("embeddings", "nomic-embed-text")

    # Home Assistant (optional)
    HA_URL   = _raw.get("home_assistant", {}).get("url", "")
    HA_TOKEN = os.getenv("HA_TOKEN", "")

    # Remote Server (optional — e.g. Proxmox, TrueNAS, bare-metal node)
    REMOTE_SERVER_HOST     = _raw.get("remote_server", {}).get("host", "")
    REMOTE_SERVER_USER     = _raw.get("remote_server", {}).get("user", "root@pam")
    REMOTE_SERVER_PASSWORD = os.getenv("REMOTE_SERVER_PASSWORD", "")

    # TTS
    _tts = _raw.get("tts", {})
    TTS_PROVIDER  = _tts.get("provider", "none")           # "kitten" | "none"
    KITTEN_TTS_URL = _tts.get("kitten_url", "")            # http://host:5100
    TTS_VOICE     = _tts.get("voice", "en_US-amy-medium")
    TTS_SPEED     = float(_tts.get("speed", 1.0))

    # Search
    _search = _raw.get("search", {})
    SEARCH_PROVIDER = _search.get("provider", "duckduckgo")  # "searxng" | "duckduckgo" | "none"
    SEARXNG_URL = _search.get("searxng_url", "")

    # Anthropic (optional — Phase 31 Big Brain)
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

    # Voice
    _voice = _raw.get("voice", {})
    VOICE_ENABLED    = _voice.get("enabled", False)
    PIPER_EXECUTABLE = _voice.get("piper_executable", "piper")
    VOICE_MODEL      = _voice.get("voice_model", "")
    WHISPER_MODEL    = _voice.get("whisper_model", "large-v3-turbo")
    WAKE_WORD        = _voice.get("wake_word", "")

    # Memory decay
    _mem = _raw.get("memory", {})
    MEMORY_HALF_LIFE = _mem.get("age_half_life_days", 14)
    MEMORY_MIN_UTIL  = _mem.get("min_utility", 0.15)
    MEMORY_MAX_AGE   = _mem.get("max_age_days", 60)

    # Auto-approve
    _aa = _raw.get("auto_approve", {})
    AUTO_APPROVE_ENABLED    = _aa.get("enabled", False)
    AUTO_APPROVE_DAILY_LIMIT = _aa.get("daily_limit", 10)
    AUTO_APPROVE_MIN_CONF   = _aa.get("min_confidence", 0.8)


CONFIG = _Config()
