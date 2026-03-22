# RouxYou — Quick Start Guide

Get from zero to a running local AI agent system in about 30 minutes.

---

## What you're setting up

RouxYou is a multi-service system. By the end of this guide you'll have:

- **7 services** running locally (Gateway, Orchestrator, Coder, Worker, Memory, RAG, Watchtower)
- **A dashboard** you can chat with and give tasks
- **Roux** (optional voice layer) — if you have a microphone and configure TTS
- **Web search** — either via DuckDuckGo (zero setup) or self-hosted SearXNG

Everything runs on your hardware. Nothing phones home.

---

## Prerequisites

### Required

| Requirement | Notes |
|---|---|
| Python 3.11+ | 3.12 works. 3.10 may have typing issues. |
| [Ollama](https://ollama.com) | Must be running at `localhost:11434` |
| 16GB RAM | 32GB recommended if running large models |
| ~20GB disk | For models + FAISS index |

### Recommended (but not required to start)

| Optional | What it unlocks |
|---|---|
| NVIDIA GPU (8GB+ VRAM) | Faster LLM inference; required for Whisper STT |
| Microphone | Voice input for Roux |
| [Kitten TTS server](https://github.com/thwiki/koe) | Voice output for Roux |
| [SearXNG](https://docs.searxng.org) via Docker | Self-hosted private web search |

---

## Step 1 — Pull Ollama models

RouxYou needs three types of models. The exact models are configurable; these are tested defaults:

```bash
# Reasoning model — used by Coder for task planning (heavy, needs GPU or patience)
ollama pull qwen2.5-coder:14b

# Router model — used for chat, intent classification, Roux voice (fast)
ollama pull qwen2.5:7b

# Embedding model — used by the RAG memory system (small, CPU fine)
ollama pull nomic-embed-text
```

**Using different models?** Any model on Ollama works. You'll set the names in `config.yaml`
in Step 3. The reasoning model should be the best you can run — it writes all execution plans.
The router model just needs to be fast and instruction-following.

> **GPU check:** Run `ollama ps` while a model is loaded to confirm it's on GPU.
> If you see `100% GPU` you're good. CPU inference works but planning will be slow (~2-5 min/task).

---

## Step 2 — Install Python dependencies

```bash
# Clone or download RouxYou
cd RouxYou

# Create a virtual environment
python -m venv venv

# Activate it
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# Install core dependencies
pip install -r requirements.txt
```

**Optional dependency groups** — install only what you need:

```bash
# Web search via DuckDuckGo (recommended for most users — no server required)
pip install duckduckgo-search

# Voice (Roux STT) — requires NVIDIA GPU with CUDA
pip install faster-whisper

# Voice (VAD — always-listening wake word detection)
pip install webrtcvad scipy sounddevice

# Vision (screen capture analysis)
pip install pyautogui pillow
```

---

## Step 3 — Configure

```bash
# Copy the example configs
cp config.example.yaml config.yaml
cp .env.example .env
```

### Edit `config.yaml`

Open `config.yaml` and update at minimum:

```yaml
# --- Models ---
# Set these to match what you actually pulled in Step 1
models:
  router: "qwen2.5:7b"          # fast model for chat + intent
  reason: "qwen2.5-coder:14b"   # heavy model for planning
  embed:  "nomic-embed-text"     # embedding model (don't change unless you know why)

# --- Ollama ---
ollama:
  host: "http://localhost:11434"  # default — change if Ollama is on another machine

# --- Search (optional) ---
search:
  provider: "duckduckgo"   # "duckduckgo" | "searxng" | "none"
  searxng_url: ""           # only needed if provider is "searxng"

# --- TTS (optional) ---
tts:
  provider: "none"          # "kitten" | "none"
  kitten_url: ""            # e.g. http://192.168.1.x:5100 if you have Kitten running
```

Everything else in `config.yaml` can stay at defaults for now.

### Edit `.env`

`.env` is for secrets only. For a basic setup it can be left empty.
Add values if you're using Home Assistant or remote server monitoring:

```bash
# Optional — only if you want HA control
HA_TOKEN=your_home_assistant_long_lived_token

# Optional — only if you're monitoring a remote server (Proxmox, TrueNAS, etc.)
REMOTE_SERVER_PASSWORD=your_server_password
```

---

## Step 4 — Build the RAG index

RouxYou's memory system uses a FAISS vector index. On first run you need to build it
from the codebase so the agents have self-knowledge.

```bash
# From the RouxYou root directory (venv activated)
python ingest_self.py
```

This scans all `.py` files in the project, chunks them, embeds them using `nomic-embed-text`,
and writes `memory/memories/index.faiss` + `memory/memories/metadata.json`.

Takes about 2-5 minutes depending on your hardware. You'll see progress output.

> **Troubleshooting:** If this fails, make sure Ollama is running and `nomic-embed-text` is pulled.
> Run `ollama list` to confirm.

---

## Step 5 — Start the system

### Option A: Launch script (recommended)

```bash
# Windows
python launch.py

# macOS/Linux
python launch.py
```

`launch.py` starts all services in dependency order and waits for each to pass its
health check before starting the next. If any service fails to start it tells you why.

### Option B: Manual (one terminal per service)

If you want to see each service's logs separately:

```bash
# Terminal 1 — Memory + RAG (start first, others depend on it)
cd memory && python memory_agent.py

# Terminal 2 — RAG HTTP API
cd memory && python http_api.py

# Terminal 3 — Gateway
python gateway/gateway.py

# Terminal 4 — Orchestrator
cd orchestrator && python orchestrator.py

# Terminal 5 — Coder
cd coder && python coder.py

# Terminal 6 — Worker
cd worker && python worker.py

# Terminal 7 — Watchtower (cron + proposals)
cd services/watchtower && python api.py

# Terminal 8 — Dashboard
streamlit run dashboard.py

# Optional Terminal 9 — Roux voice
cd services/roux && python roux_service.py
```

---

## Step 6 — Verify everything is running

Open the dashboard: **http://localhost:8501**

You should see the status bar at the top with green pills for all 5 core services:

```
🟢 Gateway  🟢 Orchestrator  🟢 Coder  🟢 Worker  🟢 Watchtower  🟢/⚪ DDG Search
```

If any pill is red, check that service's terminal window for errors.

### Quick sanity check

Type this in the Chat tab and press Send:

```
list the files in the current directory
```

Roux should acknowledge, queue the task, and within ~60 seconds return a file listing.
If it works — you're up.

---

## Step 7 — What to try first

```
# Informational — answered instantly via Roux's local LLM
what services are running right now?
how does the task queue work?

# File operations — routed through Coder → Worker pipeline
create a file called hello.txt with the contents "RouxYou is running"
read hello.txt
list all .py files in the shared/ directory

# Web search (if configured)
search the web for the latest Ollama release notes

# Self-improvement (creates a proposal requiring your approval)
analyze the codebase and suggest improvements to the memory system
```

---

## Troubleshooting

### A service won't start

1. Check the terminal window for that service — the error is almost always there
2. Make sure Ollama is running: `ollama ps`
3. Make sure the venv is activated: `which python` should show `.../venv/...`
4. Check port conflicts: `netstat -an | grep 800` — all ports 8000-8015 should be free

### Tasks time out or hang

- Check Ollama is actually running the model on GPU (`ollama ps`)
- CPU inference for a 14B model takes 2-5 minutes per task — this is normal
- Consider using a smaller reasoning model (e.g. `qwen2.5-coder:7b`) for faster planning

### RAG returns no results

- Re-run `python ingest_self.py` — the index may not have built successfully
- Check `memory/memories/` exists and contains `index.faiss` and `metadata.json`
- Make sure `nomic-embed-text` is pulled: `ollama list | grep nomic`

### Dashboard shows all red

- Nothing is started yet — run `python launch.py` first
- Or the ports are in use — restart your terminal and try again

### "Module not found" errors

- Make sure your venv is activated
- Run `pip install -r requirements.txt` again
- If it's an optional module (webrtcvad, faster-whisper, etc.) you can ignore the error
  unless you specifically need that feature

---

## Optional setup

### Web search via SearXNG (self-hosted, privacy-first)

If you want search that doesn't touch DuckDuckGo's servers:

```bash
# Pull and run SearXNG via Docker
docker run -d -p 8888:8080 \
  -e BASE_URL=http://localhost:8888 \
  searxng/searxng:latest
```

Then in `config.yaml`:
```yaml
search:
  provider: "searxng"
  searxng_url: "http://localhost:8888"
```

### Kitten TTS (voice output for Roux)

Kitten TTS is a self-hosted text-to-speech server.
See [setup instructions](https://github.com/thwiki/koe) — it runs as a separate service,
typically on another machine or in Docker.

Once running, update `config.yaml`:
```yaml
tts:
  provider: "kitten"
  kitten_url: "http://YOUR_SERVER_IP:5100"
  voice: "en_US-amy-medium"   # or any voice your Kitten server has loaded
  speed: 1.0
```

### Home Assistant integration

Add your HA URL and token to `config.yaml` and `.env`:

```yaml
# config.yaml
home_assistant:
  url: "http://YOUR_HA_IP:8123"
```

```bash
# .env
HA_TOKEN=your_long_lived_access_token
```

Workers can then control devices via the `ha_control` action:
```
turn on the living room lights
```

---

## What's running where

| Service | Port | Role |
|---|---|---|
| Gateway | 8000 | Reverse proxy — all traffic flows through here |
| Orchestrator | 8001 | Brain — routes intents, manages task queue |
| Coder | 8002 | Planner — turns tasks into execution steps |
| Worker | 8003 | Hands — executes file ops, commands, searches |
| Memory Agent | 8004 | Episodic memory store |
| Watchtower | 8010 | Blue-green deploy + service restarts |
| Watchtower Cron | 8012 | Scheduled jobs + proposal bus |
| Roux Voice | 8014 | Voice service (optional) |
| RAG API | 8011 | Vector search over codebase knowledge |
| Dashboard | 8501 | Streamlit UI |

---

## Next steps

- Read `README.md` for architecture deep-dive
- Check the Proposals tab in the dashboard — the system will start generating
  self-improvement proposals within 30 minutes of running
- Try giving it a complex multi-step task
- Add your own capabilities in `worker/capabilities/`
