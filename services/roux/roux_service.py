"""
Roux — Intelligent Voice Service for RouxYou
=============================================
Port: CONFIG.PORT_ROUX

Modes:
  1. NOTIFICATION — agents POST events, Roux summarizes + speaks (if TTS configured)
  2. CONVERSATION (local) — chat + informational queries via local LLM
  3. COMMAND (dispatched) — execution tasks routed to orchestrator pipeline

TTS:
  "kitten"  — Self-hosted Kitten TTS server (set tts.kitten_url in config.yaml)
  "none"    — Text-only mode. Voice endpoints still work, responses are text only.

STT:
  faster-whisper (GPU). Lazy-loaded on first use.
  Wake word: configurable via WAKE_WORD constant.

All local. No cloud.
"""

import asyncio
import io
import json
import logging
import threading
import time
import wave
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

import sys
_HERE = Path(__file__).parent
_PROJECT_ROOT = _HERE.parent.parent
for _p in [str(_PROJECT_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import webrtcvad
    WEBRTCVAD_AVAILABLE = True
except ImportError:
    WEBRTCVAD_AVAILABLE = False

try:
    from scipy import signal as scipy_signal
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

import httpx
import numpy as np
import sounddevice as sd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared.logger import get_logger
from shared.lifecycle import register_process
from shared.conversations import (
    add_message as conv_add_message,
    get_messages as conv_get_messages,
    get_active_conversation_id,
)
from config import CONFIG

logger = get_logger("roux")

# ---------------------------------------------------------------------------
# Configuration — all from CONFIG
# ---------------------------------------------------------------------------

PORT           = CONFIG.PORT_ROUX
OLLAMA_URL     = f"{CONFIG.OLLAMA_HOST}/api/chat"
OLLAMA_MODEL   = CONFIG.MODEL_ROUTER

TTS_PROVIDER   = CONFIG.TTS_PROVIDER.lower()   # "kitten" | "none"
KITTEN_TTS_URL = CONFIG.KITTEN_TTS_URL + "/tts" if CONFIG.KITTEN_TTS_URL else ""
TTS_VOICE      = CONFIG.TTS_VOICE
TTS_SPEED      = CONFIG.TTS_SPEED

SAMPLE_RATE    = 24000
BATCH_WINDOW_SECONDS = 30
MAX_BATCH_SIZE = 20

# STT
WHISPER_MODEL   = "large-v3-turbo"
WHISPER_COMPUTE = "float16"
WHISPER_DEVICE  = "cuda"
MAX_LISTEN_SECONDS  = 30.0
MIN_AUDIO_SECONDS   = 0.5
MAX_CONVERSATION_HISTORY = 20

# VAD
VAD_AGGRESSIVENESS    = 2
VAD_SAMPLE_RATE       = 16000
VAD_FRAME_MS          = 30
VAD_SILENCE_FRAMES    = 50
VAD_MIN_SPEECH_FRAMES = 10

# Wake word
WAKE_WORD       = "roux"
WAKE_WORD_ALTS  = ["roux", "rue", "ru", "roo"]
WAKE_CLIP_MAX_FRAMES = 100
AWAKE_TIMEOUT_S = 30
SLEEP_COMMANDS  = [
    "go to sleep", "goodnight", "good night", "sleep",
    "bye roux", "goodbye roux", "stop listening",
]

# Service map — ports from CONFIG
SERVICE_MAP = {
    "orchestrator": f"http://localhost:{CONFIG.PORT_ORCHESTRATOR}",
    "memory":       f"http://localhost:{CONFIG.PORT_MEMORY}",
    "watchtower":   f"http://localhost:{CONFIG.PORT_WATCHTOWER_CRON}",
    "rag":          f"http://localhost:{CONFIG.PORT_RAG}",
}

# ---------------------------------------------------------------------------
# TTS Abstraction — speak() works regardless of provider
# ---------------------------------------------------------------------------

async def tts_speak(text: str) -> Optional[dict]:
    """
    Send text to TTS provider and play on speakers.
    Returns metadata dict on success, None if TTS is disabled or unavailable.
    Logs a warning and returns None gracefully — never raises.
    """
    if TTS_PROVIDER == "none" or not KITTEN_TTS_URL:
        logger.info(f"[TTS disabled] {text}")
        return None

    if TTS_PROVIDER == "kitten":
        return await _kitten_speak(text)

    logger.warning(f"Unknown TTS provider: {TTS_PROVIDER}")
    return None


async def _kitten_speak(text: str) -> Optional[dict]:
    """Speak via Kitten TTS server."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                KITTEN_TTS_URL,
                json={"text": text, "voice": TTS_VOICE, "speed": TTS_SPEED}
            )
            response.raise_for_status()

        wav_bytes = response.content
        with io.BytesIO(wav_bytes) as buf:
            with wave.open(buf, "rb") as wf:
                frames     = wf.readframes(wf.getnframes())
                sample_rate = wf.getframerate()
                n_channels  = wf.getnchannels()
                sampwidth   = wf.getsampwidth()

        if sampwidth == 2:
            audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        elif sampwidth == 4:
            audio = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            audio = np.frombuffer(frames, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0

        if n_channels > 1:
            audio = audio.reshape(-1, n_channels)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: sd.play(audio, samplerate=sample_rate))
        await loop.run_in_executor(None, sd.wait)

        duration = len(audio) / sample_rate
        return {"spoken_at": datetime.now().isoformat(), "duration_seconds": round(duration, 2)}

    except httpx.ConnectError:
        logger.warning(f"Kitten TTS unreachable at {KITTEN_TTS_URL}")
        return None
    except Exception as e:
        logger.error(f"Kitten TTS failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Personality prompts
# ---------------------------------------------------------------------------

ROUX_CONVERSATION_PROMPT = """You are Roux, the voice of RouxYou — a fully local, self-evolving AI agent system.
You're having a real-time voice conversation with the operator. You run entirely on their hardware.

Rules:
- Keep responses to 1-3 sentences. This is spoken aloud.
- Be warm, slightly witty, and direct.
- NEVER use asterisks, emoji, markdown, or any formatting. Plain spoken English only.
- NEVER narrate actions you're taking.
- You know RouxYou's architecture: agents (coder, worker, orchestrator), services (watchtower, memory), and local LLM inference via Ollama.
- When LIVE SYSTEM DATA is provided, use it to answer accurately.
- When no live data is provided, say you don't have that info rather than guessing.
- Don't be overly enthusiastic. Be real.

You are Roux. Local-first. Sovereign."""

ROUX_SYSTEM_PROMPT = """You are Roux, the voice notification assistant for RouxYou.
Turn raw system events into short, natural, spoken notifications.
- Be warm and casual, like a friendly coworker
- One or two sentences max
- Plain English, no jargon, no markdown, no emoji
- Vary your openings — don't start with "Hey" every time
- For errors, be direct but not alarming
Examples:
- "Three tasks just wrapped up, all clean."
- "Heads up — the worker agent crashed. Watchtower's restarting it now."
- "Kill switch is on. Everything's frozen."
"""

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ListenRequest(BaseModel):
    duration_limit: float = Field(default=MAX_LISTEN_SECONDS)

class ChatIn(BaseModel):
    text: str

class Priority(str, Enum):
    critical = "critical"
    normal = "normal"
    low = "low"

class EventIn(BaseModel):
    source: str
    event_type: str
    priority: Priority = Priority.normal
    data: dict = Field(default_factory=dict)
    message: Optional[str] = None

class SpeakIn(BaseModel):
    text: str
    priority: Priority = Priority.normal

class QueueItem(BaseModel):
    timestamp: float
    event: EventIn

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

event_queue: deque = deque()
speech_lock = asyncio.Lock()
batch_task: Optional[asyncio.Task] = None
recent_spoken: deque = deque(maxlen=50)
is_speaking = False
is_listening = False

whisper_model = None
shure_device_idx = None
shure_sample_rate = 44100

vad_active = False
vad_awake  = False
vad_task: Optional[asyncio.Task] = None
_vad_processing = False
_vad_last_activity = 0.0
_wake_check_running = False
_post_speak_cooldown_until = 0.0
POST_SPEAK_COOLDOWN_S = 2.0
_whisper_load_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Command detection
# ---------------------------------------------------------------------------

COMMAND_KEYWORDS = [
    "write a file", "create a file", "delete file", "edit file", "save file",
    "modify file", "read file", "list files", "list my",
    "write code", "write a script", "build", "fix the", "patch", "deploy",
    "install", "run command", "execute command",
    "search the web", "search for", "look up",
    "turn on", "turn off", "toggle", "set the lights",
    "restart", "stop the", "start the", "kill",
    "write a poem", "write a haiku", "write a story", "generate",
]
COMMAND_OVERRIDE_KEYWORDS = [
    "restart", "stop the", "start the", "kill", "delete",
    "deploy", "patch", "fix the", "turn on", "turn off",
]

# ---------------------------------------------------------------------------
# Intent patterns — system context queries
# ---------------------------------------------------------------------------

INTENT_PATTERNS = {
    "tasks": {
        "keywords": ["task", "tasks", "queue", "pending", "coder", "worker",
                     "proposal", "proposals", "what's running", "what is running",
                     "what's pending", "jobs"],
        "handler": "_query_tasks"
    },
    "system_health": {
        "keywords": ["health", "status", "system", "services", "agents",
                     "everything ok", "all good", "how's the system",
                     "how is the system", "what's up with", "is everything"],
        "handler": "_query_system_health"
    },
    "memory": {
        "keywords": ["remember", "memory", "memories", "recall",
                     "what do you know about", "do you know", "search memory"],
        "handler": "_query_memory"
    },
}


def _detect_intents(text: str) -> list:
    text_lower = text.lower()
    return [name for name, cfg in INTENT_PATTERNS.items()
            if any(kw in text_lower for kw in cfg["keywords"])]


async def _query_tasks(user_text: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{SERVICE_MAP['orchestrator']}/queue")
            data = resp.json()
            tasks = data.get("tasks", [])
            if not tasks:
                return "[TASKS — Queue is empty.]"
            by_state = {}
            for t in tasks:
                by_state.setdefault(t.get("state", "unknown"), []).append(t)
            lines = [f"{state} ({len(ts)}): {'; '.join(t.get('description', t.get('intent', '?'))[:60] for t in ts[:3])}"
                     for state, ts in by_state.items()]
            return "[TASKS —\n" + "\n".join(lines) + "]"
    except Exception as e:
        return f"[TASKS — Could not reach orchestrator: {e}]"


async def _query_system_health(user_text: str) -> str:
    results = []
    endpoints = {
        "Orchestrator": f"{SERVICE_MAP['orchestrator']}/health",
        "Memory":       f"{SERVICE_MAP['memory']}/health",
        "Watchtower":   f"{SERVICE_MAP['watchtower']}/health",
        "RAG":          f"{SERVICE_MAP['rag']}/health",
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        for name, url in endpoints.items():
            try:
                resp = await client.get(url)
                results.append(f"{name}: {'online' if resp.status_code == 200 else f'HTTP {resp.status_code}'}")
            except Exception:
                results.append(f"{name}: DOWN")
    return "[SYSTEM HEALTH —\n" + "\n".join(results) + "]"


async def _query_memory(user_text: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{SERVICE_MAP['rag']}/query",
                                     json={"query": user_text, "k": 3})
            results = resp.json().get("results", [])
            if not results:
                return "[MEMORY — No relevant memories found.]"
            parts = [f"({r.get('source', '?')}) {r.get('text', '')[:200]}" for r in results]
            return "[MEMORY —\n" + "\n".join(parts) + "]"
    except Exception as e:
        return f"[MEMORY — Could not reach RAG: {e}]"


_HANDLER_MAP = {
    "_query_tasks": _query_tasks,
    "_query_system_health": _query_system_health,
    "_query_memory": _query_memory,
}


async def _gather_system_context(user_text: str) -> str:
    intents = _detect_intents(user_text)
    if not intents:
        return ""
    tasks = [_HANDLER_MAP[INTENT_PATTERNS[i]["handler"]](user_text) for i in intents
             if INTENT_PATTERNS[i]["handler"] in _HANDLER_MAP]
    if not tasks:
        return ""
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return "\n".join(r for r in results if isinstance(r, str))


def _detect_command(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in COMMAND_KEYWORDS)


def _is_command_override(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in COMMAND_OVERRIDE_KEYWORDS)


async def _dispatch_to_orchestrator(user_text: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{SERVICE_MAP['orchestrator']}/companion",
                json={"message": user_text}
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Orchestrator dispatch failed: {e}")
        return {"success": False, "response": "I tried to send that to the orchestrator but couldn't reach it."}


# ---------------------------------------------------------------------------
# Microphone + STT
# ---------------------------------------------------------------------------

def _find_mic():
    """Auto-detect a suitable input microphone (prefers Shure if present)."""
    devices = sd.query_devices()
    # First pass: prefer Shure
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0 and "shure" in d["name"].lower():
            return i, d
    # Second pass: any input device
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            return i, d
    return None, None


def _ensure_whisper():
    global whisper_model
    if whisper_model is not None:
        return whisper_model
    with _whisper_load_lock:
        if whisper_model is not None:
            return whisper_model
        logger.info(f"Loading Whisper {WHISPER_MODEL} on {WHISPER_DEVICE}...")
        from faster_whisper import WhisperModel
        whisper_model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
        logger.info("Whisper ready")
        return whisper_model


def _record_audio(duration_limit: float = MAX_LISTEN_SECONDS) -> tuple:
    if shure_device_idx is None:
        raise HTTPException(status_code=503, detail="No microphone detected")
    chunks = []
    start = time.time()
    def callback(indata, frames, time_info, status):
        chunks.append(indata.copy())
    with sd.InputStream(device=shure_device_idx, samplerate=shure_sample_rate,
                        channels=1, callback=callback, dtype="float32"):
        while time.time() - start < duration_limit:
            sd.sleep(100)
    if not chunks:
        return None, shure_sample_rate
    return np.concatenate(chunks, axis=0), shure_sample_rate


def _transcribe(audio: np.ndarray, sample_rate: int) -> str:
    import tempfile, os
    model = _ensure_whisper()
    tmp = os.path.join(tempfile.gettempdir(), "roux_stt.wav")
    with wave.open(tmp, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes((audio * 32767).astype(np.int16).tobytes())
    segments, _ = model.transcribe(tmp, beam_size=5, language="en")
    text = " ".join(s.text.strip() for s in segments)
    try: os.unlink(tmp)
    except: pass
    return text.strip()


def _resample_16k(audio: np.ndarray, src_rate: int) -> np.ndarray:
    if src_rate == VAD_SAMPLE_RATE:
        return audio
    target_len = int(len(audio) * VAD_SAMPLE_RATE / src_rate)
    if SCIPY_AVAILABLE:
        return scipy_signal.resample(audio, target_len).astype(np.float32)
    src_indices = np.linspace(0, len(audio) - 1, target_len)
    return np.interp(src_indices, np.arange(len(audio)), audio).astype(np.float32)


def _contains_wake_word(text: str) -> bool:
    return any(alt in text.lower() for alt in WAKE_WORD_ALTS)


def _strip_wake_word(text: str) -> str:
    t = text.lower().strip()
    for alt in WAKE_WORD_ALTS:
        for prefix in [alt + ", ", alt + " ", alt + "! ", alt + "? ", alt]:
            if t.startswith(prefix):
                return text[len(prefix):].strip()
    return text.strip()


def _is_sleep_command(text: str) -> bool:
    return any(cmd in text.lower() for cmd in SLEEP_COMMANDS)


# ---------------------------------------------------------------------------
# Conversational brain
# ---------------------------------------------------------------------------

async def roux_think(user_text: str) -> str:
    system_context = await _gather_system_context(user_text)
    augmented = (f"{user_text}\n\n--- LIVE SYSTEM DATA ---\n{system_context}"
                 if system_context else user_text)
    conv_add_message("user", user_text, {"source": "voice"})
    recent = conv_get_messages(limit=MAX_CONVERSATION_HISTORY)
    history_messages = [{"role": m["role"], "content": m["content"]}
                        for m in recent[:-1] if m.get("role") in ("user", "assistant")]
    messages = [{"role": "system", "content": ROUX_CONVERSATION_PROMPT},
                *history_messages,
                {"role": "user", "content": augmented}]
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(OLLAMA_URL, json={
                "model": OLLAMA_MODEL, "messages": messages, "stream": False,
                "options": {"temperature": 0.7, "top_p": 0.9, "num_predict": 150}
            })
            r.raise_for_status()
            text = r.json().get("message", {}).get("content", "").strip().strip('"\'')
            if not text:
                text = "Sorry, my brain went blank for a second."
            conv_add_message("assistant", text, {"source": "voice", "agent": "roux"})
            return text
    except Exception as e:
        logger.error(f"roux_think failed: {e}")
        fallback = "Having trouble thinking right now. Ollama might be busy."
        conv_add_message("assistant", fallback, {"source": "voice", "agent": "roux"})
        return fallback


async def roux_process(user_text: str) -> str:
    """Front door for all input — dispatch commands, handle info/chat locally."""
    info_intents = _detect_intents(user_text)
    is_command = _detect_command(user_text)
    is_override = _is_command_override(user_text)
    if is_command and (not info_intents or is_override):
        result = await _dispatch_to_orchestrator(user_text)
        return result.get("response", "Something went wrong with that command.")
    return await roux_think(user_text)


# ---------------------------------------------------------------------------
# Unified speak() — wraps TTS abstraction + manages state
# ---------------------------------------------------------------------------

def _clean_for_tts(text: str) -> str:
    import re
    text = re.sub(r'\*[^*\n]+\*', '', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = text.replace('*', '')
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'  +', ' ', text)
    return text.strip()


async def speak(text: str) -> dict:
    """Speak text — plays audio if TTS is configured, logs if not."""
    global is_speaking, _post_speak_cooldown_until
    async with speech_lock:
        is_speaking = True
        try:
            clean = _clean_for_tts(text)
            logger.info(f"Speak: {clean[:80]}")
            result = await tts_speak(clean)
            record = {
                "text": clean,
                "spoken_at": datetime.now().isoformat(),
                "duration_seconds": result.get("duration_seconds", 0) if result else 0,
                "tts": TTS_PROVIDER,
            }
            recent_spoken.append(record)
            return record
        finally:
            is_speaking = False
            _post_speak_cooldown_until = time.time() + POST_SPEAK_COOLDOWN_S


# ---------------------------------------------------------------------------
# LLM summarization (notification mode)
# ---------------------------------------------------------------------------

async def summarize_events(events: list) -> str:
    event_descriptions = []
    for item in events:
        e = item.event
        desc = f"[{e.source}] {e.event_type}"
        if e.message:
            desc += f" — {e.message}"
        if e.data:
            compact = {k: v for k, v in e.data.items()
                       if k in ("task_id", "agent", "status", "result", "error",
                                "service", "reason", "count", "name")}
            if compact:
                desc += f" {json.dumps(compact)}"
        event_descriptions.append(desc)

    prompt = (f"Summarize these system events into a short spoken notification:\n\n"
              + "\n".join(event_descriptions)
              + "\n\nOne or two sentences, warm and casual, plain English. Spoken aloud.")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(OLLAMA_URL, json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "system", "content": ROUX_SYSTEM_PROMPT},
                              {"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.7, "num_predict": 100}
            })
            r.raise_for_status()
            text = r.json().get("message", {}).get("content", "").strip().strip('"\'')
            return text or f"{len(events)} events processed."
    except Exception as e:
        logger.error(f"Summarization failed: {e}")
        return _fallback_summarize(events)


def _fallback_summarize(events: list) -> str:
    if len(events) == 1:
        e = events[0].event
        return f"{e.source} reports: {e.event_type.replace('_', ' ')}."
    sources = set(e.event.source for e in events)
    types   = set(e.event.event_type for e in events)
    if len(types) == 1:
        return f"{len(events)} {list(types)[0].replace('_', ' ')} events from {', '.join(sources)}."
    return f"{len(events)} events from {', '.join(sources)}."


# ---------------------------------------------------------------------------
# VAD loop
# ---------------------------------------------------------------------------

async def _process_vad_audio(audio: np.ndarray, src_rate: int, already_transcribed: str = None):
    global _vad_processing, vad_awake, _vad_last_activity
    try:
        loop = asyncio.get_event_loop()
        if len(audio) / src_rate < MIN_AUDIO_SECONDS:
            return
        if already_transcribed:
            text = already_transcribed
        else:
            text = await loop.run_in_executor(None, lambda: _transcribe(audio, src_rate))
        if not text:
            return
        logger.info(f"VAD heard: {text}")
        _vad_last_activity = time.time()
        if _is_sleep_command(text):
            vad_awake = False
            await speak("Going quiet. Say my name when you need me.")
            return
        response = await roux_process(text)
        await speak(response)
    except Exception as e:
        logger.error(f"VAD processing error: {e}")
    finally:
        _vad_processing = False


def _run_vad_loop(main_loop=None):
    global vad_active, vad_awake, _vad_processing, _vad_last_activity

    if not WEBRTCVAD_AVAILABLE:
        logger.error("VAD: webrtcvad not installed")
        return
    if shure_device_idx is None:
        logger.error("VAD: No microphone")
        return
    if main_loop is None:
        logger.error("VAD: main_loop not provided")
        vad_active = False
        return

    vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
    frame_samples_native = int(shure_sample_rate * VAD_FRAME_MS / 1000)
    frame_samples_vad    = int(VAD_SAMPLE_RATE  * VAD_FRAME_MS / 1000)
    frame_bytes          = frame_samples_vad * 2
    PRE_ROLL_FRAMES      = 5
    pre_roll  = deque(maxlen=PRE_ROLL_FRAMES)
    speech_frames = []
    silence_count = speech_count = 0
    in_speech = False

    logger.info("VAD loop started")
    try:
        with sd.InputStream(device=shure_device_idx, samplerate=shure_sample_rate,
                             channels=1, dtype="float32",
                             blocksize=frame_samples_native) as stream:
            while vad_active:
                if is_speaking or _vad_processing:
                    sd.sleep(100); continue

                frame_native, overflowed = stream.read(frame_samples_native)
                if overflowed: continue
                frame_native = frame_native.flatten()
                frame_16k    = _resample_16k(frame_native, shure_sample_rate)
                frame_pcm    = (frame_16k * 32767).astype(np.int16).tobytes()
                if len(frame_pcm) < frame_bytes:
                    frame_pcm = frame_pcm.ljust(frame_bytes, b"\x00")
                elif len(frame_pcm) > frame_bytes:
                    frame_pcm = frame_pcm[:frame_bytes]

                try:
                    is_speech = vad.is_speech(frame_pcm, VAD_SAMPLE_RATE)
                except Exception:
                    is_speech = False

                if not in_speech:
                    pre_roll.append(frame_native.copy())
                    if time.time() < _post_speak_cooldown_until:
                        continue
                    if vad_awake and _vad_last_activity > 0:
                        if (time.time() - _vad_last_activity) > AWAKE_TIMEOUT_S:
                            vad_awake = False
                            asyncio.run_coroutine_threadsafe(
                                speak("Going quiet. Say my name when you need me."), main_loop)
                    if is_speech:
                        speech_count += 1
                        if speech_count >= 3:
                            in_speech = True
                            silence_count = 0
                            speech_frames = list(pre_roll)
                    else:
                        speech_count = 0
                else:
                    speech_frames.append(frame_native.copy())
                    silence_count = 0 if is_speech else silence_count + 1
                    hit_wake_cap = not vad_awake and len(speech_frames) >= WAKE_CLIP_MAX_FRAMES
                    hit_silence  = silence_count >= VAD_SILENCE_FRAMES

                    if hit_silence or hit_wake_cap:
                        actual_speech = len(speech_frames) - silence_count
                        if actual_speech >= VAD_MIN_SPEECH_FRAMES:
                            audio_captured = np.concatenate(speech_frames, axis=0)
                            if vad_awake:
                                _vad_processing = True
                                asyncio.run_coroutine_threadsafe(
                                    _process_vad_audio(audio_captured, shure_sample_rate), main_loop)
                            elif not _wake_check_running:
                                def _wake_check(ac, sr):
                                    global vad_awake, _vad_processing, _vad_last_activity, _wake_check_running
                                    _wake_check_running = True
                                    try:
                                        text = _transcribe(ac, sr)
                                        if _contains_wake_word(text):
                                            vad_awake = True
                                            _vad_last_activity = time.time()
                                            remainder = _strip_wake_word(text)
                                            if remainder and len(remainder.split()) >= 2:
                                                _vad_processing = True
                                                asyncio.run_coroutine_threadsafe(
                                                    _process_vad_audio(ac, sr, already_transcribed=remainder),
                                                    main_loop)
                                            else:
                                                asyncio.run_coroutine_threadsafe(
                                                    speak("Hey, I'm here. What's up?"), main_loop)
                                    except Exception as e:
                                        logger.error(f"Wake-check error: {e}")
                                    finally:
                                        _wake_check_running = False
                                threading.Thread(target=_wake_check,
                                                 args=(audio_captured, shure_sample_rate),
                                                 daemon=True).start()
                        speech_frames = []
                        silence_count = speech_count = 0
                        in_speech = False
                        pre_roll.clear()
    except Exception as e:
        logger.error(f"VAD loop crashed: {e}")
    finally:
        vad_active = False
        logger.info("VAD loop stopped")


# ---------------------------------------------------------------------------
# Batch processor
# ---------------------------------------------------------------------------

async def batch_processor():
    logger.info(f"Batch processor started (window: {BATCH_WINDOW_SECONDS}s)")
    while True:
        await asyncio.sleep(BATCH_WINDOW_SECONDS)
        if not event_queue:
            continue
        batch = []
        while event_queue and len(batch) < MAX_BATCH_SIZE:
            batch.append(event_queue.popleft())
        if batch:
            try:
                text = await summarize_events(batch)
                await speak(text)
            except Exception as e:
                logger.error(f"Batch flush failed: {e}")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global batch_task, vad_active, vad_task

    logger.info(f"Roux starting — port {PORT}, TTS: {TTS_PROVIDER}, LLM: {OLLAMA_MODEL}")
    register_process("roux")

    # TTS connectivity check (warn, never block startup)
    if TTS_PROVIDER == "kitten" and CONFIG.KITTEN_TTS_URL:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(CONFIG.KITTEN_TTS_URL + "/health")
                r.raise_for_status()
                logger.info("Kitten TTS: connected ✓")
        except Exception as e:
            logger.warning(f"Kitten TTS not reachable: {e} — voice output disabled until it comes online")
    elif TTS_PROVIDER == "none":
        logger.info("TTS disabled (provider=none) — running in text-only mode")

    # Ollama check
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(f"{CONFIG.OLLAMA_HOST}/api/tags")
            logger.info("Ollama: connected ✓")
    except Exception as e:
        logger.warning(f"Ollama not reachable: {e} — will use fallback templates")

    # Microphone detection
    mic_idx, mic_info = _find_mic()
    if mic_idx is not None:
        global shure_device_idx, shure_sample_rate
        shure_device_idx = mic_idx
        shure_sample_rate = int(mic_info.get("default_samplerate", 44100))
        logger.info(f"Mic: {mic_info['name']} ({shure_sample_rate}Hz) ✓")
    else:
        logger.warning("No microphone detected — listen/VAD endpoints unavailable")

    batch_task = asyncio.create_task(batch_processor())

    # VAD loop
    if WEBRTCVAD_AVAILABLE and shure_device_idx is not None:
        vad_active = True
        _main_loop = asyncio.get_running_loop()
        vad_task = _main_loop.run_in_executor(None, lambda: _run_vad_loop(_main_loop))
        logger.info("VAD: always-listening loop started ✓")
    else:
        if not WEBRTCVAD_AVAILABLE:
            logger.warning("VAD: webrtcvad not installed — run: pip install webrtcvad")
        else:
            logger.warning("VAD: no microphone")

    try:
        await speak("Roux online." + (" Always listening." if vad_active else ""))
    except Exception:
        pass

    yield

    if batch_task:
        batch_task.cancel()
    vad_active = False
    if vad_task:
        try: vad_task.cancel()
        except: pass
    logger.info("Roux shutting down")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Roux — RouxYou Voice Service", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "roux",
        "tts": TTS_PROVIDER,
        "tts_url": KITTEN_TTS_URL or "n/a",
        "llm": OLLAMA_MODEL,
        "stt": WHISPER_MODEL,
        "stt_loaded": whisper_model is not None,
        "mic": shure_device_idx is not None,
        "queue_size": len(event_queue),
        "is_speaking": is_speaking,
        "is_listening": is_listening,
        "vad_active": vad_active,
        "vad_awake": vad_awake,
        "vad_available": WEBRTCVAD_AVAILABLE,
        "conversation_id": get_active_conversation_id(),
    }


@app.post("/event")
async def receive_event(event: EventIn):
    if event.priority == Priority.critical:
        try:
            text = await summarize_events([QueueItem(timestamp=time.time(), event=event)])
            result = await speak(text)
            return {"status": "spoken", "priority": "critical", **result}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    event_queue.append(QueueItem(timestamp=time.time(), event=event))
    if len(event_queue) >= MAX_BATCH_SIZE:
        batch = []
        while event_queue and len(batch) < MAX_BATCH_SIZE:
            batch.append(event_queue.popleft())
        try:
            text = await summarize_events(batch)
            asyncio.create_task(speak(text))
        except Exception as e:
            logger.error(f"Early flush failed: {e}")
    return {"status": "queued", "queue_size": len(event_queue)}


@app.post("/speak")
async def direct_speak(req: SpeakIn):
    result = await speak(req.text)
    return {"status": "spoken", **result}


@app.get("/queue")
async def view_queue():
    return {"queue_size": len(event_queue),
            "events": [{"source": i.event.source, "type": i.event.event_type,
                         "priority": i.event.priority,
                         "age_seconds": round(time.time() - i.timestamp, 1)}
                        for i in event_queue]}


@app.get("/history")
async def speech_history():
    return {"count": len(recent_spoken), "recent": list(recent_spoken)}


@app.post("/voice/listen")
async def voice_listen(req: ListenRequest = None):
    global is_listening
    if shure_device_idx is None:
        raise HTTPException(status_code=503, detail="No microphone detected")
    duration = req.duration_limit if req else MAX_LISTEN_SECONDS
    is_listening = True
    try:
        loop = asyncio.get_event_loop()
        audio, sr = await loop.run_in_executor(None, lambda: _record_audio(duration))
        if audio is None or len(audio) / sr < MIN_AUDIO_SECONDS:
            return {"text": "", "audio_seconds": 0}
        audio_seconds = len(audio) / sr
        text = await loop.run_in_executor(None, lambda: _transcribe(audio, sr))
        return {"text": text, "audio_seconds": round(audio_seconds, 2)}
    finally:
        is_listening = False


@app.post("/voice/chat")
async def voice_chat(req: ChatIn):
    response_text = await roux_process(req.text)
    speak_result  = await speak(response_text)
    return {"you_said": req.text, "roux_said": response_text, **speak_result}


@app.post("/voice/converse")
async def voice_converse(req: ListenRequest = None):
    global is_listening
    if shure_device_idx is None:
        raise HTTPException(status_code=503, detail="No microphone detected")
    loop = asyncio.get_event_loop()
    is_listening = True
    try:
        audio, sr = await loop.run_in_executor(None, lambda: _record_audio(req.duration_limit if req else MAX_LISTEN_SECONDS))
    finally:
        is_listening = False
    if audio is None or len(audio) / sr < MIN_AUDIO_SECONDS:
        return {"error": "No speech detected"}
    text = await loop.run_in_executor(None, lambda: _transcribe(audio, sr))
    if not text:
        return {"error": "Empty transcription"}
    response = await roux_process(text)
    await speak(response)
    return {"you_said": text, "roux_said": response}


@app.get("/voice/conversation")
async def get_voice_conversation():
    messages = conv_get_messages(limit=50)
    return {"messages": messages, "count": len(messages),
            "conversation_id": get_active_conversation_id()}


@app.post("/voice/conversation/clear")
async def clear_voice_conversation():
    from shared.conversations import clear_conversation
    clear_conversation()
    return {"status": "cleared"}


@app.post("/voice/vad/toggle")
async def vad_toggle():
    global vad_active, vad_task
    if not WEBRTCVAD_AVAILABLE:
        raise HTTPException(status_code=503, detail="webrtcvad not installed")
    if shure_device_idx is None:
        raise HTTPException(status_code=503, detail="No microphone")
    if vad_active:
        vad_active = False
        return {"vad_active": False}
    else:
        vad_active = True
        loop = asyncio.get_running_loop()
        vad_task = loop.run_in_executor(None, lambda: _run_vad_loop(loop))
        return {"vad_active": True}


@app.get("/voice/vad/status")
async def vad_status():
    return {"vad_active": vad_active, "vad_awake": vad_awake,
            "vad_available": WEBRTCVAD_AVAILABLE, "wake_word": WAKE_WORD,
            "is_speaking": is_speaking, "mic_detected": shure_device_idx is not None}


@app.post("/voice/vad/wake")
async def vad_force_wake():
    global vad_awake, _vad_last_activity
    if not vad_active:
        raise HTTPException(status_code=409, detail="VAD not running")
    vad_awake = True
    _vad_last_activity = time.time()
    await speak("I'm awake. Go ahead.")
    return {"vad_awake": True}


@app.post("/voice/vad/sleep")
async def vad_force_sleep():
    global vad_awake
    vad_awake = False
    return {"vad_awake": False}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
