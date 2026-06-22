"""
RouxYou — Unified LLM Provider Abstraction (Phase 34)
=====================================================
Centralizes all LLM API calls behind a provider-agnostic interface.
Supports: Ollama (local), vLLM (OpenAI-compatible), Anthropic Claude (cloud).

Usage:
    from shared.llm import get_provider, llm_generate, llm_chat

    # Quick one-shot (uses alias routing):
    response = await llm_generate("router", prompt="Hello", system="You are helpful.")
    response = await llm_chat("coder", messages=[{"role": "user", "content": "Plan this"}])

    # Direct provider access:
    provider = get_provider("ollama")
    result = await provider.generate(model="ministral-3:8b", prompt="Hi")

    # Health check:
    ok = await provider.health_check()
"""

import os
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, AsyncIterator

import aiohttp

# Load .env early so ANTHROPIC_API_KEY etc. are available
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

logger = logging.getLogger("roux.llm")


# ============================================================
# Data Types
# ============================================================

@dataclass
class TokenUsage:
    """Token usage from a single LLM call."""
    input_tokens: int = 0
    output_tokens: int = 0
    provider: str = ""
    model: str = ""
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class LLMResponse:
    """Unified response from any LLM provider."""
    text: str
    model: str = ""
    provider: str = ""
    usage: Optional[TokenUsage] = None
    raw: Optional[dict] = None  # Original API response for debugging
    success: bool = True
    error: str = ""

    @staticmethod
    def failure(error: str, provider: str = "", model: str = "") -> "LLMResponse":
        return LLMResponse(text="", model=model, provider=provider,
                           success=False, error=error)


# ============================================================
# Provider ABC
# ============================================================

class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    name: str = "base"

    @abstractmethod
    async def generate(self, model: str, prompt: str, system: str = "",
                       temperature: float = 0.7, max_tokens: int = 4096,
                       format: str = "", timeout: float = 120,
                       **kwargs) -> LLMResponse:
        """Single-turn generation (prompt in, text out)."""
        ...

    @abstractmethod
    async def chat(self, model: str, messages: list[dict], system: str = "",
                   temperature: float = 0.7, max_tokens: int = 4096,
                   format: str = "", timeout: float = 120,
                   **kwargs) -> LLMResponse:
        """Multi-turn chat (messages array in, text out)."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Returns True if the provider is reachable."""
        ...

    async def list_models(self) -> list[str]:
        """List available models. Not all providers support this."""
        return []


# ============================================================
# Ollama Provider
# ============================================================

class OllamaProvider(LLMProvider):
    """Local Ollama inference server."""

    name = "ollama"

    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url.rstrip("/")

    async def generate(self, model: str, prompt: str, system: str = "",
                       temperature: float = 0.7, max_tokens: int = 4096,
                       format: str = "", timeout: float = 120,
                       **kwargs) -> LLMResponse:
        """Ollama /api/generate endpoint."""
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens
        if format:
            payload["format"] = format

        # Pass through extra options (top_p, num_predict overrides, etc.)
        extra_options = kwargs.get("options", {})
        if extra_options:
            payload["options"].update(extra_options)

        # Top-level ollama-native passthrough (opt-in via kwargs). `think` routes
        # qwen3 reasoning into the <think> channel (gated by IsThinkSet in the
        # v5.3 Modelfile template); `keep_alive=-1` pins residency (no unload →
        # kills the 282s cold-load between turns). Only sent when the caller asks,
        # so Claude/vLLM providers and every existing caller are unaffected.
        if "think" in kwargs:
            payload["think"] = kwargs["think"]
        if "keep_alive" in kwargs:
            payload["keep_alive"] = kwargs["keep_alive"]

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/api/generate",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return LLMResponse.failure(
                            f"Ollama returned {resp.status}: {body[:200]}",
                            provider="ollama", model=model
                        )
                    result = await resp.json()
                    text = result.get("response", "")

                    usage = TokenUsage(
                        input_tokens=result.get("prompt_eval_count", 0),
                        output_tokens=result.get("eval_count", 0),
                        provider="ollama",
                        model=model,
                    )

                    return LLMResponse(
                        text=text, model=model, provider="ollama",
                        usage=usage, raw=result
                    )
        except Exception as e:
            logger.warning(f"Ollama generate failed ({model}): {e}")
            return LLMResponse.failure(str(e), provider="ollama", model=model)

    async def chat(self, model: str, messages: list[dict], system: str = "",
                   temperature: float = 0.7, max_tokens: int = 4096,
                   format: str = "", timeout: float = 120,
                   **kwargs) -> LLMResponse:
        """Ollama /api/chat endpoint."""
        # Inject system message if provided and not already in messages
        chat_messages = list(messages)
        if system and not any(m.get("role") == "system" for m in chat_messages):
            chat_messages.insert(0, {"role": "system", "content": system})

        payload = {
            "model": model,
            "messages": chat_messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens
        if format:
            payload["format"] = format

        extra_options = kwargs.get("options", {})
        if extra_options:
            payload["options"].update(extra_options)

        # Top-level ollama-native passthrough (opt-in via kwargs). `think` routes
        # qwen3 reasoning into the <think> channel; `keep_alive=-1` pins residency.
        # Only sent when the caller asks → other providers/callers unaffected.
        if "think" in kwargs:
            payload["think"] = kwargs["think"]
        if "keep_alive" in kwargs:
            payload["keep_alive"] = kwargs["keep_alive"]

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return LLMResponse.failure(
                            f"Ollama returned {resp.status}: {body[:200]}",
                            provider="ollama", model=model
                        )
                    result = await resp.json()
                    text = result.get("message", {}).get("content", "")

                    usage = TokenUsage(
                        input_tokens=result.get("prompt_eval_count", 0),
                        output_tokens=result.get("eval_count", 0),
                        provider="ollama",
                        model=model,
                    )

                    return LLMResponse(
                        text=text, model=model, provider="ollama",
                        usage=usage, raw=result
                    )
        except Exception as e:
            logger.warning(f"Ollama chat failed ({model}): {e}")
            return LLMResponse.failure(str(e), provider="ollama", model=model)

    async def chat_stream(self, model: str, messages: list[dict], system: str = "",
                          temperature: float = 0.7, max_tokens: int = 4096,
                          timeout: float = 600, **kwargs):
        """Streaming variant of chat — yields text chunks as Ollama emits them.

        Returns an async generator. Each yield is a `str` chunk. The generator
        also accumulates total state (eval_count, prompt_eval_count) in
        `self._last_stream_meta` for callers that want usage data post-stream.

        Use when you want to surface tokens incrementally (SSE to dashboard,
        etc.). For one-shot calls keep using `chat()`.
        """
        chat_messages = list(messages)
        if system and not any(m.get("role") == "system" for m in chat_messages):
            chat_messages.insert(0, {"role": "system", "content": system})

        payload = {
            "model": model,
            "messages": chat_messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens
        extra_options = kwargs.get("options", {})
        if extra_options:
            payload["options"].update(extra_options)

        # Top-level ollama-native passthrough (opt-in via kwargs). `think` routes
        # qwen3 reasoning into the <think> channel; `keep_alive=-1` pins residency.
        # Note: chat_stream only yields message.content (line below), so thinking
        # tokens are naturally NOT streamed to SSE clients — the stream just goes
        # quiet during the think phase, then bursts content.
        emit_thinking = kwargs.get("emit_thinking", False)  # opt-in: also yield the reasoning channel
        if "think" in kwargs:
            payload["think"] = kwargs["think"]
        if "keep_alive" in kwargs:
            payload["keep_alive"] = kwargs["keep_alive"]

        self._last_stream_meta = {"eval_count": 0, "prompt_eval_count": 0, "done_reason": None}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"Ollama chat_stream returned {resp.status}: {body[:200]}")
                        return
                    async for raw_line in resp.content:
                        if not raw_line:
                            continue
                        try:
                            event = json.loads(raw_line.decode("utf-8"))
                        except Exception:
                            continue
                        msg = event.get("message") or {}
                        chunk = msg.get("content", "")
                        think = msg.get("thinking", "")
                        if emit_thinking:
                            if chunk or think:
                                yield {"content": chunk, "thinking": think}
                        elif chunk:
                            yield chunk
                        if event.get("done"):
                            self._last_stream_meta["eval_count"] = event.get("eval_count", 0)
                            self._last_stream_meta["prompt_eval_count"] = event.get("prompt_eval_count", 0)
                            self._last_stream_meta["done_reason"] = event.get("done_reason")
                            break
        except Exception as e:
            logger.warning(f"Ollama chat_stream failed ({model}): {e}")
            return

    async def health_check(self) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return [m["name"] for m in data.get("models", [])]
        except Exception:
            pass
        return []


# ============================================================
# vLLM / OpenAI-Compatible Provider
# ============================================================

class OpenAICompatProvider(LLMProvider):
    """OpenAI-compatible API (vLLM, LocalAI, llama.cpp server, OpenAI, xAI)."""

    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str = "",
                 provider_name: str = "vllm"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.name = provider_name

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def generate(self, model: str, prompt: str, system: str = "",
                       temperature: float = 0.7, max_tokens: int = 4096,
                       format: str = "", timeout: float = 120,
                       **kwargs) -> LLMResponse:
        """Translate to /v1/chat/completions with a single user message."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return await self.chat(model, messages, temperature=temperature,
                               max_tokens=max_tokens, format=format,
                               timeout=timeout, **kwargs)

    async def chat(self, model: str, messages: list[dict], system: str = "",
                   temperature: float = 0.7, max_tokens: int = 4096,
                   format: str = "", timeout: float = 120,
                   **kwargs) -> LLMResponse:
        """OpenAI /v1/chat/completions endpoint."""
        chat_messages = list(messages)
        if system and not any(m.get("role") == "system" for m in chat_messages):
            chat_messages.insert(0, {"role": "system", "content": system})

        payload = {
            "model": model,
            "messages": chat_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if kwargs.get("tools"):
            # 2026-06-01: tool-use is the RELIABLE structured-output path on this Vulkan
            # llama-server build (response_format=json_object is only soft-honored here).
            # tool_choice forces a schema-valid tool_call; the handler below surfaces its args.
            payload["tools"] = kwargs["tools"]
            payload["tool_choice"] = kwargs.get("tool_choice", "required")
        elif format == "json":
            payload["response_format"] = {"type": "json_object"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return LLMResponse.failure(
                            f"{self.name} returned {resp.status}: {body[:200]}",
                            provider=self.name, model=model
                        )
                    result = await resp.json()
                    choice = result.get("choices", [{}])[0]
                    _msg = choice.get("message", {})
                    text = _msg.get("content", "") or ""
                    # If the model answered via a tool_call (tool-use path), surface the
                    # call's JSON arguments AS the text — JSON-parsing callers (the coder)
                    # then transparently get clean schema-valid JSON, not prose. (2026-06-01)
                    _tcs = _msg.get("tool_calls")
                    if _tcs:
                        _args = (_tcs[0].get("function") or {}).get("arguments")
                        if _args:
                            text = _args

                    raw_usage = result.get("usage", {})
                    usage = TokenUsage(
                        input_tokens=raw_usage.get("prompt_tokens", 0),
                        output_tokens=raw_usage.get("completion_tokens", 0),
                        provider=self.name,
                        model=model,
                    )

                    return LLMResponse(
                        text=text, model=model, provider=self.name,
                        usage=usage, raw=result
                    )
        except Exception as e:
            logger.warning(f"{self.name} chat failed ({model}): {e}")
            return LLMResponse.failure(str(e), provider=self.name, model=model)

    async def chat_stream(self, model: str, messages: list[dict], system: str = "",
                          temperature: float = 0.7, max_tokens: int = 4096,
                          timeout: float = 600, **kwargs):
        """Streaming variant — yields `str` content deltas from an OpenAI-compatible
        SSE stream (llama.cpp server, vLLM). Accumulates usage in
        `self._last_stream_meta` post-stream (mirrors OllamaProvider.chat_stream).

        Thinking is controlled server-side (e.g. llama-server --reasoning-budget) and
        surfaced under delta.reasoning_content, NOT delta.content — so only the answer
        streams here; the stream goes quiet during the think phase, then bursts. This
        matches the Ollama variant's behavior, so the companion SSE path is unchanged.

        max_tokens<=0 (e.g. -1) is omitted from the payload → server-default (uncapped,
        bounded by context) — preserves the companion's uncapped-answer preference.
        """
        chat_messages = list(messages)
        if system and not any(m.get("role") == "system" for m in chat_messages):
            chat_messages.insert(0, {"role": "system", "content": system})

        payload = {
            "model": model,
            "messages": chat_messages,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if max_tokens and max_tokens > 0:
            payload["max_tokens"] = max_tokens
        emit_thinking = kwargs.get("emit_thinking", False)  # opt-in: also yield delta.reasoning_content

        self._last_stream_meta = {"eval_count": 0, "prompt_eval_count": 0, "done_reason": None}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"{self.name} chat_stream returned {resp.status}: {body[:200]}")
                        return
                    async for raw_line in resp.content:
                        if not raw_line:
                            continue
                        line = raw_line.decode("utf-8").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                        except Exception:
                            continue
                        choices = event.get("choices") or []
                        if choices:
                            delta = choices[0].get("delta") or {}
                            chunk = delta.get("content", "")
                            think = delta.get("reasoning_content", "") or delta.get("reasoning", "")
                            if emit_thinking:
                                if chunk or think:
                                    yield {"content": chunk, "thinking": think}
                            elif chunk:
                                yield chunk
                        usage = event.get("usage")
                        if usage:
                            self._last_stream_meta["eval_count"] = usage.get("completion_tokens", 0)
                            self._last_stream_meta["prompt_eval_count"] = usage.get("prompt_tokens", 0)
        except Exception as e:
            logger.warning(f"{self.name} chat_stream failed ({model}): {e}")
            return

    async def health_check(self) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/v1/models",
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/v1/models",
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return [m["id"] for m in data.get("data", [])]
        except Exception:
            pass
        return []


# ============================================================
# Anthropic Claude Provider
# ============================================================

def _model_deprecates_temperature(model: str) -> bool:
    """Newer Anthropic models (Opus 4.7+) reject `temperature`. Detect those
    cases so we can omit the param. Older models still accept it."""
    if not model:
        return False
    m = model.lower()
    # Opus 4.7 and any later 4.x or 5.x line — extend as needed.
    return "opus-4-7" in m or "opus-4-8" in m or "opus-5" in m or "sonnet-5" in m


class ClaudeProvider(LLMProvider):
    """Anthropic Messages API."""

    name = "claude"
    API_VERSION = "2023-06-01"
    MAX_RETRIES = 4
    RETRY_BACKOFF_MS = [500, 2000, 5000, 12000]  # 0.5s, 2s, 5s, 12s — generous for 529 overload

    def __init__(self, api_key: str = "", base_url: str = "https://api.anthropic.com"):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.API_VERSION,
        }

    def _is_retryable(self, status: int) -> bool:
        # 529 = Anthropic "Overloaded" (transient capacity) — retry with backoff.
        return status in (408, 429, 500, 502, 503, 504, 529)

    async def _request_with_retry(self, payload: dict,
                                  timeout: float) -> tuple[int, dict]:
        """POST to /v1/messages with retry logic."""
        last_status = 0
        last_body = {}

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.base_url}/v1/messages",
                        json=payload,
                        headers=self._headers(),
                        timeout=aiohttp.ClientTimeout(total=timeout)
                    ) as resp:
                        last_status = resp.status
                        last_body = await resp.json()

                        if resp.status == 200:
                            return last_status, last_body

                        if not self._is_retryable(resp.status):
                            return last_status, last_body

            except Exception as e:
                last_body = {"error": str(e)}

            # Backoff before retry
            if attempt < self.MAX_RETRIES:
                wait_ms = self.RETRY_BACKOFF_MS[min(attempt, len(self.RETRY_BACKOFF_MS) - 1)]
                import asyncio
                await asyncio.sleep(wait_ms / 1000)

        return last_status, last_body

    async def generate(self, model: str, prompt: str, system: str = "",
                       temperature: float = 0.7, max_tokens: int = 4096,
                       format: str = "", timeout: float = 120,
                       **kwargs) -> LLMResponse:
        """Single-turn via Messages API."""
        messages = [{"role": "user", "content": prompt}]
        return await self.chat(model, messages, system=system,
                               temperature=temperature, max_tokens=max_tokens,
                               format=format, timeout=timeout, **kwargs)

    async def chat(self, model: str, messages: list[dict], system: str = "",
                   temperature: float = 0.7, max_tokens: int = 4096,
                   format: str = "", timeout: float = 120,
                   **kwargs) -> LLMResponse:
        """Anthropic /v1/messages endpoint."""
        if not self.api_key:
            return LLMResponse.failure(
                "No ANTHROPIC_API_KEY set", provider="claude", model=model
            )

        # Anthropic uses system as a top-level field, not in messages
        chat_messages = [m for m in messages if m.get("role") != "system"]
        # Extract system from messages if not provided directly
        if not system:
            for m in messages:
                if m.get("role") == "system":
                    system = m["content"]
                    break

        payload = {
            "model": model,
            "messages": chat_messages,
            "max_tokens": max_tokens,
        }
        # Opus 4.7+ deprecated `temperature` — only send for older models that still accept it.
        if not _model_deprecates_temperature(model):
            payload["temperature"] = temperature
        if system:
            payload["system"] = system

        status, result = await self._request_with_retry(payload, timeout)

        if status != 200:
            error_msg = result.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(result))
            return LLMResponse.failure(
                f"Claude API {status}: {error_msg}",
                provider="claude", model=model
            )

        # Extract text from content blocks
        text_parts = []
        for block in result.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        text = "\n".join(text_parts)

        raw_usage = result.get("usage", {})
        usage = TokenUsage(
            input_tokens=raw_usage.get("input_tokens", 0),
            output_tokens=raw_usage.get("output_tokens", 0),
            provider="claude",
            model=model,
        )

        return LLMResponse(
            text=text, model=model, provider="claude",
            usage=usage, raw=result
        )

    async def health_check(self) -> bool:
        """Check if API key is set. Can't ping Anthropic without a real request."""
        return bool(self.api_key)

    async def list_models(self) -> list[str]:
        return [
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
        ]


# ============================================================
# Provider Registry & Alias Router
# ============================================================

# Default model aliases — maps logical names to provider/model pairs
DEFAULT_ALIASES = {
    "router":     {"provider": "ollama", "model": "ministral-3:8b"},
    "coder":      {"provider": "ollama", "model": "qwen3:14b-q4_K_M"},
    "companion":  {"provider": "ollama", "model": "ministral-3:8b"},
    "coach":      {"provider": "ollama", "model": "qwen3:14b-q4_K_M"},
    "researcher": {"provider": "ollama", "model": "qwen3:14b-q4_K_M"},
    "embeddings": {"provider": "ollama", "model": "nomic-embed-text-cpu"},  # 2026-05-30: CPU-pinned default (was GPU "nomic-embed-text"). SAME weights/digest, num_gpu=0. Defense-in-depth floor so any path skipping the config.yaml override still can't load a GPU embedder that evicts roux-v53 off the 16GB card. Revert: drop the "-cpu".
    "bigbrain":   {"provider": "llamacpp", "model": "roux-v53"},  # 2026-06-18: was claude/claude-opus-4-8 — switched to local v5.3 (no Anthropic credits). config.yaml override matches. REVERT → {"provider":"claude","model":"claude-opus-4-8"}.
}


class ProviderRegistry:
    """
    Central registry of LLM providers and model aliases.
    Singleton — use get_registry() to access.
    """

    def __init__(self):
        self._providers: dict[str, LLMProvider] = {}
        self._aliases: dict[str, dict] = dict(DEFAULT_ALIASES)
        self._initialized = False

    def register(self, name: str, provider: LLMProvider):
        """Register a provider by name."""
        self._providers[name] = provider
        logger.info(f"LLM provider registered: {name}")

    def get_provider(self, name: str) -> Optional[LLMProvider]:
        """Get a provider by name."""
        return self._providers.get(name)

    def set_alias(self, alias: str, provider: str, model: str):
        """Set or update a model alias."""
        self._aliases[alias] = {"provider": provider, "model": model}

    def resolve_alias(self, alias: str) -> tuple[LLMProvider, str]:
        """
        Resolve an alias to (provider, model).
        Falls back to treating the alias as a direct model name on ollama.
        """
        if alias in self._aliases:
            entry = self._aliases[alias]
            provider = self.get_provider(entry["provider"])
            if provider:
                return provider, entry["model"]

        # Fallback: treat as direct model name on default (ollama)
        ollama = self.get_provider("ollama")
        if ollama:
            return ollama, alias

        raise ValueError(f"Cannot resolve alias '{alias}': no provider available")

    def initialize_defaults(self, ollama_url: str = "http://localhost:11434",
                            vllm_url: str = "", vllm_api_key: str = "",
                            router_url: str = "", router_api_key: str = "",
                            llamacpp_url: str = "", llamacpp_api_key: str = "",
                            llamacpp_coder_url: str = "", llamacpp_coder_api_key: str = "",
                            claude_api_key: str = "",
                            aliases: Optional[dict] = None):
        """
        Initialize default providers. Call once at startup.
        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._initialized:
            return

        # Always register Ollama (local, free, default)
        self.register("ollama", OllamaProvider(base_url=ollama_url))

        # vLLM / OpenAI-compat (if configured)
        if vllm_url:
            self.register("vllm", OpenAICompatProvider(
                base_url=vllm_url, api_key=vllm_api_key, provider_name="vllm"
            ))
            logger.info(f"vLLM provider configured at {vllm_url}")

        # llama.cpp server (Vulkan, OpenAI-compat) — Roux v53 backend with MoE
        # expert offload (--n-cpu-moe). 2026-05-30 "the precise way": beats
        # ollama's native-engine layer-split on the 16GB card (gen ~59 vs 38 t/s).
        # Same OpenAICompatProvider class; thinking controlled server-side
        # (--reasoning-budget), so the companion's think/keep_alive kwargs are
        # harmlessly ignored by this provider.
        if llamacpp_url:
            self.register("llamacpp", OpenAICompatProvider(
                base_url=llamacpp_url, api_key=llamacpp_api_key, provider_name="llamacpp"
            ))
            logger.info(f"llama.cpp provider configured at {llamacpp_url}")

        # llama.cpp CODER backend (lever #2): qwen3-coder via its own llama-server (--jinja
        # template) on a separate port, so an agentic coder can serve while v53 owns :8090.
        if llamacpp_coder_url:
            self.register("llamacpp_coder", OpenAICompatProvider(
                base_url=llamacpp_coder_url, api_key=llamacpp_coder_api_key, provider_name="llamacpp_coder"
            ))
            logger.info(f"llama.cpp coder provider configured at {llamacpp_coder_url}")

        # Router LoRA endpoint (separate OpenAI-compat process, e.g. port 9102).
        # 2026-05-15: Router LoRA serves on its own FastAPI alongside v3. Same
        # OpenAICompatProvider class, different base_url and provider name so
        # the alias system can route per-intent.
        if router_url:
            self.register("vllm_router", OpenAICompatProvider(
                base_url=router_url, api_key=router_api_key, provider_name="vllm_router"
            ))
            logger.info(f"Router LoRA provider configured at {router_url}")

        # Claude (if API key available)
        api_key = claude_api_key or os.getenv("ANTHROPIC_API_KEY", "")
        if api_key:
            self.register("claude", ClaudeProvider(api_key=api_key))
            logger.info("Claude provider configured")

        # Apply custom aliases
        if aliases:
            for alias, config in aliases.items():
                if isinstance(config, dict) and "provider" in config and "model" in config:
                    self.set_alias(alias, config["provider"], config["model"])
                elif isinstance(config, str) and "/" in config:
                    # Format: "provider/model"
                    parts = config.split("/", 1)
                    self.set_alias(alias, parts[0], parts[1])

        self._initialized = True
        logger.info(f"LLM registry initialized: {list(self._providers.keys())} "
                     f"| aliases: {list(self._aliases.keys())}")

    @property
    def initialized(self) -> bool:
        return self._initialized

    def status(self) -> dict:
        """Return registry status for dashboard/health checks."""
        return {
            "initialized": self._initialized,
            "providers": list(self._providers.keys()),
            "aliases": {k: f"{v['provider']}/{v['model']}" for k, v in self._aliases.items()},
        }


# ============================================================
# Module-level singleton & convenience functions
# ============================================================

_registry = ProviderRegistry()


def get_registry() -> ProviderRegistry:
    """Get the global provider registry."""
    return _registry


def get_provider(name: str) -> Optional[LLMProvider]:
    """Get a provider by name from the global registry."""
    return _registry.get_provider(name)


def init_providers(**kwargs):
    """
    Initialize default providers. Call once at startup.
    Auto-loads from config.yaml if present and no explicit kwargs given.
    """
    if not kwargs:
        # Try to load from config.yaml
        config = _load_config_yaml()
        if config:
            kwargs = config
    _registry.initialize_defaults(**kwargs)


def _load_config_yaml() -> Optional[dict]:
    """Load LLM config from config.yaml if it exists."""
    from pathlib import Path
    # Check common locations
    candidates = [
        Path(__file__).parent.parent / "config.yaml",  # project root
    ]
    for path in candidates:
        if path.exists():
            try:
                import yaml
                with open(path, "r") as f:
                    raw = yaml.safe_load(f)

                llm_config = raw.get("llm", {})
                if not llm_config:
                    # Fallback to top-level ollama_host
                    return {"ollama_url": raw.get("ollama_host", "http://localhost:11434")}

                result = {}

                # Ollama URL
                providers = llm_config.get("providers", {})
                ollama_cfg = providers.get("ollama", {})
                result["ollama_url"] = ollama_cfg.get("base_url", raw.get("ollama_host", "http://localhost:11434"))

                # vLLM
                vllm_cfg = providers.get("vllm", {})
                if vllm_cfg.get("enabled") and vllm_cfg.get("base_url"):
                    result["vllm_url"] = vllm_cfg["base_url"]
                    result["vllm_api_key"] = vllm_cfg.get("api_key", "")

                # llama.cpp server (OpenAI-compat, Vulkan v53 backend, e.g. port 8090)
                llamacpp_cfg = providers.get("llamacpp", {})
                if llamacpp_cfg.get("enabled") and llamacpp_cfg.get("base_url"):
                    result["llamacpp_url"] = llamacpp_cfg["base_url"]
                    result["llamacpp_api_key"] = llamacpp_cfg.get("api_key", "")

                # llama.cpp CODER backend (lever #2, 2026-06-05): qwen3-coder-30b-a3b on a
                # SECOND llama-server (e.g. port 8091), so the coder alias can target an
                # agentic coder via its proper --jinja template, separate from the v53 companion.
                llamacpp_coder_cfg = providers.get("llamacpp_coder", {})
                if llamacpp_coder_cfg.get("enabled") and llamacpp_coder_cfg.get("base_url"):
                    result["llamacpp_coder_url"] = llamacpp_coder_cfg["base_url"]
                    result["llamacpp_coder_api_key"] = llamacpp_coder_cfg.get("api_key", "")

                # Router LoRA endpoint (separate OpenAI-compat process)
                router_cfg = providers.get("vllm_router", {})
                if router_cfg.get("enabled") and router_cfg.get("base_url"):
                    result["router_url"] = router_cfg["base_url"]
                    result["router_api_key"] = router_cfg.get("api_key", "")

                # Claude
                claude_cfg = providers.get("claude", {})
                if claude_cfg.get("enabled"):
                    result["claude_api_key"] = os.getenv("ANTHROPIC_API_KEY", "")

                # Aliases
                aliases = llm_config.get("aliases", {})
                if aliases:
                    result["aliases"] = aliases

                logger.info(f"Loaded LLM config from {path}")
                return result

            except ImportError:
                logger.warning("PyYAML not installed — config.yaml ignored")
            except Exception as e:
                logger.warning(f"Failed to load config.yaml: {e}")

    return None


async def llm_generate(alias_or_model: str, prompt: str, system: str = "",
                       temperature: float = 0.7, max_tokens: int = 4096,
                       format: str = "", timeout: float = 120,
                       **kwargs) -> LLMResponse:
    """
    One-shot generation using an alias or direct model name.

    Examples:
        await llm_generate("router", prompt="Classify this intent")
        await llm_generate("coder", prompt="Plan these steps", format="json")
        await llm_generate("bigbrain", prompt="Complex reasoning task")
        await llm_generate("ministral-3:8b", prompt="Quick question")
    """
    if not _registry.initialized:
        init_providers()  # Auto-load config.yaml aliases (else fallback to DEFAULT_ALIASES misses vllm/v3 routing)

    provider, model = _registry.resolve_alias(alias_or_model)
    response = await provider.generate(
        model=model, prompt=prompt, system=system,
        temperature=temperature, max_tokens=max_tokens,
        format=format, timeout=timeout, **kwargs
    )

    # Phase 35: Auto-record usage for cost tracking
    if response.usage:
        try:
            from shared.cost_tracker import record_usage
            record_usage(response.usage, caller=alias_or_model)
        except Exception:
            pass  # Cost tracking is optional — don't break LLM calls

    return response


async def llm_chat(alias_or_model: str, messages: list[dict], system: str = "",
                    temperature: float = 0.7, max_tokens: int = 4096,
                    format: str = "", timeout: float = 120,
                    **kwargs) -> LLMResponse:
    """
    Multi-turn chat using an alias or direct model name.

    Examples:
        await llm_chat("coder", messages=[...], format="json", temperature=0.1)
        await llm_chat("bigbrain", messages=[...], system="You are an architect.")
    """
    if not _registry.initialized:
        init_providers()  # Auto-load config.yaml aliases (else fallback to DEFAULT_ALIASES misses vllm/v3 routing)

    provider, model = _registry.resolve_alias(alias_or_model)
    response = await provider.chat(
        model=model, messages=messages, system=system,
        temperature=temperature, max_tokens=max_tokens,
        format=format, timeout=timeout, **kwargs
    )

    # Phase 35: Auto-record usage for cost tracking
    if response.usage:
        try:
            from shared.cost_tracker import record_usage
            record_usage(response.usage, caller=alias_or_model)
        except Exception:
            pass

    return response


async def llm_chat_stream(alias_or_model: str, messages: list[dict], system: str = "",
                          temperature: float = 0.7, max_tokens: int = 4096,
                          timeout: float = 600, **kwargs):
    """Streaming version of llm_chat. Returns an async generator of text chunks.

    Currently only OllamaProvider implements chat_stream. Other providers
    (vLLM/Claude) raise NotImplementedError — callers should check first or
    use a fallback to chat().

    Example:
        async for chunk in llm_chat_stream("companion", messages=[...]):
            print(chunk, end="", flush=True)
    """
    if not _registry.initialized:
        init_providers()
    provider, model = _registry.resolve_alias(alias_or_model)
    if not hasattr(provider, "chat_stream"):
        raise NotImplementedError(
            f"provider {provider.__class__.__name__} doesn't implement chat_stream — "
            f"use llm_chat() for non-streaming."
        )
    async for chunk in provider.chat_stream(
        model=model, messages=messages, system=system,
        temperature=temperature, max_tokens=max_tokens, timeout=timeout, **kwargs
    ):
        yield chunk


async def llm_health(provider_name: str = "ollama") -> bool:
    """Check if a specific provider is healthy."""
    provider = _registry.get_provider(provider_name)
    if not provider:
        return False
    return await provider.health_check()


# ============================================================
# Sync wrappers (for non-async callers like coach.py, researcher.py)
# ============================================================

def llm_generate_sync(alias_or_model: str, prompt: str, system: str = "",
                      temperature: float = 0.7, max_tokens: int = 4096,
                      format: str = "", timeout: float = 120,
                      **kwargs) -> LLMResponse:
    """Synchronous wrapper around llm_generate. For use in non-async contexts."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an async context — create a new thread to avoid deadlock
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run,
                                 llm_generate(alias_or_model, prompt=prompt, system=system,
                                              temperature=temperature, max_tokens=max_tokens,
                                              format=format, timeout=timeout, **kwargs))
            return future.result(timeout=timeout + 5)
    else:
        return asyncio.run(
            llm_generate(alias_or_model, prompt=prompt, system=system,
                         temperature=temperature, max_tokens=max_tokens,
                         format=format, timeout=timeout, **kwargs)
        )


def llm_chat_sync(alias_or_model: str, messages: list[dict], system: str = "",
                   temperature: float = 0.7, max_tokens: int = 4096,
                   format: str = "", timeout: float = 120,
                   **kwargs) -> LLMResponse:
    """Synchronous wrapper around llm_chat. For use in non-async contexts."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run,
                                 llm_chat(alias_or_model, messages=messages, system=system,
                                          temperature=temperature, max_tokens=max_tokens,
                                          format=format, timeout=timeout, **kwargs))
            return future.result(timeout=timeout + 5)
    else:
        return asyncio.run(
            llm_chat(alias_or_model, messages=messages, system=system,
                     temperature=temperature, max_tokens=max_tokens,
                     format=format, timeout=timeout, **kwargs)
        )


# ============================================================
# Cold-start warmup
# ============================================================
# Cold-start latencies for local Ollama models can be 10-30s on the first
# call after a service starts (model load into VRAM + first prefill). Warm
# calls are <1s. The fix is to send a 1-token prompt at service startup so
# the model is loaded into VRAM before any user request arrives. Services
# should call warm_models() from a background task in their startup hook
# so the warmup doesn't block the service from coming up healthy.

async def warm_model(alias: str, timeout: float = 120) -> tuple:
    """Send a tiny prompt to load a model into memory.

    Returns (success, elapsed_seconds, error_message). On Ollama, this
    causes the model to be loaded into VRAM and stay loaded for the
    duration of the keepalive window (default 5 min).

    Uses max_tokens=1 + temperature=0 to minimize generation cost — we
    only care about the load, not the output.
    """
    start = time.time()
    try:
        response = await llm_generate(
            alias,
            prompt="hi",
            max_tokens=1,
            temperature=0.0,
            timeout=timeout,
        )
        elapsed = time.time() - start
        if response.success:
            return (True, elapsed, "")
        else:
            return (False, elapsed, response.error or "unknown")
    except Exception as e:
        elapsed = time.time() - start
        return (False, elapsed, str(e))


async def warm_models(aliases: list) -> dict:
    """Warm multiple model aliases in parallel.

    Returns dict of {alias: (success, elapsed_seconds, error)}.
    Safe to call on aliases that may resolve to the same underlying
    Ollama model — Ollama deduplicates the load internally, so the
    second call is fast.

    This function does NOT raise — it logs warnings on failure and
    returns the result dict. The caller decides what to do with
    failures (typically: nothing, since warmup is best-effort).
    """
    import asyncio
    if not aliases:
        return {}
    if not _registry.initialized:
        _registry.initialize_defaults()

    results = await asyncio.gather(
        *[warm_model(a) for a in aliases],
        return_exceptions=True,
    )
    out = {}
    for alias, r in zip(aliases, results):
        if isinstance(r, Exception):
            logger.warning(f"warm_model({alias}) raised: {r}")
            out[alias] = (False, 0.0, str(r))
        else:
            success, elapsed, error = r
            status = "OK" if success else "FAIL"
            logger.info(f"WARMUP [{status}] {alias}: {elapsed:.1f}s {('('+error+')') if error else ''}")
            out[alias] = r
    return out
