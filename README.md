# RouxYou

**Sovereign AI on consumer hardware.**

A self-evolving, multi-agent AI system that runs entirely on your local network. No cloud. No API keys required. No data leaves your machine.

[rouxyou.com](https://rouxyou.com) | [Architecture](https://rouxyou.com/architecture/)

---

## What is this?

RouxYou is a modular AI agent framework built around local LLMs (via [Ollama](https://ollama.com)). It coordinates multiple specialized agents — a router, a reasoner, an executor, a supervisor — to handle tasks autonomously, modify its own code through a safe blue-green deploy pipeline, and learn from what it does.

It was built to prove that meaningful AI autonomy doesn't require a datacenter or a cloud subscription. An i5, a mid-range GPU, and 32GB of RAM is enough.

### Key capabilities

- **Multi-agent coordination** — Gateway routes traffic, Orchestrator classifies intent, Coder plans with a 14B-parameter model, Worker executes with 15+ built-in capabilities
- **Self-modification** — Coder can patch Worker/Orchestrator code through a blue-green deploy pipeline with automatic rollback on failure
- **Episodic memory** — FAISS-backed RAG system lets agents remember what they've done, learn from outcomes, and build skills over time
- **Proposal system** — Observers monitor health, resources, and codebase quality, then propose improvements. An auto-approve engine can act on safe proposals without human intervention
- **Voice interface** — Optional Roux voice layer: Whisper STT, local LLM reasoning, Piper/Kitten TTS. Talk to your system like a person.
- **Watchtower supervisor** — Immutable process that runs cron jobs, enforces deploy gates, and can restart failed services. The one thing the system can't modify.
- **Dashboard** — Streamlit UI for chat, task queue, proposals, deploy management, and system health at a glance

### What it's not

- Not a wrapper around ChatGPT/Claude/Gemini (though Claude can be added as an optional reasoning backend)
- Not a framework that requires cloud infrastructure to function
- Not a toy — this runs real tasks, manages real schedules, and modifies its own codebase

---

## Architecture

```
               +-------------+
               |   Gateway   |  :8000 — reverse proxy, route table
               +------+------+
                      |
         +------------+------------+
         |                         |
  +------+-------+         +------+------+
  | Orchestrator |         |  Watchtower |  :8010 — immutable supervisor
  |    :8001     |         +------+------+
  +------+-------+                |
         |                 +------+------+
    +----+----+            | Cron :8012  |  health, decay, proposals
    |         |            +-------------+
+---+---+ +---+---+
| Coder | |Worker |
| :8002 | | :8003 |
+-------+ +-------+
              |
        +-----+-----+
        | 15+ caps   |  filesystem, web search, commands,
        | no LLM     |  vision, verification, system stats
        +------------+

  +----------+   +----------+   +-----------+
  | Memory   |   | RAG API  |   |   Roux    |
  |  :8004   |   |  :8011   |   |  :8014    |
  +----------+   +----------+   +-----------+
     FAISS         bridge         voice I/O
```

**Models (defaults):**
| Role | Model | VRAM |
|---|---|---|
| Router | ministral:3b | ~3 GB |
| Reasoning / Code | qwen3:14b-q4_K_M | ~10 GB |
| Embeddings | nomic-embed-text | ~300 MB |
| STT (optional) | Whisper large-v3-turbo | GPU shared |

Fits comfortably on a single consumer GPU (16GB VRAM).

---

## Quick start

See **[QUICKSTART.md](QUICKSTART.md)** for the full setup guide. The short version:

```bash
# 1. Clone and configure
git clone https://github.com/RouxYouAI/rouxyou.git
cd rouxyou
cp config.example.yaml config.yaml   # edit with your paths/preferences
cp .env.example .env                 # add secrets (optional — HA, remote server)

# 2. Install dependencies
pip install -r requirements.txt

# 3. Pull Ollama models
ollama pull ministral:3b
ollama pull qwen3:14b-q4_K_M
ollama pull nomic-embed-text

# 4. Build the RAG index
python ingest_self.py

# 5. Launch
python launch.py
```

Dashboard opens at `http://localhost:8501`. Start chatting.

---

## Project structure

```
RouxYou_Public/
├── config.example.yaml    # Configuration template
├── config.py              # Central config loader
├── launch.py              # Cross-platform service launcher
├── ingest_self.py         # RAG bootstrap — indexes the codebase
├── dashboard.py           # Streamlit UI
├── gateway/               # Reverse proxy + route table
├── orchestrator/          # Intent classification + task routing
├── coder/                 # LLM-powered planning + code generation
├── worker/                # Execution engine (15+ capabilities)
│   └── capabilities/      # Pluggable capability modules
├── memory/                # FAISS vector store + episodic memory
│   ├── memory_agent.py    # Memory service (:8004)
│   ├── http_api.py        # RAG bridge API (:8011)
│   └── vectorstore.py     # FAISS index management
├── services/
│   ├── roux/              # Voice interface (STT + TTS + intent)
│   └── watchtower/        # Cron, health checks, deploy gate
├── shared/                # Modules shared across all agents
│   ├── companion.py       # Conversational AI layer
│   ├── deployer.py        # Blue-green deploy pipeline
│   ├── proposal_bus.py    # Proposal lifecycle management
│   ├── coach.py           # LLM enrichment for proposals
│   ├── search.py          # Web search abstraction
│   └── ...
├── state/                 # Runtime state (gitignored)
└── logs/                  # Service logs (gitignored)
```

---

## Configuration

All configuration lives in two files (neither is committed to git):

- **`config.yaml`** — ports, model names, service URLs, feature flags. Copy from `config.example.yaml`.
- **`.env`** — secrets only (API keys, passwords). Copy from `.env.example`.

Optional integrations (leave blank to disable):
- **Home Assistant** — control smart home devices via the `ha_control` Worker action
- **Remote server monitoring** — track a hypervisor, NAS, or bare-metal node
- **SearXNG** — self-hosted privacy-first web search (or use DuckDuckGo with zero setup)
- **Kitten TTS** — self-hosted text-to-speech for Roux voice output

See `config.example.yaml` for full documentation of every option.

---

## How it works

1. **You say something** (via dashboard chat or Roux voice)
2. **Orchestrator** classifies intent — is this a conversation, a task, or both?
3. **Conversations** get answered directly by the router LLM (fast, ~3B params)
4. **Tasks** get routed to **Coder** for planning (14B params), then to **Worker** for execution
5. **Worker** executes using built-in capabilities — no LLM overhead for file ops, commands, search
6. **Results** flow back through the Orchestrator to your chat/voice interface
7. **Memory** records what happened — episodic memory with utility scoring and decay
8. **Watchtower** watches everything — health checks, proposal dispatch, deploy approvals

The system can also propose its own improvements via observers that monitor health, resources, and code quality. Safe proposals (reversible, high-confidence) can be auto-approved. Code changes always go through the blue-green deploy pipeline with automatic rollback.

---

## Requirements

- **Python 3.11+**
- **Ollama** running locally
- **16 GB RAM** minimum (32 GB recommended)
- **GPU with 16 GB VRAM** recommended (CPU-only works but slower)
- **~20 GB disk** for models and FAISS index

---

## License

TBD

---

## Credits

Built by [Dr. Helix](https://github.com/DrH3lix) and Claude.

Site: [rouxyou.com](https://rouxyou.com)
