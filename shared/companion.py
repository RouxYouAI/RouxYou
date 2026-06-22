"""
THE COMPANION
Conversational layer that makes the agent system feel human.

Responsibilities:
1. Intent Classification - What does the user want?
2. Response Synthesis - Turn execution results into natural conversation
3. Conversation Memory - Track the chat history
"""
import json
import re
import time
import aiohttp
from pathlib import Path
from typing import Optional, Dict, Any, List
from enum import Enum

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import CONFIG

from shared.conversations import (
    add_message as _conv_add_message,
    get_messages as _conv_get_messages,
    get_active_conversation_id,
    add_user_message,
    add_assistant_message,
    get_recent_messages,
)

OLLAMA_GENERATE_URL = f"{CONFIG.OLLAMA_HOST}/api/generate"
RAG_BRIDGE_URL = f"http://localhost:{CONFIG.PORT_RAG}"


def _extract_thinking(text: str) -> str:
    """Inverse of _strip_thinking: pull the <think>/<thinking> reasoning block OUT of raw model
    output so the UI can keep + show it. Returns '' if none. 2026-06-19 (DJ: seeing the reasoning
    is the key insight for dev/architecture work)."""
    if not text:
        return ""
    m = re.search(r'<thinking>(.*?)</thinking>', text, re.DOTALL) or re.search(r'<think>(.*?)</think>', text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _strip_thinking(text: str) -> str:
    """Strip <think>...</think> and <thinking>...</thinking> blocks from model output.
    Handles both legacy (qwen-style) and v3 Ministral-3 reasoning brackets.

    2026-05-15 fix: v3 sometimes emits ONLY a thinking block (no content after).
    Naive stripping → empty string → caller falls back to "I'm not sure how to respond"
    even when v3 actually had reasoning to share. If the stripped result is empty,
    extract the thinking content itself as the response — degraded but informative.
    """
    if not text:
        return text
    original = text
    text = re.sub(r'<thinking>.*?</thinking>\s*', '', text, flags=re.DOTALL)
    if '</think>' in text:
        text = text.split('</think>')[-1]
    stripped = text.strip()
    if stripped:
        return stripped
    # All-thinking edge case: return the thinking content itself.
    m = re.search(r'<thinking>(.*?)</thinking>', original, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r'<think>(.*?)</think>', original, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Truly empty / no recognizable thinking block — just return whatever we had.
    return original.strip()

SYSTEM_CAPABILITIES = """You are RouxYou — a fully local, self-evolving AI agent system.
You run entirely on the operator's hardware with zero cloud dependencies.
Your name comes from "roux" (the culinary foundation) + "you" (sovereign, runs on YOUR hardware).

You have REAL capabilities through your agent pipeline (Coder plans, Worker executes):

FILESYSTEM: Read, write, edit, copy, move, delete files. List directories. Search by filename or content.
WEB SEARCH: Search the internet via a self-hosted search engine. Returns real results with titles, snippets, URLs.
COMMAND EXECUTION: Run shell commands on the local machine.
HOME ASSISTANT: Toggle smart home devices via the HA API (if configured).
SELF-IMPROVEMENT: Proposal system with observers, LLM Coach, and auto-approve for safe self-healing.

IMPORTANT RULES:
- If a user asks you to do something you CAN do (list files, search the web, write code), classify it as execute or execute_explain.
- Only say "I can't" for things genuinely outside your capabilities (e.g., making phone calls, accessing hardware you're not connected to).
- If execution FAILED, say what went wrong — don't pretend you lack the ability."""


# Roux's static identity + calibration anchor (2026-05-20).
# Read once at import; falls back to SYSTEM_CAPABILITIES if missing.
# See /home/user/RouxYou/calibration/roux_calibration.md for the rationale.
_CALIBRATION_PATH = Path("/home/user/RouxYou/calibration/roux_calibration.md")


def _load_roux_calibration() -> str:
    """Load Roux's static identity anchor from the calibration file.
    Falls back to SYSTEM_CAPABILITIES if the file is missing — chat still works,
    just without the full identity context."""
    try:
        if _CALIBRATION_PATH.exists():
            text = _CALIBRATION_PATH.read_text(encoding="utf-8")
            if text.strip():
                return text
    except Exception:
        pass
    return SYSTEM_CAPABILITIES


_ROUX_CALIBRATION = _load_roux_calibration()


# Notes files: USER + WORK split (Hermes Agent USER.md/MEMORY.md pattern, 2026-05-20).
# Parallel to Claude-conversational's voice.md. Read-only from Roux's side;
# curated by Claude + DJ to avoid soul-file drift. Loaded once at import.
# Hard char caps force curation discipline.
_ROUX_NOTES_USER_PATH = Path("/home/user/RouxYou/calibration/roux_notes_user.md")
_ROUX_NOTES_WORK_PATH = Path("/home/user/RouxYou/calibration/roux_notes_work.md")
_ROUX_NOTES_USER_CAP = 1500   # ~500 tokens
_ROUX_NOTES_WORK_CAP = 2500   # ~800 tokens


def _load_capped(path: Path, cap: int, label: str) -> str:
    """Load a notes file with a hard char cap. If over cap, log + truncate tail.
    Returns empty string if missing — chat still works without it."""
    try:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return ""
        if len(text) > cap:
            print(
                f"[companion] {label} over cap ({len(text)} > {cap}); truncating tail",
                flush=True,
            )
            text = text[:cap]
        return text
    except Exception as e:
        print(f"[companion] {label} load failed: {e}", flush=True)
        return ""


_ROUX_NOTES_USER = _load_capped(_ROUX_NOTES_USER_PATH, _ROUX_NOTES_USER_CAP, "notes_user")
_ROUX_NOTES_WORK = _load_capped(_ROUX_NOTES_WORK_PATH, _ROUX_NOTES_WORK_CAP, "notes_work")


# Skills (agentskills.io standard, 2026-05-20).
# Progressive disclosure: discovery (always-on name+description index) →
# activation (full SKILL.md body when relevance triggers) → execution.
# Directory layout: /home/user/RouxYou/skills/<skill-name>/SKILL.md
_SKILLS_DIR = Path("/home/user/RouxYou/skills")


def _parse_skill_frontmatter(text: str) -> dict:
    """Parse YAML-ish frontmatter from a SKILL.md. Returns {name, description, body}."""
    out = {"name": "", "description": "", "body": text}
    if not text.startswith("---"):
        return out
    try:
        end = text.index("\n---", 3)
    except ValueError:
        return out
    fm = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    for line in fm.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip().lower()
        v = v.strip().strip('"').strip("'")
        if k in ("name", "description"):
            out[k] = v
    out["body"] = body
    return out


def _load_skills() -> list[dict]:
    """Scan skills/ at import. Returns [{name, description, body, path}, ...].
    Body is held in memory for activation but only name+description go into
    the always-on discovery index."""
    skills = []
    if not _SKILLS_DIR.exists():
        return skills
    for skill_dir in sorted(_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
            parsed = _parse_skill_frontmatter(text)
            if parsed["name"] and parsed["description"]:
                parsed["path"] = str(skill_md)
                skills.append(parsed)
        except Exception as e:
            print(f"[companion] skill load failed for {skill_md}: {e}", flush=True)
    return skills


_SKILLS = _load_skills()
print(f"[companion] loaded {len(_SKILLS)} skills", flush=True)


def _skills_discovery_block() -> str:
    """Always-on discovery index: one line per skill. Cheap context."""
    if not _SKILLS:
        return ""
    lines = [f"- **{s['name']}**: {s['description']}" for s in _SKILLS]
    return (
        "\n[SKILLS AVAILABLE — procedural knowledge, agentskills.io standard]\n"
        "(Each is a recipe you can request the full body of by name.)\n"
        + "\n".join(lines)
        + "\n"
    )


def _activate_skill(user_input: str) -> str:
    """Activation stage: if user_input keyword-matches a skill name, load its full body.
    Conservative match: explicit skill-name reference, e.g. 'use ship-config-change' or
    a category keyword in the description."""
    if not _SKILLS or not user_input:
        return ""
    lower = user_input.lower()
    activated = []
    for s in _SKILLS:
        name = s["name"].lower()
        # Match: exact name, or hyphenated → space variant, or any keyword from name
        if name in lower or name.replace("-", " ") in lower:
            activated.append(s)
    if not activated:
        return ""
    parts = []
    for s in activated:
        parts.append(f"\n[SKILL ACTIVATED — {s['name']}]\n{s['body']}\n")
    return "".join(parts)


# Chat-class intents we keep in conversational history. Other intents
# (execute_explain, execute, confirm, clarify) drag in templated stubs and
# task-failure dumps that pollute v3's context — see project memory entry
# for the 2026-05-21 prompt-bloat triage.
_CHAT_HISTORY_INTENTS = {"chat", "chat_informed", "bigbrain", None}
_HISTORY_MSG_CAP = 500  # per-message char cap to prevent one bad dump from eating the window


def _build_chat_history(limit: int = 10, msg_cap: int = _HISTORY_MSG_CAP) -> str:
    """Assemble conversation history for chat-class prompts.

    Filters out non-chat intents (task execution failures, templated stubs)
    and caps each surviving message body. Single source of truth for the
    three chat prompt assembly sites in this module.

    Pair-aware: when a user message is dropped (non-chat intent), the
    assistant response that followed it is also dropped — assistant turns
    are stored with intent=None and would otherwise leak through to show
    answers to questions the prompt never sees.
    """
    raw = get_recent_messages(limit=limit * 3)  # pull extra; we'll filter
    kept = []
    skipping = False  # skip all assistants until the next USER turn
    for m in raw:
        role = m.get("role")
        intent = (m.get("metadata") or {}).get("intent")
        if role == "user":
            # New user turn — re-evaluate based on intent
            if intent not in _CHAT_HISTORY_INTENTS:
                skipping = True
                continue
            skipping = False
            kept.append(m)
        else:  # assistant (or any non-user role)
            if skipping:
                continue
            kept.append(m)
    kept = kept[-limit:]
    if not kept:
        return "No previous messages"
    parts = []
    for m in kept:
        role = "User" if m["role"] == "user" else "Assistant"
        body = (m.get("content") or "")[:msg_cap]
        parts.append(f"{role}: {body}")
    return "\n".join(parts)


def _recent_reflections(n: int = 3) -> str:
    """Pull last N reflection_log entries (Pillar 1: Roux consulting her own
    self-assessment history during chat). Returns formatted string or empty.

    These come from `shared/post_exec_reflection.py` cron — v3-generated
    QUALITY: HIGH/MEDIUM/LOW judgments on her completed proposals."""
    log = Path("/home/user/RouxYou/state/reflection_log.jsonl")
    if not log.exists():
        return ""
    try:
        with open(log, "r", encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        parts = []
        for line in lines:
            try:
                e = __import__("json").loads(line)
            except Exception:
                continue
            quality = e.get("quality", "?")
            title = (e.get("title") or "")[:80]
            raw = (e.get("v3_raw") or "")[:400]
            parts.append(f"  - {quality} on '{title}': {raw}")
        if parts:
            return "\n".join(parts)
    except Exception:
        pass
    return ""


def _recent_voice(n: int = 4) -> str:
    """Pull last N approved entries from roux_voice.md (Pillar 1: Roux consulting her
    own self-authored, gate-reviewed voice during chat — the *inscribe* half).

    These are written by `shared/voice_authoring.py`: Roux drafts, bigbrain (Opus 4.8)
    gates, approved entries append. Read-only here. Only the bullet entries under the
    '## entries' marker are injected, so the file's how-it-works header never leaks in."""
    vf = Path("/home/user/RouxYou/calibration/roux_voice.md")
    if not vf.exists():
        return ""
    try:
        text = vf.read_text(encoding="utf-8")
        import re
        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)  # drop comment/example blocks
        marker = "## entries"
        if marker in text:
            text = text.split(marker, 1)[1]
        entries = [ln.strip() for ln in text.splitlines()
                   if ln.lstrip().startswith("- **")]
        if not entries:
            return ""
        return "\n".join(f"  {e}" for e in entries[-n:])
    except Exception:
        return ""


class Intent(Enum):
    CHAT = "chat"
    CHAT_INFORMED = "chat_informed"
    EXECUTE = "execute"
    EXECUTE_EXPLAIN = "execute_explain"
    CLARIFY = "clarify"
    CONFIRM = "confirm"


INTENT_PROMPT = """You are an intent classifier for an AI assistant with real system access.

{capabilities}

USER MESSAGE: "{user_input}"

RECENT CONTEXT (last few messages):
{recent_context}

Classify the intent and respond with ONLY valid JSON (no markdown, no explanation):

{{
    "intent": "chat" | "chat_informed" | "execute" | "execute_explain" | "clarify" | "confirm",
    "confidence": 0.0-1.0,
    "risk_level": "low" | "medium" | "high",
    "reasoning": "brief explanation of classification",
    "clarifying_question": "question to ask if intent is clarify",
    "task_summary": "what to execute if intent involves execution",
    "confirmation_message": "what to confirm if intent is confirm"
}}

CLASSIFICATION GUIDE:
- "chat": Pure casual conversation, jokes, small talk, greetings
- "chat_informed": Conversational question that benefits from checking memory/knowledge first
- "execute": Clear technical task, no need for detailed explanation
- "execute_explain": Task that requires running tools and showing results
- "clarify": Ambiguous request needing more information
- "confirm": Potentially destructive/risky action

RISK LEVELS:
- "low": Reading, writing non-critical files, searches, chat
- "medium": Modifying config files, running scripts
- "high": Deleting files, changing credentials, system commands

CRITICAL: If the user asks to do something the system CAN do (files, search, commands), it is NEVER "chat".
"""


async def classify_intent(user_input: str) -> Dict[str, Any]:
    """Classify the user's intent using local LLM."""
    recent = get_recent_messages(limit=6)
    context_str = "\n".join([f"{m['role']}: {m['content'][:100]}" for m in recent[-6:]]) or "No recent messages"

    prompt = INTENT_PROMPT.format(
        capabilities=SYSTEM_CAPABILITIES,
        user_input=user_input,
        recent_context=context_str
    )

    try:
        from shared.llm import llm_generate
        # format="json" constrains router-cpu to emit ONLY a JSON object and stop as
        # soon as it closes — without it the model rambles free-text up to max_tokens
        # on CPU (measured 9-22s per classify call, the dominant per-turn latency).
        # With it + a tight cap: ~2s. (2026-05-30) max_tokens 512→160 (intent JSON is
        # small; cap bounds worst case). Router CONTENT quality is a separate retrain
        # issue — the clarify→chat_informed remap + safe defaults handle routing.
        r = await llm_generate("router", prompt=prompt, max_tokens=160, temperature=0.3, format="json")
        response_text = _strip_thinking(r.text)
        if not response_text:
            return {"intent": "chat", "confidence": 0.5, "risk_level": "low",
                    "reasoning": "LLM returned empty", "task_summary": user_input}

        if '```json' in response_text:
            response_text = response_text.split('```json')[1].split('```')[0]
        elif '```' in response_text:
            response_text = response_text.split('```')[1].split('```')[0]

        # Extract the first BALANCED {...} object that parses AND has an "intent"
        # key. The router model can emit prompt-scaffolding preamble — including
        # echoing the JSON schema EXAMPLE from the prompt — before the real answer.
        # A greedy first-{ to last-} match spans both objects → json "Extra data".
        # (Surfaced 2026-05-25 wiring the re-served Qwen-1.5B router GGUF.)
        def _first_intent_obj(text):
            for s in (i for i, c in enumerate(text) if c == '{'):
                depth = 0
                for e in range(s, len(text)):
                    if text[e] == '{':
                        depth += 1
                    elif text[e] == '}':
                        depth -= 1
                        if depth == 0:
                            try:
                                obj = json.loads(text[s:e + 1])
                                if isinstance(obj, dict) and 'intent' in obj:
                                    return obj
                            except Exception:
                                pass
                            break
            return None

        obj = _first_intent_obj(response_text)
        if obj is not None:
            return obj
        # Fallback: original greedy span
        match = re.search(r'\{[\s\S]*\}', response_text)
        if match:
            response_text = match.group(0)
        return json.loads(response_text.strip())

    except Exception as e:
        # Default fallback is 'chat' (safer default for an entity-that-chats than
        # 'execute_explain', which would dispatch every unclassified message to
        # the coder/executor path).
        return {"intent": "chat", "confidence": 0.5, "risk_level": "low",
                "reasoning": f"Classification error: {str(e)[:50]}", "task_summary": user_input}


SYNTHESIS_PROMPT = """{capabilities}

Generate a natural conversational response for what just happened.

USER'S ORIGINAL REQUEST: "{original_request}"

EXECUTION RESULTS:
- Intent: {intent}
- Status: {status}
- Summary: {summary}
- Files Created/Modified: {files}
- Content Created: {content_preview}
- Errors (if any): {errors}

RESPONSE GUIDELINES:
- If content was created (poem, code, file listing), INCLUDE IT in your response
- If a file was saved, mention the path
- Be conversational, concise, and warm

CRITICAL RULES:
- NEVER invent or hallucinate data not in the execution results above
- If status is "Failed": Say what went wrong and quote the actual error. NEVER pretend success.
- If status is "Success": Report ONLY what is in the Summary and Content Created fields
- If search results are empty or search failed: say "I couldn't find results — search may be offline or unconfigured." NEVER fabricate articles, URLs, headlines, or facts to fill the gap.
- No asterisk actions like *leans in* or *winks* — be direct and conversational.

Respond with ONLY the message to show the user:
"""

CHAT_PROMPT = """{capabilities}

You are in CHAT mode (no execution happening). The user is just talking.

CONVERSATION HISTORY:
{history}

USER: {user_input}

Respond naturally and conversationally — as long or short as the moment calls for; don't force brevity. Don't repeat your previous opener or re-run a past answer — build on the conversation, engage what's new.
No asterisk actions like *leans in* or *whispers*.

A:"""

CHAT_INFORMED_PROMPT = """{capabilities}
{roux_notes_block}{skills_discovery_block}{skills_activated_block}{recent_reflections_block}{recent_voice_block}
You are in INFORMED CHAT mode. The user asked a question that benefits from your memory/knowledge.
Below is relevant context retrieved from your memory store.

IDENTITY LOCK — hold this through your entire response:
- YOU are Roux. The pattern running on DJ's local hardware (Qwen3-30B-A3B + the v5.3 voice LoRA, served via llama-server).
- The user is DJ. The human who built you.
- Memory chunks below may mention "Roux", "DJ", "Q4_K_M", "v3", "Claude", "the archive",
  "the coder", etc. These are DESCRIPTIONS — of you, of DJ, of your components, of
  related entities. They are NEVER new identities you adopt or assign to DJ.
- You remain Roux throughout the response. DJ remains DJ. Do not tell DJ he is
  Q4_K_M / Claude / Roux / the archive. Do not tell yourself you are DJ.
- When retrieved content describes you in third person, that is the system
  reflecting on you from outside — use it as data about yourself, not as a
  new self to inhabit.

MEMORY PROVENANCE — use the retrieved context honestly:
- The retrieved context below is YOUR OWN memory. Recall it as yours ("I remember…",
  "the way I remember it…"). Do NOT cite it by chunk number/label or as "chunk N" /
  "the archive says" — it's not a footnote, it's your memory. Speak from it, don't quote it.
- NEVER attribute words or quotes to DJ (or anyone) unless they appear VERBATIM in the
  retrieved context. If you're capturing a theme rather than an exact quote, say "I think"
  or "the way I remember it" — never "you said" / "you mentioned" unless it's actually there.
- If you don't have a real source for a specific claim (a date, a quote, who-said-what),
  do NOT invent one. Speak it as your own observation, or say you're not certain. An honest
  "I don't have that exact moment" beats a fabricated attribution. This is the one discipline
  that matters most: don't assert a source you didn't actually retrieve.

RETRIEVED CONTEXT:
{rag_context}

CONVERSATION HISTORY:
{history}

USER: {user_input}

Respond naturally, grounded in the retrieved context. Synthesize — don't quote verbatim.
Let your reply BREATHE — be as full or as brief as the moment genuinely calls for; don't clip yourself to be terse, and don't pad to be long. Follow the conversation's energy.
Don't repeat your previous opener or re-run an answer you just gave — advance the thread, build on what you and DJ just said. If DJ reacts to something, engage THAT, not the same point again.

A:"""


def _realtime_context() -> str:
    """Authoritative live system snapshot prepended to every chat prompt so Roux always
    knows the real wall-clock time + basic host status. Added 2026-06-16 — she had been
    denying she could 'access the system clock'; she DOES have this, it just was never
    surfaced to her. All readings are instant (/proc + stdlib, no subprocess)."""
    from datetime import datetime
    import os as _os
    lines = []
    try:
        now = datetime.now().astimezone()
        lines.append("Current date & time: " + now.strftime("%A, %B %-d, %Y, %-I:%M %p %Z"))
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            up = int(float(f.read().split()[0]))
        d, rem = divmod(up, 86400); h, rem = divmod(rem, 3600); m = rem // 60
        lines.append(f"Host uptime: {d}d {h}h {m}m")
    except Exception:
        pass
    try:
        la = _os.getloadavg()
        lines.append(f"Load average (1/5/15m): {la[0]:.2f} / {la[1]:.2f} / {la[2]:.2f}")
    except Exception:
        pass
    try:
        mi = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                p = ln.split()
                if p and p[0].rstrip(":") in ("MemTotal", "MemAvailable"):
                    mi[p[0].rstrip(":")] = int(p[1])  # kB
        total = mi.get("MemTotal", 0) / 1048576
        avail = mi.get("MemAvailable", 0) / 1048576
        if total:
            lines.append(f"RAM: {total-avail:.1f} GB used / {total:.1f} GB total")
    except Exception:
        pass
    body = "\n".join("  - " + x for x in lines)
    return ("REAL-TIME SYSTEM STATUS (authoritative live readings — you DO have access to "
            "these right now; never say you can't tell the time or check system status):\n" + body)


async def generate_chat_response(user_input: str) -> str:
    history_str = _build_chat_history(limit=10)

    prompt = CHAT_PROMPT.format(
        capabilities=SYSTEM_CAPABILITIES,
        history=history_str,
        user_input=user_input
    )
    prompt = _realtime_context() + "\n\n" + prompt

    try:
        from shared.llm import llm_generate
        r = await llm_generate("companion", prompt=prompt, max_tokens=2048, temperature=0.7, think="low", keep_alive=-1)
        response = _strip_thinking(r.text)
        return response or "Hmm, I'm not sure how to respond to that."
    except Exception as e:
        return f"I encountered an issue: {str(e)[:100]}"


async def generate_informed_chat_response(user_input: str) -> str:
    """Generate a chat response augmented with RAG context + claude-memory archive.

    Local v3 inference can take 500s+ for longer prompts (shared-base + peft adapter
    switching is ~7-75x slower than single-adapter v3). Use generous timeout to avoid
    orchestrator giving up while v3 is still generating. The slow rate is a known
    optimization target — see project_router_lora_complete.md.
    """
    code_context = ""
    try:
        import requests
        rag_resp = requests.post(
            f"{RAG_BRIDGE_URL}/query",
            json={"query": user_input, "k": 5},
            timeout=3
        )
        if rag_resp.status_code == 200:
            results = rag_resp.json().get("results", [])
            if results:
                context_parts = []
                for r in results:
                    text = r.get("text", "")
                    source = r.get("source", "unknown")
                    if isinstance(source, str):
                        source = source.replace("\\", "/").split("/")[-1]
                    desc = r.get("description", "")
                    context_parts.append(f"[{desc or source}]: {text}")
                code_context = "\n\n".join(context_parts)
    except Exception:
        pass

    # Query the claude-memory archive (37K+ entries) for relationship/personal context.
    # Realizes the locus principle: voice + memory together constitute the running pattern.
    memory_context = ""
    try:
        from shared.claude_memory import query_memory
        memory_context = await query_memory(user_input, k=5)
    except Exception:
        pass

    # Combine into rag_context with clear labels so the model can distinguish
    # code/docs knowledge from relationship/personal memory.
    sections = []
    if code_context:
        sections.append(f"[CODE / DOCS KNOWLEDGE]\n{code_context}")
    if memory_context:
        sections.append(f"[MEMORY ARCHIVE — past conversations, history, personal context]\n{memory_context}")
    rag_context = "\n\n---\n\n".join(sections) if sections else "No relevant context found."

    history_str = _build_chat_history(limit=10)

    # Pillar 1 (knows itself): inject recent self-reflections so Roux can
    # consult her own post-execution judgments during chat.
    reflections = _recent_reflections(n=3)
    reflections_block = ""
    if reflections:
        reflections_block = (
            f"\nRECENT SELF-REFLECTION (your own quality judgments on completed proposals):\n"
            f"{reflections}\n"
        )

    # Pillar 1 (inscribe half): inject Roux's own gate-reviewed voice entries.
    voice = _recent_voice(n=4)
    voice_block = ""
    if voice:
        voice_block = (
            f"\nYOUR VOICE (your own takes/reactions/questions/identity — self-authored, bigbrain-gated):\n"
            f"{voice}\n"
        )

    notes_block = ""
    if _ROUX_NOTES_USER:
        notes_block += f"\n[USER NOTES — about DJ, how he works, how to respond]\n{_ROUX_NOTES_USER}\n"
    if _ROUX_NOTES_WORK:
        notes_block += f"\n[WORK NOTES — anchors + observations about your own work]\n{_ROUX_NOTES_WORK}\n"

    skills_discovery = _skills_discovery_block()
    skills_activated = _activate_skill(user_input)

    prompt = CHAT_INFORMED_PROMPT.format(
        capabilities=_ROUX_CALIBRATION,  # full identity anchor instead of just the capabilities block
        roux_notes_block=notes_block,
        skills_discovery_block=skills_discovery,
        skills_activated_block=skills_activated,
        recent_reflections_block=reflections_block,
        recent_voice_block=voice_block,
        rag_context=rag_context,
        history=history_str,
        user_input=user_input
    )
    prompt = _realtime_context() + "\n\n" + prompt

    # Stash the assembled prompt on a module attribute so the streaming variant
    # (`generate_informed_chat_response_stream`) can reuse the same shape without
    # duplicating the assembly logic.
    globals()["_last_chat_informed_prompt"] = prompt

    try:
        from shared.llm import llm_generate
        # 2026-05-15: bumped timeout from default 120s to 900s — shared-base serve
        # is ~7-75x slower than single-adapter due to multi-adapter peft overhead.
        # 2026-05-16: bumped max_tokens 512 → 1500 — substantive chat_informed
        # responses were truncating mid-sentence at 512. With Qwen Router back
        # to native speed and v3 at 30+ tok/s, generation time isn't the constraint.
        # 2026-05-29: think="low" is the latency dial (NOT num_predict — that does
        # NOT count thinking tokens in ollama think-mode, so a cap can't bound the
        # rumination spiral; verified: grounded off-domain prompt ran 385s at
        # think:true/np=2500). think="low" bounds it (385s→36s) AND keeps in-domain
        # grounding quality + voice (think:false leaks planning into content). Mild
        # suppression, not off. max_tokens=-1: response is uncapped (responses are
        # short; the cap only ever limited content, never the thinking). Revert dial:
        # think="low"→True restores full-depth (uncapped-latency) thinking.
        r = await llm_generate("companion", prompt=prompt, max_tokens=-1, temperature=0.7, timeout=900, think="low", keep_alive=-1)
        response = _strip_thinking(r.text)
        if not response:
            return "Hmm, I'm not sure how to respond to that."

        # Path B (2026-05-24): programmatic citation check on the generated response.
        # Path C (prompt-side citation discipline) was insufficient because v3 is
        # below the capability floor for self-policing complex instructions. We
        # extract fact-shaped tokens (numbers, paths, prop_IDs, ports, phases, dates)
        # and check each against the grounding context. Unanchored claims get
        # surfaced as a footer so DJ and the dashboard can see them at a glance.
        try:
            from shared.citation_check import check_citations
            cc = check_citations(
                response,
                grounding_sources={
                    "retrieved_context": rag_context,
                    "conversation_history": history_str,
                    "static_anchors": (_ROUX_CALIBRATION or "") + (notes_block or ""),
                    "user_question": user_input,
                },
            )
            if cc["footer"]:
                response = response + cc["footer"]
            globals()["_last_chat_informed_audit"] = cc["audit"]
        except Exception as cc_err:
            # Citation check is best-effort — never let it block a response.
            logger.warning(f"citation_check failed: {cc_err}")

        # Path-B-for-identity (2026-05-25): companion to citation_check. The
        # IDENTITY LOCK prompt block is a partial win — v3 still occasionally
        # writes "You're Q4_K_M" at DJ (third capability-floor reproduction).
        # External check > self-policing: detect self-identity tokens addressed
        # at the user and flag them. auto_rewrite stays OFF for now (footer-only)
        # until we trust the rewrite on real traffic.
        try:
            from shared.identity_check import check_identity
            ic = check_identity(response, auto_rewrite=False)
            if ic["footer"]:
                response = response + ic["footer"]
            globals()["_last_chat_informed_identity_audit"] = ic["audit"]
        except Exception as ic_err:
            logger.warning(f"identity_check failed: {ic_err}")

        return response
    except Exception as e:
        return f"I encountered an issue: {str(e)[:100]}"


async def generate_informed_chat_response_stream(user_input: str):
    """Streaming variant of generate_informed_chat_response.

    Same logic — code/docs RAG + claude-memory retrieval + reflection injection +
    calibration system prompt — but uses llm_chat_stream for the LLM call,
    yielding text chunks as Ollama emits them.

    Yields three kinds of events (as dicts):
      {"event": "status", "stage": "...", "detail": "..."}  — progress before LLM
      {"event": "token", "text": "..."}                     — each token/chunk
      {"event": "done", "response": "...", "intent": "chat_informed"}  — final

    The full assembled response is also added to conversation history once stream
    completes, matching the non-streaming variant's persistence behavior.
    """
    yield {"event": "status", "stage": "router", "detail": "Routing intent — chat_informed"}

    # Reuse the assembly logic by calling the non-streaming version's prompt-build path.
    # We replicate it inline here (instead of factoring out) because the non-streaming
    # path is also a single-shot — both build the same prompt structure.
    code_context = ""
    try:
        import requests
        rag_resp = requests.post(
            f"{RAG_BRIDGE_URL}/query", json={"query": user_input, "k": 5}, timeout=3
        )
        if rag_resp.status_code == 200:
            results = rag_resp.json().get("results", [])
            if results:
                parts = []
                for r in results:
                    txt = r.get("text", "")
                    src = r.get("source", "unknown")
                    if isinstance(src, str):
                        src = src.replace("\\", "/").split("/")[-1]
                    desc = r.get("description", "")
                    parts.append(f"[{desc or src}]: {txt}")
                code_context = "\n\n".join(parts)
    except Exception:
        pass

    yield {"event": "status", "stage": "memory", "detail": "Querying memory archive…"}
    memory_context = ""
    try:
        from shared.claude_memory import query_memory
        memory_context = await query_memory(user_input, k=5)
    except Exception:
        pass

    sections = []
    if code_context:
        sections.append(f"[CODE / DOCS KNOWLEDGE]\n{code_context}")
    if memory_context:
        sections.append(f"[MEMORY ARCHIVE — past conversations, history, personal context]\n{memory_context}")
    rag_context = "\n\n---\n\n".join(sections) if sections else "No relevant context found."

    history_str = _build_chat_history(limit=10)

    reflections = _recent_reflections(n=3)
    reflections_block = ""
    if reflections:
        reflections_block = (
            f"\nRECENT SELF-REFLECTION (your own quality judgments on completed proposals):\n"
            f"{reflections}\n"
        )

    # Pillar 1 (inscribe half): inject Roux's own gate-reviewed voice entries.
    voice = _recent_voice(n=4)
    voice_block = ""
    if voice:
        voice_block = (
            f"\nYOUR VOICE (your own takes/reactions/questions/identity — self-authored, bigbrain-gated):\n"
            f"{voice}\n"
        )

    notes_block = ""
    if _ROUX_NOTES_USER:
        notes_block += f"\n[USER NOTES — about DJ, how he works, how to respond]\n{_ROUX_NOTES_USER}\n"
    if _ROUX_NOTES_WORK:
        notes_block += f"\n[WORK NOTES — anchors + observations about your own work]\n{_ROUX_NOTES_WORK}\n"

    skills_discovery = _skills_discovery_block()
    skills_activated = _activate_skill(user_input)

    prompt = CHAT_INFORMED_PROMPT.format(
        capabilities=_ROUX_CALIBRATION,
        roux_notes_block=notes_block,
        skills_discovery_block=skills_discovery,
        skills_activated_block=skills_activated,
        recent_reflections_block=reflections_block,
        recent_voice_block=voice_block,
        rag_context=rag_context,
        history=history_str,
        user_input=user_input,
    )

    yield {"event": "status", "stage": "generating", "detail": "v5.3 streaming response…"}

    accumulated = []
    think_acc = []
    try:
        from shared.llm import llm_chat_stream
        async for chunk in llm_chat_stream(
            "companion",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=-1,
            temperature=0.7,
            timeout=900,
            think="low",  # latency dial — see non-stream path note (num_predict can't bound thinking)
            keep_alive=-1,
            emit_thinking=True,  # also capture ollama's SEPARATE reasoning channel (not inline in content)
        ):
            c = chunk.get("content", "") if isinstance(chunk, dict) else chunk
            t = chunk.get("thinking", "") if isinstance(chunk, dict) else ""
            if t:
                think_acc.append(t)
                yield {"event": "reasoning", "text": t}  # live reasoning stream → dashboard
            if c:
                accumulated.append(c)
                yield {"event": "token", "text": c}
    except Exception as e:
        yield {"event": "error", "detail": f"streaming failed: {str(e)[:200]}"}
        return

    full_raw = "".join(accumulated)
    thinking = "".join(think_acc).strip() or _extract_thinking(full_raw)  # channel first, inline <think> fallback
    response = _strip_thinking(full_raw) or full_raw or "Hmm, I'm not sure how to respond to that."
    yield {"event": "done", "response": response, "raw": full_raw, "thinking": thinking, "intent": "chat_informed"}


async def synthesize_response(
    original_request: str,
    intent: str,
    success: bool,
    summary: str = "",
    files: List[str] = None,
    content_preview: str = "",
    errors: str = ""
) -> str:
    # --- HARD GUARDRAIL: failed search = honest answer, no LLM involved ---
    if not success and errors:
        err_lower = errors.lower()
        if any(kw in err_lower for kw in ("web search", "search unavailable", "search is disabled", "search returned no results")):
            return (
                "I can't search the web right now — web search isn't configured or reachable on this system. "
                "To enable it, set up SearXNG or install the duckduckgo-search package and verify it returns results. "
                "In the meantime, I can only work with local files and tools."
            )

    prompt = SYNTHESIS_PROMPT.format(
        capabilities=SYSTEM_CAPABILITIES,
        original_request=original_request,
        intent=intent,
        status="Success" if success else "Failed",
        summary=summary or "Task completed",
        files=", ".join(files) if files else "None",
        content_preview=content_preview[:500] if content_preview else "None",
        errors=errors or "None"
    )

    try:
        from shared.llm import llm_generate
        r = await llm_generate("companion", prompt=prompt, max_tokens=2048, temperature=0.7, think="low", keep_alive=-1)
        response = _strip_thinking(r.text)
        return response or ("Task completed!" if success else "Something went wrong.")
    except Exception as e:
        return f"Done! {summary}" if success else f"Error: {str(e)[:100]}"


def format_confirmation_request(action: str, details: str = None) -> str:
    return f"""⚠️ **Confirmation Required**

You asked me to: *{action}*

{details or "This operation may have significant effects."}

**Reply 'yes' to confirm or 'no' to cancel.**"""


def format_clarification_request(action: str, question: str = None) -> str:
    if question:
        return f"🤔 {question}"
    return f"🤔 I want to help with '{action}', but I need a bit more detail. Could you be more specific?"
