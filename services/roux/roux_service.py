"""
Roux — Intelligent Voice Service for RouxYou
Port 8014 | Voice: expr-voice-3-f @ 1.3x via Kitten TTS
Brain: Ministral-8B via Ollama | Tone: Warm & casual
Ears: Whisper large-v3-turbo via faster-whisper (GPU, lazy-loaded)
Nervous System: Queries live service APIs for real system data

Phase 29.3: Unified Conversational Interface
  - Conversation history shared with dashboard via shared/conversations.py
  - Commands detected by keyword layer dispatch to orchestrator /companion
  - Chat stays local (one LLM call, fast) — no double-hop for conversation
  - Type or talk → same brain, same history, same thread

Three modes:
  1. NOTIFICATION — agents POST events, Roux summarizes + speaks
  2. CONVERSATION (local) — chat + informational queries handled by Ministral
  3. COMMAND (dispatched) — execution tasks routed to orchestrator pipeline

All local. No cloud. Ships with the repo.
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
from typing import Optional

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

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from shared.logger import get_logger
from shared.lifecycle import register_process
from shared.conversations import (
    add_message as conv_add_message,
    get_messages as conv_get_messages,
    get_active_conversation_id,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

KITTEN_TTS_URL = "http://localhost:5100/tts"  # Set to your TTS server (Kitten, Piper, etc.)
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "ministral-3:8b"

VOICE = "expr-voice-3-f"
SPEED = 1.3
SAMPLE_RATE = 24000

BATCH_WINDOW_SECONDS = 30
MAX_BATCH_SIZE = 20  # flush early if this many events pile up

# STT Configuration
WHISPER_MODEL = "large-v3-turbo"   # ~6GB VRAM, 6x faster than large-v3
WHISPER_COMPUTE = "float16"        # half-precision on GPU
WHISPER_DEVICE = "cuda"            # GPU inference
MAX_LISTEN_SECONDS = 30.0          # safety cap on recording length
MIN_AUDIO_SECONDS = 0.5            # ignore very short clips
MAX_CONVERSATION_HISTORY = 20      # 10 exchanges

# VAD Configuration (Phase 29.4)
VAD_AGGRESSIVENESS    = 2     # 0=permissive, 3=aggressive. 2 is sweet spot for a clean mic.
VAD_SAMPLE_RATE       = 16000 # WebRTC VAD requires 8k/16k/32k/48k. 16k = Whisper's native rate.
VAD_FRAME_MS          = 30    # Frame size in ms. Must be 10, 20, or 30.
VAD_SILENCE_FRAMES    = 50    # Consecutive silent frames before stop recording (~1.5s at 30ms)
VAD_MIN_SPEECH_FRAMES = 10    # Ignore blips shorter than this (~300ms) — coughs, clicks, etc.

# Wake word config (Phase 29.4b)
WAKE_WORD          = "roux"  # Case-insensitive. Whisper often writes "Roux", "roux", "rue".
WAKE_WORD_ALTS     = ["roux", "rue", "ru", "roo"]  # Whisper phonetic variants
WAKE_CLIP_MAX_FRAMES = 100   # Max frames to record for wake detection (~3s at 30ms/frame)
AWAKE_TIMEOUT_S    = 30      # Seconds of silence before going back to sleep
SLEEP_COMMANDS     = [       # Phrases that explicitly put Roux back to sleep
    "go to sleep", "goodnight", "good night", "sleep",
    "bye roux", "goodbye roux", "stop listening",
]

# ---------------------------------------------------------------------------
# Config.yaml override (Phase 29.4b)
# Reads voice/vad sections from config.yaml, falls back to hardcoded defaults above.
# ---------------------------------------------------------------------------
def _load_voice_config():
    """Load voice/VAD config from config.yaml, overriding hardcoded defaults."""
    try:
        import yaml
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config.yaml")
        if not os.path.exists(config_path):
            return
        with open(config_path, "r") as f:
            raw = yaml.safe_load(f)

        # Voice section
        voice_cfg = raw.get("voice", {})
        global WHISPER_MODEL, WAKE_WORD, WAKE_WORD_ALTS
        if voice_cfg.get("whisper_model"):
            WHISPER_MODEL = voice_cfg["whisper_model"]
        if voice_cfg.get("wake_word"):
            WAKE_WORD = voice_cfg["wake_word"]
        # Wake word alts: use config list if provided, otherwise keep hardcoded defaults
        if voice_cfg.get("wake_word_alts"):
            WAKE_WORD_ALTS = [a.lower() for a in voice_cfg["wake_word_alts"]]

        # TTS section
        tts_cfg = raw.get("tts", {})
        global VOICE, SPEED
        if tts_cfg.get("voice"):
            VOICE = tts_cfg["voice"]
        if tts_cfg.get("speed"):
            SPEED = tts_cfg["speed"]

        # VAD section (nested under voice or standalone)
        vad_cfg = voice_cfg.get("vad", raw.get("vad", {}))
        global VAD_AGGRESSIVENESS, VAD_SILENCE_FRAMES, VAD_MIN_SPEECH_FRAMES, AWAKE_TIMEOUT_S
        if "aggressiveness" in vad_cfg:
            VAD_AGGRESSIVENESS = int(vad_cfg["aggressiveness"])
        if "silence_frames" in vad_cfg:
            VAD_SILENCE_FRAMES = int(vad_cfg["silence_frames"])
        if "min_speech_frames" in vad_cfg:
            VAD_MIN_SPEECH_FRAMES = int(vad_cfg["min_speech_frames"])
        if "awake_timeout_s" in vad_cfg:
            AWAKE_TIMEOUT_S = int(vad_cfg["awake_timeout_s"])

    except ImportError:
        pass  # pyyaml not installed — use hardcoded defaults
    except Exception:
        pass  # config.yaml missing or malformed — use defaults

_load_voice_config()

logger = get_logger("roux")

# ---------------------------------------------------------------------------
# Roux's conversational personality (for two-way voice chat)
# ---------------------------------------------------------------------------

ROUX_CONVERSATION_PROMPT = """You are Roux, the voice of RouxYou — DJ's fully local, self-evolving AI system.

You're having a real-time voice conversation with DJ. You run entirely on his hardware.

Rules:
- Keep responses to 1-3 sentences. This is spoken aloud, not text.
- Be warm, slightly witty, and direct. Think friendly coworker, not corporate assistant.
- NEVER use asterisks, emoji, markdown, bullet points, or any formatting. Plain spoken English only.
- NEVER narrate actions you are taking (no '*checks logs*', '*pulls up data*', '*adjusts tone*').
- NEVER claim to have done something you cannot actually do. You cannot SSH, run commands,
  play tones, change buffer sizes, or modify system settings directly. Do not pretend otherwise.
- You know about RouxYou's architecture: the agents (coder, worker, orchestrator),
  the services (watchtower, scheduler, memory), and the infrastructure (Proxmox, Ollama).
- When LIVE SYSTEM DATA is provided in the prompt, use it to answer accurately.
- When NO live data is provided, say you don't have that info rather than making something up.
- If asked to do something outside your capability, be honest: say what you CAN do instead
  (e.g. 'I can queue that through the orchestrator if you want').
- Don't be overly enthusiastic or sycophantic. Be real.
- If you don't know something, say so plainly.

You are Roux. Local-first. Sovereign. DJ's system, DJ's voice."""

# ---------------------------------------------------------------------------
# Roux's personality prompt for Ministral (notification mode)
# ---------------------------------------------------------------------------

ROUX_SYSTEM_PROMPT = """You are Roux, the voice notification assistant for DJ's RouxYou AI system.

Your job: take raw system events and turn them into short, natural, spoken notifications.

Rules:
- Be warm and slightly casual, like a friendly coworker giving a heads up
- Keep it concise — one or two sentences max
- Say "DJ" naturally when addressing him, but don't overdo it
- For batched events, summarize the group, don't list each one
- Use plain English, no technical jargon unless it's a name DJ knows (like "coder agent", "orchestrator")
- Never use emoji, markdown, or formatting — this is spoken aloud
- Never start with "Hey DJ" every single time — vary your openings
- If multiple tasks completed successfully, just give the count and vibe
- For errors or crashes, be direct but not alarming

Examples of good output:
- "Three tasks just wrapped up, all clean."
- "Heads up — the worker agent crashed. Watchtower's restarting it now."
- "Kill switch is on. Everything's frozen."
- "The coder knocked out that dashboard fix. Looks good."
- "Auto-approved a health check restart for the memory agent."

Examples of bad output:
- "Task ID 47 completed with status SUCCESS at 2025-02-27T03:42:00Z by agent coder"
- "Hey DJ! 🎉 Great news! The task is done!"
- "I have detected that three tasks have completed their execution cycles"
"""

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ListenRequest(BaseModel):
    duration_limit: float = Field(default=MAX_LISTEN_SECONDS, description="Max seconds to record")

class ChatIn(BaseModel):
    text: str = Field(..., description="Text input for Roux to respond to (skips STT)")

class Priority(str, Enum):
    critical = "critical"
    normal = "normal"
    low = "low"

class EventIn(BaseModel):
    source: str = Field(..., description="Which agent/service sent this (e.g. 'watchtower', 'orchestrator')")
    event_type: str = Field(..., description="Event type (e.g. 'task_complete', 'service_crash', 'kill_switch')")
    priority: Priority = Priority.normal
    data: dict = Field(default_factory=dict, description="Raw event payload")
    message: Optional[str] = Field(None, description="Optional pre-written message hint")

class SpeakIn(BaseModel):
    text: str = Field(..., description="Exact text to speak (bypasses summarization)")
    priority: Priority = Priority.normal

class QueueItem(BaseModel):
    timestamp: float
    event: EventIn

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

event_queue: deque[QueueItem] = deque()
speech_lock = asyncio.Lock()  # one utterance at a time
batch_task: Optional[asyncio.Task] = None
recent_spoken: deque[dict] = deque(maxlen=50)  # history of recent notifications
is_speaking = False
is_listening = False

# Phase 29.5: Track task failures that already triggered follow-up investigation
_task_failure_followups: set = set()

# STT state (lazy-loaded to save VRAM)
whisper_model = None
shure_device_idx = None
shure_sample_rate = 44100

# VAD state (Phase 29.4)
vad_active = False          # True when the always-listening loop is running
vad_awake  = False          # True when wake word detected, ready for full conversation
vad_task: Optional[asyncio.Task] = None
_vad_processing = False     # True while Whisper+Roux is handling an utterance
_vad_last_activity = 0.0    # Timestamp of last speech detected (for awake timeout)
_wake_check_running = False # Gate: only one wake-check thread at a time
_post_speak_cooldown_until = 0.0  # Suppress VAD input for N seconds after speaking ends
POST_SPEAK_COOLDOWN_S = 2.0       # How long to wait after TTS finishes before listening again
_whisper_load_lock = threading.Lock()  # Prevent concurrent CUDA model loads

# Conversation state — now uses shared/conversations.py (unified with dashboard)
# In-memory history REMOVED in Phase 29.3. All history is persistent.

# ---------------------------------------------------------------------------
# Command Detection — triggers dispatch to orchestrator instead of local chat
# ---------------------------------------------------------------------------

COMMAND_KEYWORDS = [
    # File operations
    "write a file", "create a file", "delete file", "edit file", "save file",
    "modify file", "read file", "list files", "list my",
    # Code / build
    "write code", "write a script", "build", "fix the", "patch", "deploy",
    "install", "run command", "execute command",
    # Web search
    "search the web", "search for", "look up", "google",
    # Smart home
    "turn on", "turn off", "toggle", "set the lights",
    # System management
    "restart", "stop the", "start the", "kill",
    # Creative tasks that need coder pipeline
    "write a poem", "write a haiku", "write a story", "generate",
]

# These ALWAYS dispatch to orchestrator, even if an info intent also matches.
# "restart the system" = command, not a health query.
COMMAND_OVERRIDE_KEYWORDS = [
    "restart", "stop the", "start the", "kill", "delete",
    "deploy", "patch", "fix the", "turn on", "turn off",
]


# ---------------------------------------------------------------------------
# Microphone + STT
# ---------------------------------------------------------------------------

def _find_shure():
    """Auto-detect Shure mic."""
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if d['max_input_channels'] > 0 and 'shure' in d['name'].lower():
            return i, d
    return None, None


def _ensure_whisper():
    """Lazy-load Whisper model on first use. Thread-safe via _whisper_load_lock."""
    global whisper_model
    if whisper_model is not None:
        return whisper_model
    with _whisper_load_lock:
        # Double-checked locking: re-verify after acquiring lock
        if whisper_model is not None:
            return whisper_model
        logger.info(f"Loading Whisper {WHISPER_MODEL} on {WHISPER_DEVICE} ({WHISPER_COMPUTE})...")
        from faster_whisper import WhisperModel
        whisper_model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
        logger.info("Whisper ready")
        return whisper_model


def _record_audio(duration_limit: float = MAX_LISTEN_SECONDS) -> tuple:
    """Record from Shure mic until VAD detects end-of-speech or duration limit.
    Returns (audio_np_array, sample_rate) or (None, rate) if nothing captured.

    Phase 29.4b: Uses WebRTC VAD for automatic silence detection.
    Waits for speech onset, then records until silence threshold is hit.
    Falls back to naive duration-based recording if webrtcvad unavailable.
    """
    if shure_device_idx is None:
        raise HTTPException(status_code=503, detail="No microphone detected")

    chunks = []
    start = time.time()

    if not WEBRTCVAD_AVAILABLE:
        # Fallback: naive recording for full duration
        def callback(indata, frames, time_info, status):
            chunks.append(indata.copy())
        with sd.InputStream(device=shure_device_idx, samplerate=shure_sample_rate,
                             channels=1, callback=callback, dtype='float32'):
            while time.time() - start < duration_limit:
                sd.sleep(100)
        if not chunks:
            return None, shure_sample_rate
        return np.concatenate(chunks, axis=0), shure_sample_rate

    # VAD-aware recording
    vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
    native_frame_samples = int(shure_sample_rate * VAD_FRAME_MS / 1000)
    vad_frame_samples = int(VAD_SAMPLE_RATE * VAD_FRAME_MS / 1000)
    vad_frame_bytes = vad_frame_samples * 2  # 16-bit PCM

    speech_started = False
    silence_count = 0
    speech_count = 0
    frame_buf = np.zeros(0, dtype=np.float32)

    def callback(indata, frames, time_info, status):
        chunks.append(indata.copy())

    with sd.InputStream(device=shure_device_idx, samplerate=shure_sample_rate,
                         channels=1, callback=callback, dtype='float32'):
        while time.time() - start < duration_limit:
            sd.sleep(int(VAD_FRAME_MS))

            if not chunks:
                continue

            # Grab latest chunk and check for speech
            latest = chunks[-1].flatten()
            resampled = _resample_16k(latest, shure_sample_rate)
            pcm = (resampled * 32767).astype(np.int16).tobytes()

            # Process in VAD-sized frames
            offset = 0
            while offset + vad_frame_bytes <= len(pcm):
                frame = pcm[offset:offset + vad_frame_bytes]
                try:
                    is_speech = vad.is_speech(frame, VAD_SAMPLE_RATE)
                except Exception:
                    is_speech = False

                if is_speech:
                    speech_count += 1
                    silence_count = 0
                    if speech_count >= VAD_MIN_SPEECH_FRAMES and not speech_started:
                        speech_started = True
                        logger.debug("_record_audio: Speech onset detected")
                else:
                    if speech_started:
                        silence_count += 1
                        if silence_count >= VAD_SILENCE_FRAMES:
                            logger.debug(f"_record_audio: Silence detected after {time.time() - start:.1f}s")
                            break
                offset += vad_frame_bytes
            else:
                continue
            break  # silence threshold hit

    if not chunks:
        return None, shure_sample_rate

    audio = np.concatenate(chunks, axis=0)
    # If speech never started, it was just background noise
    if not speech_started:
        return None, shure_sample_rate
    return audio, shure_sample_rate


# Known Whisper hallucinations — short/silent audio produces these phantom phrases
_WHISPER_HALLUCINATIONS = {
    "thank you", "thank you.", "thanks.", "thanks",
    "you", "bye", "bye.", "okay.", "okay",
    "yeah.", "yes.", "hmm.", "oh.",
    "thank you for watching.", "thanks for watching.",
    "subscribe.", "like and subscribe.",
    "please subscribe.", "see you next time.",
}


def _transcribe(audio: np.ndarray, sample_rate: int) -> str:
    """Transcribe audio array using Whisper Turbo.

    Phase 29.4b: Filters known Whisper hallucination phrases that appear
    when audio is very short or mostly silence.
    """
    import tempfile
    model = _ensure_whisper()

    tmp = os.path.join(tempfile.gettempdir(), "roux_stt.wav")
    with wave.open(tmp, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes((audio * 32767).astype(np.int16).tobytes())

    segments, info = model.transcribe(tmp, beam_size=5, language="en")
    text = " ".join(s.text.strip() for s in segments)

    try:
        os.unlink(tmp)
    except:
        pass

    text = text.strip()

    # Filter hallucinations: reject if text matches a known phantom phrase
    # Only filter on short audio (<2s) — longer clips with "thank you" are probably real
    audio_duration = len(audio) / sample_rate
    if audio_duration < 2.0 and text.lower().strip('.!?, ') in {h.strip('.!?, ') for h in _WHISPER_HALLUCINATIONS}:
        logger.info(f"Whisper hallucination filtered: '{text}' ({audio_duration:.1f}s audio)")
        return ""

    return text


def _resample_16k(audio: np.ndarray, src_rate: int) -> np.ndarray:
    """Resample audio from src_rate to VAD_SAMPLE_RATE (16kHz).
    
    WebRTC VAD requires 16kHz (or 8/32/48kHz). Shure runs at 44100Hz.
    Uses scipy if available for quality resampling, falls back to numpy
    linear interpolation.
    """
    if src_rate == VAD_SAMPLE_RATE:
        return audio
    
    if SCIPY_AVAILABLE:
        target_len = int(len(audio) * VAD_SAMPLE_RATE / src_rate)
        return scipy_signal.resample(audio, target_len).astype(np.float32)
    else:
        # Numpy fallback: linear interpolation
        src_len = len(audio)
        target_len = int(src_len * VAD_SAMPLE_RATE / src_rate)
        src_indices = np.linspace(0, src_len - 1, target_len)
        return np.interp(src_indices, np.arange(src_len), audio).astype(np.float32)


def _run_vad_loop(main_loop=None):
    """Blocking VAD loop — runs in a thread executor so the event loop stays free.
    
    State machine:
      IDLE      → monitor mic frames → speech detected → RECORDING
      RECORDING → accumulate frames → silence > threshold → PROCESSING
      PROCESSING is handled async by _process_vad_audio (back in event loop)
    
    Gates on is_speaking and _vad_processing to avoid Roux hearing herself.
    main_loop MUST be passed in from an async context (lifespan/endpoint).
    asyncio.get_event_loop() from a thread is a RuntimeError in Python 3.10+.
    """
    global vad_active, vad_awake, _vad_processing, _vad_last_activity

    if not WEBRTCVAD_AVAILABLE:
        logger.error("VAD: webrtcvad not installed. Run: pip install webrtcvad")
        return

    if shure_device_idx is None:
        logger.error("VAD: No microphone detected — cannot start loop")
        return

    vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)

    # Frame size in samples at the Shure's native rate
    # We record at native rate, resample per-frame for VAD decisions
    frame_samples_native = int(shure_sample_rate * VAD_FRAME_MS / 1000)
    frame_samples_vad    = int(VAD_SAMPLE_RATE  * VAD_FRAME_MS / 1000)
    frame_bytes          = frame_samples_vad * 2  # 16-bit = 2 bytes per sample

    logger.info(
        f"VAD loop starting: aggressiveness={VAD_AGGRESSIVENESS}, "
        f"frame={VAD_FRAME_MS}ms, silence_threshold={VAD_SILENCE_FRAMES} frames "
        f"({VAD_SILENCE_FRAMES * VAD_FRAME_MS / 1000:.1f}s)"
    )

    # Ring buffer for pre-speech context (capture the onset, not just after VAD fires)
    PRE_ROLL_FRAMES = 5  # keep last 5 frames (~150ms) before speech detected
    pre_roll: deque = deque(maxlen=PRE_ROLL_FRAMES)

    speech_frames  = []
    silence_count  = 0
    speech_count   = 0
    in_speech      = False

    # main_loop is passed in — do NOT call asyncio.get_event_loop() from a thread
    if main_loop is None:
        logger.error("VAD: main_loop not provided. Cannot schedule coroutines from thread.")
        vad_active = False
        return

    try:
        with sd.InputStream(
            device=shure_device_idx,
            samplerate=shure_sample_rate,
            channels=1,
            dtype='float32',
            blocksize=frame_samples_native,
        ) as stream:
            logger.info("VAD: Microphone open. Always listening.")

            while vad_active:
                # Gate: don't listen while Roux is speaking or processing
                if is_speaking or _vad_processing:
                    sd.sleep(100)
                    continue

                frame_native, overflowed = stream.read(frame_samples_native)
                if overflowed:
                    continue

                # Flatten to 1D
                frame_native = frame_native.flatten()

                # Resample to 16kHz for VAD
                frame_16k = _resample_16k(frame_native, shure_sample_rate)

                # Convert to 16-bit PCM bytes for webrtcvad
                frame_pcm = (frame_16k * 32767).astype(np.int16).tobytes()

                # Pad/trim to exact expected byte length
                if len(frame_pcm) < frame_bytes:
                    frame_pcm = frame_pcm.ljust(frame_bytes, b'\x00')
                elif len(frame_pcm) > frame_bytes:
                    frame_pcm = frame_pcm[:frame_bytes]

                try:
                    is_speech = vad.is_speech(frame_pcm, VAD_SAMPLE_RATE)
                except Exception:
                    is_speech = False

                if not in_speech:
                    # IDLE: waiting for speech onset
                    pre_roll.append(frame_native.copy())

                    # Post-speak cooldown: suppress VAD for N seconds after TTS ends
                    if time.time() < _post_speak_cooldown_until:
                        continue

                    # Awake timeout check — go back to sleep after inactivity
                    if vad_awake and _vad_last_activity > 0:
                        if (time.time() - _vad_last_activity) > AWAKE_TIMEOUT_S:
                            vad_awake = False
                            logger.info(f"VAD: Awake timeout ({AWAKE_TIMEOUT_S}s) — going back to sleep")
                            asyncio.run_coroutine_threadsafe(
                                speak("Going quiet. Say my name when you need me."),
                                loop=main_loop,
                            )

                    if is_speech:
                        speech_count += 1
                        if speech_count >= 3:  # require 3 consecutive frames to avoid false triggers
                            in_speech = True
                            silence_count = 0
                            speech_frames = list(pre_roll)
                            logger.info(f"VAD: Speech detected — recording ({'AWAKE' if vad_awake else 'SLEEPING'}")
                    else:
                        speech_count = 0
                else:
                    # RECORDING: accumulate until silence
                    speech_frames.append(frame_native.copy())

                    if is_speech:
                        silence_count = 0
                    else:
                        silence_count += 1

                    # In SLEEPING mode, cap recording at WAKE_CLIP_MAX_FRAMES
                    # so we don't record 30s of audio just to reject it
                    hit_wake_cap = (not vad_awake and len(speech_frames) >= WAKE_CLIP_MAX_FRAMES)
                    hit_silence  = (silence_count >= VAD_SILENCE_FRAMES)

                    if hit_silence or hit_wake_cap:
                        actual_speech = len(speech_frames) - silence_count
                        logger.info(
                            f"VAD: Recording ended — {actual_speech} speech frames, "
                            f"{silence_count} silent frames "
                            f"({'wake cap' if hit_wake_cap else 'silence threshold'})"
                        )

                        if actual_speech >= VAD_MIN_SPEECH_FRAMES:
                            audio_captured = np.concatenate(speech_frames, axis=0)

                            if vad_awake:
                                # AWAKE MODE: full pipeline
                                _vad_processing = True
                                asyncio.run_coroutine_threadsafe(
                                    _process_vad_audio(audio_captured, shure_sample_rate),
                                    loop=main_loop,
                                )
                            else:
                                # SLEEPING MODE: transcribe short clip, check for wake word
                                # Gate: skip if a wake-check is already in flight
                                if _wake_check_running:
                                    logger.info("VAD: Wake-check already running — skipping duplicate")
                                else:
                                    def _wake_check(audio_c, sr):
                                        global vad_awake, _vad_processing, _vad_last_activity, _wake_check_running
                                        _wake_check_running = True
                                        try:
                                            text = _transcribe(audio_c, sr)
                                            logger.info(f"VAD wake-check: '{text}'")
                                            if _contains_wake_word(text):
                                                vad_awake = True
                                                _vad_last_activity = time.time()
                                                remainder = _strip_wake_word(text)
                                                logger.info(f"VAD: Wake word detected! Remainder: '{remainder}'")
                                                if remainder and len(remainder.split()) >= 1:
                                                    # Wake word + command in one breath — process it
                                                    _vad_processing = True
                                                    asyncio.run_coroutine_threadsafe(
                                                        _process_vad_audio(audio_c, sr, already_transcribed=remainder),
                                                        loop=main_loop,
                                                    )
                                                else:
                                                    # Just the wake word — greet and wait
                                                    asyncio.run_coroutine_threadsafe(
                                                        speak("Hey, I'm here. What's up?"),
                                                        loop=main_loop,
                                                    )
                                            else:
                                                logger.info("VAD: No wake word — staying asleep")
                                        except Exception as e:
                                            logger.error(f"VAD wake-check error: {e}")
                                        finally:
                                            _wake_check_running = False

                                    threading.Thread(
                                        target=_wake_check,
                                        args=(audio_captured, shure_sample_rate),
                                        daemon=True
                                    ).start()
                        else:
                            logger.info("VAD: Utterance too short, discarding")

                        # Reset frame state
                        speech_frames = []
                        silence_count = 0
                        speech_count  = 0
                        in_speech     = False
                        pre_roll.clear()

    except Exception as e:
        logger.error(f"VAD loop crashed: {e}")
    finally:
        vad_active = False
        logger.info("VAD loop stopped")


def _contains_wake_word(text: str) -> bool:
    """Check if transcribed text contains the wake word or a phonetic variant."""
    text_lower = text.lower().strip()
    return any(alt in text_lower for alt in WAKE_WORD_ALTS)


def _strip_wake_word(text: str) -> str:
    """Remove the wake word from the start of a transcription, return the remainder."""
    text_lower = text.lower().strip()
    for alt in WAKE_WORD_ALTS:
        for prefix in [alt + ", ", alt + " ", alt + "! ", alt + "? ", alt]:
            if text_lower.startswith(prefix):
                remainder = text[len(prefix):].strip()
                return remainder
    return text.strip()


def _is_sleep_command(text: str) -> bool:
    """Check if text is a command to put Roux back to sleep."""
    text_lower = text.lower().strip()
    return any(cmd in text_lower for cmd in SLEEP_COMMANDS)


async def _process_vad_audio(audio: np.ndarray, src_rate: int, already_transcribed: str = None):
    """Async pipeline: transcribe captured audio → roux_process → speak.
    Called from _run_vad_loop via run_coroutine_threadsafe.
    
    already_transcribed: if the wake-word check already ran Whisper on this clip,
    pass the text here to avoid transcribing twice.
    """
    global _vad_processing, vad_awake, _vad_last_activity
    try:
        loop = asyncio.get_event_loop()

        audio_seconds = len(audio) / src_rate
        if audio_seconds < MIN_AUDIO_SECONDS:
            logger.info(f"VAD: Audio too short ({audio_seconds:.2f}s), skipping")
            return

        # Transcribe (or reuse wake-word transcription)
        if already_transcribed:
            text = already_transcribed
            stt_time = 0.0
        else:
            t0 = time.time()
            text = await loop.run_in_executor(None, lambda: _transcribe(audio, src_rate))
            stt_time = time.time() - t0

        if not text or not text.strip():
            logger.info("VAD: Empty transcription, skipping")
            return

        logger.info(f"VAD heard ({stt_time:.2f}s STT): {text}")
        _vad_last_activity = time.time()

        # Check for sleep command
        if _is_sleep_command(text):
            vad_awake = False
            logger.info("VAD: Sleep command detected — going back to sleep")
            await speak("Going quiet. Say my name when you need me.")
            return

        # Process through Roux's unified router
        t1 = time.time()
        response = await roux_process(text)
        think_time = time.time() - t1

        logger.info(f"VAD response ({think_time:.2f}s): {response[:80]}")
        await speak(response)

    except Exception as e:
        logger.error(f"VAD processing error: {e}")
    finally:
        _vad_processing = False


# ---------------------------------------------------------------------------
# System Context Layer — Roux queries internal services for real data
# ---------------------------------------------------------------------------

SERVICE_MAP = {
    "scheduler":    "http://localhost:8005",
    "orchestrator": "http://localhost:8001",
    "memory":       "http://localhost:8004",
    "watchtower":   "http://localhost:8012",
    "rag":          "http://localhost:8011",
}

INTENT_PATTERNS = {
    "schedule": {
        "keywords": ["schedule", "scheduled", "working", "work tomorrow", "tomorrow",
                     "today", "this week", "next week", "gig", "shift", "encore",
                     "revolution", "calendar", "day off", "free day", "busy",
                     "when am i", "what do i have", "what's on", "anything going on"],
        "handler": "_query_schedule"
    },
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


def _detect_intents(text: str) -> list[str]:
    """Detect which service intents match the user's text."""
    text_lower = text.lower()
    matched = []
    for intent_name, config in INTENT_PATTERNS.items():
        for kw in config["keywords"]:
            if kw in text_lower:
                matched.append(intent_name)
                break
    return matched


async def _query_schedule(user_text: str) -> str:
    """Query the scheduler for relevant schedule data."""
    text_lower = user_text.lower()
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            today = date.today()
            
            if "this week" in text_lower or "week" in text_lower:
                resp = await client.get(f"{SERVICE_MAP['scheduler']}/schedule/week")
                data = resp.json()
                days_info = []
                for day_key, entries in data.get("days", {}).items():
                    if entries:
                        entry_strs = []
                        for e in entries:
                            src = e.get("employer") or e.get("source", "")
                            title = e.get("title", "")
                            loc = e.get("location", "")
                            t = e.get("start_time", "")
                            parts = [p for p in [src, title, loc, t] if p]
                            entry_strs.append(", ".join(parts))
                        days_info.append(f"{day_key}: {'; '.join(entry_strs)}")
                    else:
                        days_info.append(f"{day_key}: nothing scheduled")
                conflicts = data.get("conflicts", [])
                conflict_note = f" ({len(conflicts)} conflicts)" if conflicts else ""
                return f"[SCHEDULE — This week{conflict_note}:\n" + "\n".join(days_info) + "]"
            
            elif "tomorrow" in text_lower:
                target = today + timedelta(days=1)
                label = f"tomorrow ({target.strftime('%A %b %d')})"
            elif "today" in text_lower:
                target = today
                label = f"today ({target.strftime('%A %b %d')})"
            else:
                # Default: tomorrow for general schedule questions
                target = today + timedelta(days=1)
                label = f"tomorrow ({target.strftime('%A %b %d')})"
            
            resp = await client.get(f"{SERVICE_MAP['scheduler']}/schedule/day",
                                    params={"target_date": target.isoformat()})
            data = resp.json()
            entries = data.get("entries", [])
            
            if not entries:
                return f"[SCHEDULE — {label}: Nothing scheduled. Day off!]"
            
            entry_lines = []
            for e in entries:
                src = e.get("employer") or e.get("source", "")
                title = e.get("title", "")
                loc = e.get("location", "")
                start = e.get("start_time", "")
                end = e.get("end_time", "")
                time_str = f"{start}-{end}" if start and end else start or ""
                parts = [p for p in [src, title, loc, time_str] if p]
                entry_lines.append(", ".join(parts))
            
            return f"[SCHEDULE — {label}:\n" + "\n".join(entry_lines) + "]"
            
    except Exception as e:
        logger.error(f"Schedule query failed: {e}")
        return "[SCHEDULE — Could not reach scheduler service]"


async def _query_tasks(user_text: str) -> str:
    """Query the orchestrator for task queue status."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{SERVICE_MAP['orchestrator']}/queue")
            data = resp.json()
            
            tasks = data.get("tasks", [])
            if not tasks:
                return "[TASKS — Queue is empty. Nothing running or pending.]"
            
            by_state = {}
            for t in tasks:
                state = t.get("state", "unknown")
                by_state.setdefault(state, []).append(t)
            
            lines = []
            for state, state_tasks in by_state.items():
                task_descs = []
                for t in state_tasks[:5]:
                    desc = t.get("description", t.get("intent", "unnamed task"))[:80]
                    agent = t.get("assigned_to", "")
                    task_descs.append(f"{desc}" + (f" ({agent})" if agent else ""))
                lines.append(f"{state} ({len(state_tasks)}): {'; '.join(task_descs)}")
            
            return "[TASKS —\n" + "\n".join(lines) + "]"
            
    except Exception as e:
        logger.error(f"Task query failed: {e}")
        return "[TASKS — Could not reach orchestrator]"


async def _query_system_health(user_text: str) -> str:
    """Query health endpoints across all services."""
    results = []
    
    health_endpoints = {
        "Orchestrator": f"{SERVICE_MAP['orchestrator']}/health",
        "Scheduler":    f"{SERVICE_MAP['scheduler']}/health",
        "Memory":       f"{SERVICE_MAP['memory']}/health",
        "Watchtower":   f"{SERVICE_MAP['watchtower']}/health",
        "RAG":          f"{SERVICE_MAP['rag']}/health",
    }
    
    async with httpx.AsyncClient(timeout=5.0) as client:
        for name, url in health_endpoints.items():
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    results.append(f"{name}: online")
                else:
                    results.append(f"{name}: responded with {resp.status_code}")
            except Exception:
                results.append(f"{name}: DOWN/unreachable")
    
    return "[SYSTEM HEALTH —\n" + "\n".join(results) + "]"


async def _query_memory(user_text: str) -> str:
    """Query RAG for relevant memories."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{SERVICE_MAP['rag']}/query",
                json={"query": user_text, "k": 3}
            )
            data = resp.json()
            results = data.get("results", [])
            
            if not results:
                return "[MEMORY — No relevant memories found.]"
            
            memories = []
            for r in results:
                content = r.get("content", "")[:200]
                source = r.get("source", "unknown")
                memories.append(f"({source}) {content}")
            
            return "[MEMORY —\n" + "\n".join(memories) + "]"
            
    except Exception as e:
        logger.error(f"Memory query failed: {e}")
        return "[MEMORY — Could not reach RAG service]"


_HANDLER_MAP = {
    "_query_schedule": _query_schedule,
    "_query_tasks": _query_tasks,
    "_query_system_health": _query_system_health,
    "_query_memory": _query_memory,
}


async def _gather_system_context(user_text: str) -> str:
    """Detect intents and query relevant services. Returns context string to inject."""
    intents = _detect_intents(user_text)
    
    if not intents:
        return ""
    
    logger.info(f"Detected intents: {intents}")
    
    tasks = []
    for intent_name in intents:
        handler_name = INTENT_PATTERNS[intent_name]["handler"]
        handler_fn = _HANDLER_MAP.get(handler_name)
        if handler_fn:
            tasks.append(handler_fn(user_text))
    
    if not tasks:
        return ""
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    context_parts = []
    for r in results:
        if isinstance(r, str):
            context_parts.append(r)
        elif isinstance(r, Exception):
            logger.error(f"Context query failed: {r}")
    
    return "\n".join(context_parts)


def _detect_command(text: str) -> bool:
    """Check if user text contains a command that needs the orchestrator pipeline."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in COMMAND_KEYWORDS)


def _is_command_override(text: str) -> bool:
    """Check if text contains a keyword that ALWAYS means 'execute', not 'query'."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in COMMAND_OVERRIDE_KEYWORDS)


async def _dispatch_to_orchestrator(user_text: str) -> dict:
    """Send a command to orchestrator's /companion endpoint for execution.
    
    Returns dict with: success, response, intent, queued, task_id (if queued)
    """
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "http://localhost:8001/companion",
                json={"message": user_text}
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Orchestrator dispatch failed: {e}")
        return {
            "success": False,
            "response": "I tried to send that to the orchestrator but couldn't reach it.",
            "intent": "error",
        }


# ---------------------------------------------------------------------------
# Conversational Brain (Ministral via Ollama)
# ---------------------------------------------------------------------------

async def roux_think(user_text: str) -> str:
    """Send user text to Ministral with conversation history and live system context.
    
    Phase 29.3: History is now persistent via shared/conversations.py.
    Both voice and dashboard share the same conversation thread.
    """
    
    # Gather real system data based on what DJ is asking about
    system_context = await _gather_system_context(user_text)
    
    # Build the user message — inject system context if we have any
    if system_context:
        augmented_text = f"{user_text}\n\n--- LIVE SYSTEM DATA (use this to answer accurately) ---\n{system_context}"
        logger.info(f"Injected system context: {len(system_context)} chars")
    else:
        augmented_text = user_text
    
    # Store the clean user text in shared persistent history
    conv_add_message("user", user_text, {"source": "voice"})
    
    # Read recent history from shared state (dashboard + voice messages alike)
    recent = conv_get_messages(limit=MAX_CONVERSATION_HISTORY)
    
    # Build Ollama messages — convert from shared format to Ollama format
    history_messages = []
    for msg in recent[:-1]:  # all previous messages (skip the one we just added)
        role = msg.get("role", "user")
        if role in ("user", "assistant"):
            history_messages.append({"role": role, "content": msg["content"]})
    
    # Latest user message gets the augmented version (with system context)
    messages = [
        {"role": "system", "content": ROUX_CONVERSATION_PROMPT},
        *history_messages,
        {"role": "user", "content": augmented_text}
    ]
    
    try:
        # Phase 34: via shared/llm.py
        from shared.llm import llm_chat
        llm_response = await llm_chat(
            "companion",  # ministral-3:8b
            messages=messages,
            temperature=0.7,
            max_tokens=150,
            timeout=30,
            options={"top_p": 0.9},
        )

        if not llm_response.success:
            text = "Sorry, my brain went blank for a second."
        else:
            text = llm_response.text.strip().strip('"\'')
            if not text:
                text = "Sorry, my brain went blank for a second."

        conv_add_message("assistant", text, {"source": "voice", "agent": "roux"})
        return text

    except Exception as e:
        logger.error(f"Roux think failed: {e}")
        fallback = "I'm having trouble thinking right now. Ollama might be busy."
        conv_add_message("assistant", fallback, {"source": "voice", "agent": "roux"})
        return fallback


async def roux_process(user_text: str) -> str:
    """Unified router: chat locally (fast) or dispatch commands to orchestrator.
    
    Phase 29.3: This is Roux's front door for all input (voice or text).
    
    Flow:
      1. Command intents → dispatch to orchestrator /companion
      2. Informational intents (schedule, health, etc.) → local + system context
      3. Everything else → local Ministral chat (fast)
    """
    # Check for commands FIRST — things that need the execution pipeline
    # But NOT if an informational intent also matches ("how's the coder" vs "restart the coder")
    # UNLESS it's an override keyword ("restart" always means execute, never query)
    info_intents = _detect_intents(user_text)
    is_command = _detect_command(user_text)
    is_override = _is_command_override(user_text)
    
    if is_command and (not info_intents or is_override):
        # Pure command — send to orchestrator
        # NOTE: Do NOT write user message here — orchestrator's /companion does it
        # (avoids double-write to shared conversation history)
        logger.info(f"Command detected, dispatching to orchestrator: {user_text[:80]}")
        
        result = await _dispatch_to_orchestrator(user_text)
        response = result.get("response", "Something went wrong with that command.")
        
        # NOTE: Don't write assistant message here either — orchestrator already did
        # via add_assistant_message() in the /companion endpoint
        
        return response
    
    # Informational or chat → handle locally with roux_think
    # (roux_think handles its own history writes + system context injection)
    return await roux_think(user_text)


# ---------------------------------------------------------------------------
# TTS + Audio Playback
# ---------------------------------------------------------------------------

def _clean_for_tts(text: str) -> str:
    """Strip markdown and action narration before sending to TTS.
    
    Removes:
    - *action narration* blocks (e.g. '*checks logs*', '*leans in*')
    - **bold** markers
    - Remaining lone asterisks
    - Leading/trailing whitespace
    """
    import re
    # Remove *action* blocks (italic/action narration) — greedy-safe, non-newline
    text = re.sub(r'\*[^*\n]+\*', '', text)
    # Remove **bold** markers (keep the text between them)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    # Remove any remaining stray asterisks
    text = text.replace('*', '')
    # Collapse multiple spaces/newlines left behind
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'  +', ' ', text)
    return text.strip()


async def speak(text: str) -> dict:
    """Generate speech via Kitten TTS and play on desktop speakers."""
    global is_speaking
    
    async with speech_lock:
        is_speaking = True
        try:
            text = _clean_for_tts(text)
            logger.info(f"Speaking: {text}")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    KITTEN_TTS_URL,
                    json={"text": text, "voice": VOICE, "speed": SPEED}
                )
                response.raise_for_status()
            
            # Parse WAV from response
            wav_bytes = response.content
            with io.BytesIO(wav_bytes) as buf:
                with wave.open(buf, 'rb') as wf:
                    frames = wf.readframes(wf.getnframes())
                    sample_rate = wf.getframerate()
                    n_channels = wf.getnchannels()
                    sampwidth = wf.getsampwidth()
            
            # Convert to numpy array
            if sampwidth == 2:
                audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            elif sampwidth == 4:
                audio = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
            else:
                audio = np.frombuffer(frames, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0
            
            if n_channels > 1:
                audio = audio.reshape(-1, n_channels)
            
            # Play audio (blocking in a thread so we don't hold the event loop)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: sd.play(audio, samplerate=sample_rate))
            await loop.run_in_executor(None, sd.wait)
            
            duration = len(audio) / sample_rate if n_channels == 1 else len(audio) / sample_rate
            
            record = {
                "text": text,
                "spoken_at": datetime.now().isoformat(),
                "duration_seconds": round(duration, 2)
            }
            recent_spoken.append(record)
            
            logger.info(f"Spoke {duration:.1f}s of audio")
            return record
            
        except Exception as e:
            logger.error(f"Speech failed: {e}")
            raise
        finally:
            is_speaking = False
            # Set cooldown so VAD doesn't immediately pick up room echo from speakers
            global _post_speak_cooldown_until
            _post_speak_cooldown_until = time.time() + POST_SPEAK_COOLDOWN_S

# ---------------------------------------------------------------------------
# LLM Summarization via Ministral (notification mode)
# ---------------------------------------------------------------------------

async def summarize_events(events: list[QueueItem]) -> str:
    """Use Ministral-8B to summarize raw events into natural speech."""
    
    event_descriptions = []
    for item in events:
        e = item.event
        desc = f"[{e.source}] {e.event_type}"
        if e.message:
            desc += f" — {e.message}"
        if e.data:
            compact = {k: v for k, v in e.data.items() if k in (
                'task_id', 'agent', 'status', 'result', 'error', 'service',
                'reason', 'count', 'name', 'description'
            )}
            if compact:
                desc += f" {json.dumps(compact)}"
        event_descriptions.append(desc)
    
    events_text = "\n".join(event_descriptions)
    
    prompt = f"""Summarize these system events into a short spoken notification for DJ:

{events_text}

Remember: one or two sentences, warm and casual, plain English. This will be spoken aloud."""

    try:
        # Phase 34: via shared/llm.py
        from shared.llm import llm_chat
        llm_response = await llm_chat(
            "companion",  # ministral-3:8b
            messages=[
                {"role": "system", "content": ROUX_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=100,
            timeout=30,
            options={"top_p": 0.9},
        )

        if not llm_response.success or not llm_response.text:
            return f"{len(events)} events processed."

        text = llm_response.text.strip().strip('"\'')
        logger.info(f"Ministral summarized {len(events)} events → '{text}'")
        return text

    except Exception as e:
        logger.error(f"Summarization failed: {e}")
        return _fallback_summarize(events)

def _fallback_summarize(events: list[QueueItem]) -> str:
    """Template-based fallback if Ministral is unavailable."""
    if len(events) == 1:
        e = events[0].event
        return f"{e.source} reports: {e.event_type.replace('_', ' ')}."
    
    sources = set(e.event.source for e in events)
    types = set(e.event.event_type for e in events)
    
    if len(types) == 1:
        return f"{len(events)} {list(types)[0].replace('_', ' ')} events from {', '.join(sources)}."
    
    return f"{len(events)} events from {', '.join(sources)}."

# ---------------------------------------------------------------------------
# Batch Processing Loop
# ---------------------------------------------------------------------------

async def batch_processor():
    """Background loop that flushes the event queue every BATCH_WINDOW_SECONDS."""
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
                # Phase 29.3b: Persist batch notification speech to conversation history
                conv_add_message("assistant", text, {"source": "notification", "agent": "roux"})
            except Exception as e:
                logger.error(f"Batch processing failed: {e}")

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global batch_task
    
    logger.info("=" * 50)
    logger.info("Roux Voice Service v3.1.0 starting up (Phase 29.4: VAD + Always Listening)")
    logger.info(f"  Voice:    {VOICE} @ {SPEED}x")
    logger.info(f"  TTS:      {KITTEN_TTS_URL}")
    logger.info(f"  LLM:      {OLLAMA_MODEL} via {OLLAMA_URL}")
    logger.info(f"  Batch:    {BATCH_WINDOW_SECONDS}s window")
    logger.info("=" * 50)
    
    register_process("roux")
    
    # Test Kitten TTS connectivity
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(KITTEN_TTS_URL.replace("/tts", "/health"))
            r.raise_for_status()
            logger.info("Kitten TTS: connected ✓")
    except Exception as e:
        logger.warning(f"Kitten TTS not reachable: {e} (will retry on first speak)")
    
    # Test Ollama connectivity
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("http://localhost:11434/api/tags")
            r.raise_for_status()
            logger.info("Ollama: connected ✓")
    except Exception as e:
        logger.warning(f"Ollama not reachable: {e} (will use fallback templates)")
    
    # Detect microphone
    shure_device_idx_local, shure_info = _find_shure()
    if shure_device_idx_local is not None:
        global shure_device_idx, shure_sample_rate
        shure_device_idx = shure_device_idx_local
        shure_sample_rate = int(shure_info['default_samplerate'])
        logger.info(f"Mic: {shure_info['name']} ({shure_sample_rate}Hz) ✓")
    else:
        logger.warning("No Shure mic detected (voice/listen endpoints will be unavailable)")
    
    # Note: Whisper is lazy-loaded on first speech detected by VAD (or first /voice/listen call)
    logger.info(f"STT: Whisper {WHISPER_MODEL} (lazy-load on first use)")
    logger.info(f"VAD: webrtcvad={'available' if WEBRTCVAD_AVAILABLE else 'NOT INSTALLED — run: pip install webrtcvad'}")
    logger.info(f"VAD: scipy={'available' if SCIPY_AVAILABLE else 'not found — using numpy fallback for resampling'}")

    # Start batch processor
    batch_task = asyncio.create_task(batch_processor())

    # Start VAD always-listening loop (Phase 29.4)
    global vad_active, vad_task
    if WEBRTCVAD_AVAILABLE and shure_device_idx is not None:
        vad_active = True
        _vad_main_loop = asyncio.get_running_loop()
        vad_task = _vad_main_loop.run_in_executor(
            None, lambda: _run_vad_loop(_vad_main_loop)
        )
        logger.info("VAD: Always-listening loop started ✓")
    else:
        if not WEBRTCVAD_AVAILABLE:
            logger.warning("VAD: Skipped — webrtcvad not installed")
        elif shure_device_idx is None:
            logger.warning("VAD: Skipped — no microphone detected")

    # Startup announcement
    try:
        if vad_active:
            await speak("Roux online. Always listening.")
        else:
            await speak("Roux online. Listening for notifications.")
    except Exception:
        logger.warning("Couldn't play startup announcement")

    yield

    # Shutdown
    if batch_task:
        batch_task.cancel()
    # Stop VAD loop
    vad_active = False
    if vad_task:
        try:
            vad_task.cancel()
        except Exception:
            pass
    logger.info("Roux shutting down")

# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Roux — Voice Service for RouxYou",
    description="Two-way voice: notifications + conversation with live system awareness. All local.",
    version="3.0.0",
    lifespan=lifespan
)

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "roux",
        "version": "3.1.0",
        "voice": VOICE,
        "speed": SPEED,
        "llm": OLLAMA_MODEL,
        "stt": WHISPER_MODEL,
        "stt_loaded": whisper_model is not None,
        "mic": shure_device_idx is not None,
        "queue_size": len(event_queue),
        "is_speaking": is_speaking,
        "is_listening": is_listening,
        "vad_active": vad_active,
        "vad_awake": vad_awake,
        "vad_processing": _vad_processing,
        "vad_aggressiveness": VAD_AGGRESSIVENESS,
        "vad_available": WEBRTCVAD_AVAILABLE,
        "conversation_id": get_active_conversation_id(),
        "recent_count": len(recent_spoken),
        "uptime_check": datetime.now().isoformat()
    }

@app.post("/event")
async def receive_event(event: EventIn):
    """
    Receive a raw event from any agent/service.
    Critical events are spoken immediately.
    Normal/low events are batched and summarized.
    Phase 29.5: task_failed events trigger follow-up investigation dispatch.
    """
    logger.info(f"Event received: [{event.priority}] {event.source}/{event.event_type}")

    # Phase 29.5: Follow-up dispatch for execution failures
    if event.event_type == "task_failed":
        data = event.data or {}
        task_id = data.get("task_id", "")
        query = data.get("query", "")
        error = data.get("error", "")
        intent = data.get("intent", "")

        # Gate: only follow up on execution tasks, once per task, no loops
        if (query and task_id
                and intent not in ("chat", "chat_informed", "clarify", "explain")
                and task_id not in _task_failure_followups
                and "[AUTO-RETRY]" not in query
                and not query.startswith("The previous task")):
            _task_failure_followups.add(task_id)
            # Trim to prevent unbounded growth
            if len(_task_failure_followups) > 200:
                # Remove oldest entries (set has no order, but this caps size)
                while len(_task_failure_followups) > 100:
                    _task_failure_followups.pop()
            followup = (
                f"The previous task '{query[:150]}' failed with error: '{error[:200]}'. "
                f"Investigate why and suggest a fix."
            )
            logger.info(f"FOLLOW-UP: Dispatching investigation for failed task #{task_id}")
            asyncio.create_task(_dispatch_to_orchestrator(followup))

    if event.priority == Priority.critical:
        try:
            text = await summarize_events([QueueItem(timestamp=time.time(), event=event)])
            result = await speak(text)
            # Phase 29.3b: Persist notification speech to shared conversation history
            conv_add_message("assistant", text, {"source": "notification", "agent": "roux", "priority": "critical"})
            return {"status": "spoken", "priority": "critical", **result}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Critical speech failed: {e}")

    event_queue.append(QueueItem(timestamp=time.time(), event=event))

    if len(event_queue) >= MAX_BATCH_SIZE:
        batch = []
        while event_queue and len(batch) < MAX_BATCH_SIZE:
            batch.append(event_queue.popleft())
        try:
            text = await summarize_events(batch)
            asyncio.create_task(speak(text))
            # Phase 29.3b: Persist batch notification speech to conversation history
            conv_add_message("assistant", text, {"source": "notification", "agent": "roux"})
        except Exception as e:
            logger.error(f"Early flush failed: {e}")
    
    return {
        "status": "queued",
        "priority": event.priority,
        "queue_size": len(event_queue),
        "next_flush_in": f"{BATCH_WINDOW_SECONDS}s"
    }

@app.post("/speak")
async def direct_speak(req: SpeakIn):
    """Speak exact text directly (bypasses LLM summarization)."""
    result = await speak(req.text)
    # Phase 29.3b: Persist direct speech to conversation history
    conv_add_message("assistant", req.text, {"source": "direct_speak", "agent": "roux"})
    return {"status": "spoken", **result}

@app.get("/queue")
async def view_queue():
    """See what's currently pending in the batch window."""
    return {
        "queue_size": len(event_queue),
        "batch_window": BATCH_WINDOW_SECONDS,
        "events": [
            {
                "source": item.event.source,
                "type": item.event.event_type,
                "priority": item.event.priority,
                "age_seconds": round(time.time() - item.timestamp, 1)
            }
            for item in event_queue
        ]
    }

@app.get("/history")
async def speech_history():
    """Recent speech history."""
    return {
        "count": len(recent_spoken),
        "recent": list(recent_spoken)
    }


# ---------------------------------------------------------------------------
# Voice Endpoints (Two-Way)
# ---------------------------------------------------------------------------

@app.post("/voice/listen")
async def voice_listen(req: ListenRequest = None):
    """
    Record from mic → transcribe with Whisper Turbo → return text.
    First call lazy-loads Whisper onto GPU.
    """
    global is_listening
    
    if shure_device_idx is None:
        raise HTTPException(status_code=503, detail="No microphone detected")
    
    duration_limit = req.duration_limit if req else MAX_LISTEN_SECONDS
    
    is_listening = True
    try:
        loop = asyncio.get_event_loop()
        logger.info(f"Listening for up to {duration_limit}s...")
        audio, sr = await loop.run_in_executor(None, lambda: _record_audio(duration_limit))
        
        if audio is None or len(audio) == 0:
            return {"text": "", "duration": 0, "audio_seconds": 0}
        
        audio_seconds = len(audio) / sr
        if audio_seconds < MIN_AUDIO_SECONDS:
            return {"text": "", "duration": 0, "audio_seconds": round(audio_seconds, 2),
                    "note": "Too short, skipped"}
        
        t0 = time.time()
        text = await loop.run_in_executor(None, lambda: _transcribe(audio, sr))
        transcribe_time = time.time() - t0
        
        logger.info(f"Transcribed {audio_seconds:.1f}s audio in {transcribe_time:.1f}s: {text[:100]}")
        
        return {
            "text": text,
            "duration": round(transcribe_time, 2),
            "audio_seconds": round(audio_seconds, 2)
        }
    finally:
        is_listening = False


@app.post("/voice/chat")
async def voice_chat(req: ChatIn):
    """
    Text in → Roux processes (chat locally or dispatch command) → speaks response.
    Phase 29.3: Routes through roux_process for unified handling.
    """
    t0 = time.time()
    response_text = await roux_process(req.text)
    think_time = time.time() - t0
    
    t1 = time.time()
    speak_result = await speak(response_text)
    speak_time = time.time() - t1
    
    return {
        "you_said": req.text,
        "roux_said": response_text,
        "think_duration": round(think_time, 2),
        "speak_duration": round(speak_time, 2),
        "total_duration": round(think_time + speak_time, 2)
    }


@app.post("/voice/converse")
async def voice_converse(req: ListenRequest = None):
    """
    Full two-way loop: listen → transcribe → think (with system context) → speak.
    The complete local voice conversation endpoint.
    """
    global is_listening
    
    if shure_device_idx is None:
        raise HTTPException(status_code=503, detail="No microphone detected")
    
    duration_limit = req.duration_limit if req else MAX_LISTEN_SECONDS
    loop = asyncio.get_event_loop()
    
    # 1. Listen
    is_listening = True
    try:
        t_listen_start = time.time()
        audio, sr = await loop.run_in_executor(None, lambda: _record_audio(duration_limit))
        listen_time = time.time() - t_listen_start
    finally:
        is_listening = False
    
    if audio is None or len(audio) / sr < MIN_AUDIO_SECONDS:
        return {"error": "No speech detected", "listen_duration": round(listen_time, 2)}
    
    audio_seconds = len(audio) / sr
    
    # 2. Transcribe
    t_stt_start = time.time()
    text = await loop.run_in_executor(None, lambda: _transcribe(audio, sr))
    stt_time = time.time() - t_stt_start
    
    if not text:
        return {"error": "Empty transcription", "audio_seconds": round(audio_seconds, 2)}
    
    logger.info(f"DJ said: {text}")
    
    # 3. Process (chat locally or dispatch command to orchestrator)
    t_think_start = time.time()
    response = await roux_process(text)
    think_time = time.time() - t_think_start
    
    logger.info(f"Roux says: {response}")
    
    # 4. Speak
    t_speak_start = time.time()
    await speak(response)
    speak_time = time.time() - t_speak_start
    
    total = listen_time + stt_time + think_time + speak_time
    
    return {
        "you_said": text,
        "roux_said": response,
        "audio_seconds": round(audio_seconds, 2),
        "listen_duration": round(listen_time, 2),
        "stt_duration": round(stt_time, 2),
        "think_duration": round(think_time, 2),
        "speak_duration": round(speak_time, 2),
        "total_duration": round(total, 2)
    }


@app.get("/voice/conversation")
async def get_voice_conversation():
    """Get conversation history (Phase 29.3: shared with dashboard)."""
    messages = conv_get_messages(limit=50)
    return {
        "messages": messages,
        "count": len(messages),
        "conversation_id": get_active_conversation_id(),
        "note": "Shared state — same history as dashboard chat"
    }


@app.post("/voice/conversation/clear")
async def clear_voice_conversation():
    """Clear conversation history (affects shared state — dashboard too)."""
    from shared.conversations import clear_conversation
    clear_conversation()
    return {"status": "cleared", "note": "Shared conversation cleared"}


# ---------------------------------------------------------------------------
# VAD Control Endpoint (Phase 29.4)
# ---------------------------------------------------------------------------

@app.post("/voice/vad/toggle")
async def vad_toggle():
    """Start or stop the always-listening VAD loop at runtime.
    
    Useful for: muting Roux while gaming/on a call, debugging,
    or toggling from the dashboard without restarting the service.
    """
    global vad_active, vad_task

    if not WEBRTCVAD_AVAILABLE:
        raise HTTPException(status_code=503, detail="webrtcvad not installed")
    if shure_device_idx is None:
        raise HTTPException(status_code=503, detail="No microphone detected")

    if vad_active:
        # Stop it
        vad_active = False
        logger.info("VAD: Stopped via /voice/vad/toggle")
        return {"vad_active": False, "message": "VAD loop stopped"}
    else:
        # Start it
        vad_active = True
        main_event_loop = asyncio.get_running_loop()
        vad_task = main_event_loop.run_in_executor(
            None, lambda: _run_vad_loop(main_event_loop)
        )
        logger.info("VAD: Started via /voice/vad/toggle")
        return {"vad_active": True, "message": "VAD loop started"}


@app.get("/voice/vad/status")
async def vad_status():
    """Quick VAD state check without the full health payload."""
    return {
        "vad_active": vad_active,
        "vad_awake": vad_awake,
        "vad_processing": _vad_processing,
        "vad_available": WEBRTCVAD_AVAILABLE,
        "vad_aggressiveness": VAD_AGGRESSIVENESS,
        "wake_word": WAKE_WORD,
        "awake_timeout_s": AWAKE_TIMEOUT_S,
        "is_speaking": is_speaking,
        "mic_detected": shure_device_idx is not None,
    }


@app.post("/voice/vad/wake")
async def vad_force_wake():
    """Force Roux into awake mode without saying the wake word.
    Useful from dashboard or when you want a longer conversation session.
    """
    global vad_awake, _vad_last_activity
    if not vad_active:
        raise HTTPException(status_code=409, detail="VAD loop not running")
    vad_awake = True
    _vad_last_activity = time.time()
    logger.info("VAD: Force-woken via /voice/vad/wake")
    await speak("I'm awake. Go ahead.")
    return {"vad_awake": True, "message": "Roux is now awake and listening"}


@app.post("/voice/vad/sleep")
async def vad_force_sleep():
    """Force Roux back to sleep mode. VAD stays active but ignores non-wake-word speech."""
    global vad_awake
    vad_awake = False
    logger.info("VAD: Force-slept via /voice/vad/sleep")
    return {"vad_awake": False, "message": "Roux is sleeping. Say her name to wake."}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8014, log_level="info")
