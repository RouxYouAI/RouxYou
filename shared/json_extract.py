"""Canonical robust JSON extraction from messy LLM output.

Built 2026-05-27 to STOP re-solving structured-output-from-thinking-models
per-service. The researcher and coder each grew their
own variant of "strip the qwen3 <think>, strip the ``` fences, dig the JSON
out of surrounding prose." This is the one place that lives now.

Returns the parsed value (dict/list/...) or None — callers decide the fallback.
Pair it with `think:false` + `format:json` on the Ollama call for clean input;
this is the belt to that suspenders (handles the cases where prose leaks in).
"""
import json
from typing import Any, Optional


def extract_json(text: str) -> Optional[Any]:
    """Pull the first valid JSON value out of messy LLM text.

    Order: drop qwen3 reasoning (<think>…</think>) → strip ``` fences →
    parse the whole thing → fall back to the outermost {..} / [..] substring
    (tolerates prose like 'Here are the findings: [...]'). None if nothing parses.
    """
    if not text or not text.strip():
        return None
    s = text

    # 1. Drop qwen3 reasoning. Keep only what follows the final </think>;
    #    a lone opening <think> with no close means it was all reasoning.
    if "</think>" in s:
        s = s.split("</think>")[-1]
    if "<think>" in s:
        s = s.split("<think>")[0]

    # 2. Strip markdown code fences.
    if "```json" in s:
        s = s.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in s:
        parts = s.split("```")
        if len(parts) >= 3:
            s = parts[1]
    s = s.strip()

    # 3. Clean case (what think:false + format:json should give us).
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        pass

    # 4. Prose-wrapped fallback: outermost object, then outermost array.
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = s.find(open_c)
        end = s.rfind(close_c) + 1
        if 0 <= start < end:
            try:
                return json.loads(s[start:end])
            except (json.JSONDecodeError, ValueError):
                continue
    return None
