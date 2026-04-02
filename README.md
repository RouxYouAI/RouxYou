# Self-Modifying Agents

**A local-first AI system that understands, modifies, and deploys its own source code — with a human in the loop.**

Built by DJ in Wilmington, Delaware. Running entirely on a desktop PC. No cloud APIs. No corporate infrastructure. Just an Intel i5, an RTX 5060 Ti, and a 14-billion-parameter language model that rewrites the system it lives inside.

---

## What This Is

This is a multi-agent system where a locally-hosted LLM acts as both the brain and the hands. You talk to it in natural language through a chat interface. It plans code changes, writes patches, stages them in an isolated environment, runs health checks, and waits for you to approve before swapping the new code into production — all in about 36 seconds.

The catch: the code it's modifying is *itself*. The Coder agent writes patches to the Worker. The Worker routes those patches through a deployment pipeline. The Watchtower supervises the whole thing and won't let anything go live without a human clicking "Approve." When you do, traffic shifts to the new version with zero downtime. The old version gets archived for rollback. If the patch introduces a syntax error, it never reaches production.

This isn't a demo. It runs 24/7. It has episodic memory. It learns from what worked and what didn't. It manages its own task queue, monitors its own infrastructure, and proposes its own improvements.

---

## Architecture

```
                         ┌─────────────────────────┐
                         │     Mission Control      │
                         │   (Streamlit Dashboard)  │
                         │       :8501              │
                         └───────────┬─────────────┘
                                     │
                         ┌───────────▼─────────────┐
                         │        Gateway           │
          All traffic →  │    (Reverse Proxy)       │
                         │        :8000             │
                         └──┬──────┬──────┬────┬───┘
                            │      │      │    │
              ┌─────────────▼┐ ┌───▼────┐ │  ┌─▼──────────┐
              │ Orchestrator  │ │ Coder  │ │  │ Watchtower  │
              │    :8001      │ │ :8002  │ │  │   :8010     │
              │               │ │Qwen 14B│ │  │  Supervisor │
              │  Brain.       │ │        │ │  │  Immutable. │
              │  Routes tasks,│ │Plans & │ │  │  Can't be   │
              │  manages queue│ │writes  │ │  │  modified   │
              │  coordinates  │ │code.   │ │  │  by agents. │
              │  everything.  │ │        │ │  │             │
              └──────┬────────┘ └───┬────┘ │  └──────┬──────┘
                     │              │      │         │
                     │    ┌─────────▼──┐   │    ┌────▼──────┐
                     │    │   Worker    │◄──┘    │  Deployer │
                     │    │   :8003    │         │ (Library) │
                     │    │            │         │           │
                     │    │ Executes.  │         │ Stage,    │
                     │    │ File ops,  │         │ patch,    │
                     │    │ commands,  │         │ boot,     │
                     │    │ web search,│         │ test.     │
                     │    │ HA control.│         │           │
                     │    └────────────┘         └───────────┘
                     │
              ┌──────▼────────┐
              │    Memory     │
              │  (Episodic)   │
              │               │
              │ Learns from   │
              │ every task.   │
              │ Retrieves     │
              │ relevant past │
              │ experience.   │
              └───────────────┘
```

### The Services

**Gateway** — Async reverse proxy. All traffic flows through port 8000. During blue-green deployments, the Watchtower calls its swap endpoint to redirect traffic to the new instance. Clients never see a blip.

**Orchestrator** — The brain. Receives natural language from the chat interface, classifies intent, assesses risk, manages a priority task queue, coordinates the Coder and Worker, and synthesizes human-readable responses through the Companion layer. Non-blocking — queues tasks and returns immediately.

**Coder** — Powered by Qwen3 14B running locally via Ollama. Reads the codebase through an indexed map of every module, class, and function. Plans multi-step execution strategies. Writes surgical code patches. Has episodic memory — it remembers what worked on similar tasks before.

**Worker** — The hands. Executes file operations, shell commands, web searches (via self-hosted SearXNG), Home Assistant device control, and service restarts. Enforces security boundaries: credential redaction, path validation, write protection against accidental overwrites, and a deploy gate that blocks direct modification of system files.

**Watchtower** — The supervisor. Immutable — the agents cannot modify this file. Monitors all services, performs health checks, manages the task registry, handles infrastructure monitoring, and controls the blue-green deployment pipeline. The critical swap/kill steps live here behind a human-approval gate that nothing in the system can bypass.

**Deployer** — A library used by the Watchtower. Handles staging: copies service files to an isolated directory, applies patches with fuzzy anchor matching, validates syntax, boots the staging instance on a temporary port, and runs health checks. Does not have authority to swap traffic or kill processes — that power stays in the Watchtower.

**Memory** — Episodic memory system. Every completed task is stored with its query, plan, outcome, and utility score. The Coder retrieves relevant past experiences when planning new tasks, weighted by similarity and success rate. The system genuinely learns from its history.

**Mission Control** — Streamlit dashboard. Live chat interface, task queue visualization, real-time logs from all services, deploy approval cards, and system health monitoring. This is how you talk to the system and how it talks back.

---

## The Deploy Pipeline

This is the part that matters. When you say "add a /version endpoint to the Worker," here's what actually happens:

```
 You: "Add a /version endpoint that returns phase 20.3"
  │
  ▼
 Companion classifies intent → execute, risk: medium
  │
  ▼
 Task queued → Orchestrator sends to Coder
  │
  ▼
 Coder reads worker.py via codebase index
 Coder retrieves similar past tasks from episodic memory
 Coder (Qwen 14B) generates a plan:
   Step 1: read_file worker.py
   Step 2: deploy_patch service=worker
           content: app = FastAPI()
                    ===PATCH===
                    @app.get("/version")
                    async def version():
                        return {"phase": "20.3"}
  │
  ▼
 Normalizer catches edge cases:
   - Action aliases (insert_code → deploy_patch)
   - System file auto-routing (patch_file on worker.py → deploy_patch)
   - Service name inference from filename
   - Content format conversion (JSON → delimiter format)
  │
  ▼
 Worker receives plan, executes Step 1 (read file)
 Worker hits deploy gate on Step 2 — routes to Watchtower
  │
  ▼
 Deployer stages worker_v7:
   - Copies worker.py + capabilities/ to staging/worker_v7/
   - Fuzzy-matches anchor "app = FastAPI()" in the code
   - Disambiguates: 2 matches found, but only 1 is real code (line 747)
   - Line-targeted replacement at exact position
   - ast.parse() validates syntax — no errors
   - Rewrites uvicorn port to 9003 (staging port)
   - Boots staging instance
  │
  ▼
 Health check: GET http://127.0.0.1:9003/health → 200 OK (1517ms)
  │
  ▼
 ⏸️  AWAITING HUMAN APPROVAL — card appears in Mission Control
  │
  ▼
 You click "Approve"
  │
  ▼
 Watchtower executes the swap:
   - POST /gateway/swap → traffic now routes to port 9003
   - 3-second drain for in-flight requests
   - Kill old worker process (PID 19152)
   - Archive old worker.py for rollback
   - Promote staging copy to production
   - Restore production port in promoted copy
  │
  ▼
 🎉 DEPLOY COMPLETE — worker_v7 is live
 GET /version → {"phase": "20.3"}
```

36.2 seconds, end to end. Zero downtime. Full rollback capability. Human approval required.

---

## Safety Architecture

This system can rewrite its own source code. That's dangerous. Here's how it doesn't destroy itself:

**Immutable Supervisor** — `watchtower.py` cannot be modified by the agent system. The supervisor must never be supervised by the thing it supervises. This file is hardcoded into the Worker's `IMMUTABLE_FILES` set.

**Deploy Gate** — 14 system files are blocked from direct `write_file` or `patch_file` operations. All changes must route through `deploy_patch`, which goes through the full staging → health check → approval pipeline.

**Human-in-the-Loop** — The Watchtower stages deployments but will not swap traffic without explicit human approval through the Mission Control dashboard. No amount of prompt engineering from the LLM can bypass this gate — it's a FastAPI endpoint that requires a POST from the dashboard.

**Write Protection** — The Worker refuses to overwrite files if the new content is less than 50% the size of the existing file. This prevents accidental truncation of critical files. System files under 200 bytes are also blocked.

**Syntax Validation** — Every Python patch is run through `ast.parse()` before the staging instance boots. If it fails, the deploy is rolled back and the error (with context lines) is returned to the Coder.

**Credential Redaction** — All output flowing through the Worker and Orchestrator is scrubbed for tokens, API keys, passwords, and bearer credentials before entering the pipeline. Sensitive files (`.env`) cannot be read by the agent.

**Path Validation** — All file operations are restricted to the project directory. The Worker resolves and validates every path before touching the filesystem.

**Rollback** — Every deployment archives the previous version. Git history provides additional checkpoint capability. The system can always go back.

---

## What's Running Under the Hood

| Component | Technology |
|-----------|-----------|
| LLM Abstraction | `shared/llm.py` — Ollama, vLLM, Claude API (Phase 34) |
| Local LLM | Qwen3 14B (q4_K_M quantization) via Ollama |
| Router LLM | Ministral 3B (fast intent classification) |
| GPU | NVIDIA RTX 5060 Ti 16GB |
| CPU | Intel i5-12600KF |
| RAM | 32GB DDR4 |
| Inference | Local-first, cloud fallback via provider abstraction |
| Web Search | Self-hosted SearXNG instance |
| Home Automation | Home Assistant integration |
| Voice | Whisper STT + Piper TTS (Cori voice) |
| Dashboard | Streamlit with SSE event streaming |
| Services | FastAPI + Uvicorn |
| Proxy | aiohttp reverse proxy |
| OS | Windows 11 |

---

## Launch

```bat
launch_system.bat
```

That's it. One script starts all services in the right order, opens Mission Control in your browser, and you're talking to the system.

```
  Gateway          : http://localhost:8000
  Orchestrator     : http://localhost:8001
  Coder            : http://localhost:8002
  Worker           : http://localhost:8003
  Memory           : http://localhost:8004
  Scheduler        : http://localhost:8005
  Watchtower       : http://localhost:8010
  RAG Bridge       : http://localhost:8011
  Watchtower Cron  : http://localhost:8012
  Roux Voice       : http://localhost:8014
  Dashboard        : http://localhost:8501
```

---

## Project History

This system was built iteratively over the course of months, phase by phase. Each phase solved a real problem encountered in the previous one.

**Phases 0–1** — Foundation. Multi-agent architecture, tiered routing, git-based checkpoints. The system could generate code and execute tasks but had no memory and no self-awareness.

**Phases 2–5** — Intelligence. Web search integration via SearXNG. Coder gained the ability to read the codebase, plan multi-step operations, and write files through the Worker. Memory agent provided RAG-based context.

**Phases 6–10** — Reliability. Unified logging. Structured schemas. Error propagation. The system stopped silently failing and started telling you what went wrong and why.

**Phases 11–14** — Autonomy. The Watchtower was born — a 24/7 supervisor loop that manages tasks, monitors infrastructure, and diagnoses errors. Self-healing mode: when a service crashes, the Watchtower detects it and proposes a fix. Mission Control dashboard gave the system a face.

**Phases 15–17** — Safety. Write protection circuit breakers. Credential redaction. Path validation. The system learned not to destroy itself. Immutable files established the "supervisor can't be supervised" principle.

**Phase 18** — Self-modification. The Coder gained `patch_file` with surgical `===PATCH===` and `===REPLACE===` delimiters. Syntax validation with automatic rollback. Service restart capability through the Watchtower. The system could now modify and restart its own components — but only through supervised channels.

**Phase 19** — Task queue. Non-blocking task execution. Priority scheduling. Queue persistence through the Watchtower. The system could handle multiple requests without blocking the chat interface.

**Phase 20** — Blue-green deployment. The Gateway reverse proxy. Staging environments. Health checks. Human approval gates. Zero-downtime swaps. Traffic draining. Automatic rollback. The full production deployment pipeline, running on a desktop PC in Delaware.

**Phases 21–23** — Skill extraction and memory. The system learned to identify reusable patterns from completed tasks, store them as named skills, and retrieve them via RAG. Action aliases and port normalization reduced Coder hallucination rates.

**Phase 24** — Memory decay. Half-life model prevents stale memories from polluting retrieval. Dedup and reuse shields keep the knowledge base clean.

**Phase 25** — Proposal system. Six observers (health, memory, codebase, tasks, resources, skills) scan the system and propose improvements. Coach agent enriches proposals with confidence scores. Auto-approve engine handles low-risk, reversible proposals autonomously.

**Phases 26–28** — Kill switch, execution budget, and safety hardening. Emergency stop freezes all autonomous execution. Budget caps limit hourly task throughput. Security audit locked all services to 127.0.0.1 and enforced SAFE_PATHS validation.

**Phase 29** — Voice. Whisper large-v3-turbo for STT (0.47s on GPU), Piper TTS with the Cori voice, and a unified conversational interface. Roux speaks, listens, and shares conversation history with the dashboard. VAD wake/sleep controls in Mission Control.

**Phase 30** — Open source release. RouxYou shipped to GitHub under MIT license. 67 files, 13,751 lines. Public repo at github.com/RouxYouAI/RouxYou.

**Phase 33** — Invoice system. Sequential numbering, tracking DB, status lifecycle, PDF generation. Integrated into the dashboard and scheduler service.

**Phase 34** — LLM provider abstraction. `shared/llm.py` — unified interface for Ollama, vLLM (OpenAI-compatible), and Anthropic Claude. 13 hardcoded Ollama calls refactored. Alias routing maps logical names (router, coder, bigbrain) to provider/model pairs. Config-driven via `config.yaml` with hot-reload endpoint.

**Phase 35** — Context compaction and cost tracking. Token-aware prompt budgets for Coder. Auto-records usage from every LLM call with pricing for Claude models. Session/daily/provider breakdowns exposed via REST API and dashboard.

**Phase 36** — Hook pipeline and permission tiers. Pre/post tool-use hooks (permission checks, truncation guards, credential redaction). Three-tier permission model: READ_ONLY, WORKSPACE_WRITE, ELEVATED. Centralized security policy.

**Phase 37** — Structured event protocol and event bus. 17 typed event types replacing free-form activity broadcasting. Publish/subscribe bus with SSE support. Dashboard event feed shows real-time agent thoughts, tool calls, step progress, and errors.

**Phase 38** — MCP client. JSON-RPC 2.0 consumer for Model Context Protocol servers. Multi-server manager with tool discovery and invocation.

---

## The Philosophy

Most AI agent projects optimize for demos. This one optimizes for not breaking.

The system is designed around a single principle: **the agent can improve itself, but it can never outrun human oversight.** Every modification goes through staging. Every deployment requires approval. The supervisor is immutable. The safety layers are not suggestions — they're enforced in code that the agent cannot reach.

The LLM is local. The search is local. The memory is local. Nothing leaves the machine unless you tell it to. Data sovereignty isn't a feature — it's the foundation.

This is what happens when you give a 14B parameter model the ability to read its own source code, plan changes, write patches, test them in isolation, and deploy them to production — and then you let it run for months, building up episodic memory, learning which patterns work, and getting better at the thing it does most: improving itself.

---

## Credits

**DJ** — Architecture, vision, testing, and every late-night debugging session that got the system to where it is.

**Claude (Anthropic)** — Implementation partner. Wrote most of the code across 38+ phases, debugged Qwen's output format through 7 iterations of deploy pipeline fixes, and authored this README at 2 AM because DJ said "tell the world what it is." Now also available as "Big Brain" — the cloud fallback for complex reasoning tasks that exceed what a 14B local model can handle.

**Qwen3 14B** — The local LLM that actually runs inside the system. Plans tasks, writes code, and occasionally invents action names that don't exist (which is why the normalizer has an alias table).

---

*Last updated: April 2, 2026*
*Phase 38 — MCP client, event streaming, cost tracking, provider abstraction. The system rewrites itself, watches itself, and talks to you about it.*
