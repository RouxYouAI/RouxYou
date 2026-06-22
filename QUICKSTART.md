# Quick Start

## Prerequisites

```bash
# 1. Ollama running?
curl http://localhost:11434

# 2. Models pulled?
ollama list | grep ministral-3:8b
ollama list | grep qwen3:14b-q4_K_M
ollama list | grep nomic-embed-text

# If not:
ollama pull ministral-3:8b
ollama pull qwen3:14b-q4_K_M
ollama pull nomic-embed-text
```

## Install & Run

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. (Optional) Set up .env for Claude Big Brain
echo ANTHROPIC_API_KEY=sk-ant-your-key-here > .env

# 3. Run preflight check
python preflight.py --fix

# 4. Launch everything
python launch.py
```

## Services

Once running, Mission Control opens at **http://localhost:8501**

| Service | Port | Role |
|---------|------|------|
| Gateway | 8000 | Reverse proxy — all traffic enters here |
| Orchestrator | 8001 | Brain — intent routing, task queue, coordination |
| Coder | 8002 | Plans & writes code (Qwen3 14B) |
| Worker | 8003 | Executes — file ops, commands, web search, HA |
| Memory | 8004 | Episodic memory system |
| Watchtower | 8010 | Supervisor — health, deploy pipeline, immutable |
| RAG Bridge | 8011 | Claude memory → local LLM bridge |
| Watchtower Cron | 8012 | Proposal system, auto-approve, kill switch |
| Roux Voice | 8014 | Voice interface — STT/TTS/VAD |
| Dashboard | 8501 | Mission Control (Streamlit) |

## Configuration

All config lives in `config.yaml`:
- **LLM providers:** Ollama (default), vLLM, Claude — switch without code changes
- **Model aliases:** `router`, `coder`, `companion`, `coach`, `bigbrain`
- **Services:** Home Assistant, SearXNG, Proxmox
- **Voice:** Whisper model, Piper voice, wake word

Hot-reload LLM config without restart:
```bash
curl -X POST http://localhost:8001/llm/reload
```

## First Test

Open Mission Control and type in the chat:
```
What can you do?
```

The Orchestrator routes it, the Companion responds, and you'll see the full pipeline in the live logs.

## Dashboard Features

- **Brain Activity** — real-time agent state (thinking, executing, idle)
- **Event Feed** — live stream of typed events (thoughts, tool calls, steps, errors)
- **Cost Tracker** — session/daily token usage and cost breakdown
- **LLM Status** — provider health and alias routing
- **Task Queue** — priority queue with pause/resume/abort
- **Proposals** — system self-improvement suggestions with auto-approve
- **Roux Controls** — VAD wake/sleep, always-listening toggle

## Troubleshooting

- **Services won't start?** Run `python preflight.py --fix` to validate deps, config, Ollama, models, and RAG index
- **No response in chat?** Check Gateway (8000) and Orchestrator (8001) are running
- **LLM errors?** Check Ollama is running and models are pulled: `ollama list`
- **Voice not working?** Roux voice service is optional — system works without it
- **Weird errors?** Check `logs/` folder or Live Logs in the dashboard
