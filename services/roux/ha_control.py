"""Phase 2b — Roux's smart-home hands.

Roux is the SINGLE decider for the voice puck. When /voice/think classifies an
utterance as a device command, this module:

  1. holds a CATALOG of HA's controllable entities + their domains (live-fetched
     from HA's REST API and cached; falls back to a static snapshot if no token),
  2. asks Roux's LLM to emit an EXACT {service, entity_id, data} action, GRAMMAR-
     CONSTRAINED via the llama-server tool-use path so entity_id/service can only
     be real values (kills the structured-output-format weakness),
  3. returns that action to /voice/think, which hands it to HA's conversation.py
     to execute literally via hass.services.

Design notes:
  - HA is the pure EXECUTOR. This module never calls HA services itself; it only
    DECIDES. conversation.py (native hass access) runs the action. Keeps the one-
    brain/HA-is-hands separation and means Roux needs only READ access to HA.
  - NO_MATCH is a first-class action value: tool_choice="required" forces SOME
    call, so the model needs a legal way to say "no device fits" (e.g. a mangled
    STT name) -> /voice/think turns that into a clean spoken decline, not a guess.
"""
from __future__ import annotations

import json
import os
import time
import logging

import httpx

logger = logging.getLogger("roux")

# Ensure .env (HA_TOKEN/HA_URL) is loaded even if shared.llm hasn't imported yet.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"))
except Exception:
    pass

# --- config -----------------------------------------------------------------
# Read lazily (via _ha_url()/_ha_token()) so a token added after import is seen.
CATALOG_TTL = 120  # seconds; refresh the device list at most this often


def _ha_url() -> str:
    return os.environ.get("HA_URL", "http://homeassistant.local:8123")


def _ha_token() -> str:
    return os.environ.get("HA_TOKEN", "")  # empty -> static catalog

# Domains the puck is allowed to drive (read+control). Excludes sensors/etc.
CONTROLLABLE_DOMAINS = {
    "light", "switch", "fan", "cover", "lock", "media_player", "input_boolean",
}

# Universal on/off/toggle services that apply to most of the above, plus a few
# domain-specifics worth having day one. The model picks domain.service; the
# enum guarantees it's a real one. (Brightness/color/percentage = a later pass.)
SERVICES_BY_DOMAIN = {
    "light": ["light.turn_on", "light.turn_off", "light.toggle"],
    "switch": ["switch.turn_on", "switch.turn_off", "switch.toggle"],
    "fan": ["fan.turn_on", "fan.turn_off", "fan.toggle"],
    "cover": ["cover.open_cover", "cover.close_cover", "cover.stop_cover"],
    "lock": ["lock.lock", "lock.unlock"],
    "input_boolean": ["input_boolean.turn_on", "input_boolean.turn_off", "input_boolean.toggle"],
    "media_player": [
        "media_player.turn_on", "media_player.turn_off",
        "media_player.media_play", "media_player.media_pause",
        "media_player.media_stop", "media_player.volume_up",
        "media_player.volume_down", "media_player.volume_mute",
    ],
}

# Static fallback catalog (measured from core.entity_registry 2026-06-14) so the
# action path works/tests even before the HA token is wired. Live fetch overrides.
_STATIC_CATALOG = [
    {"entity_id": "light.lab_shelves", "name": "Lab Shelves", "domain": "light"},
    {"entity_id": "light.lab_shelves_main", "name": "Lab Shelves Main", "domain": "light"},
    {"entity_id": "light.wled", "name": "WLED", "domain": "light"},
    {"entity_id": "light.chamber_1_ceiling_fan_main_light", "name": "Chamber 1 Ceiling Fan Light", "domain": "light"},
    {"entity_id": "fan.chamber_1_ceiling_fan_main_fan", "name": "Chamber 1 Ceiling Fan", "domain": "fan"},
    {"entity_id": "switch.lab_shelves_nightlight", "name": "Lab Shelves Nightlight", "domain": "switch"},
    {"entity_id": "switch.wled_nightlight", "name": "WLED Nightlight", "domain": "switch"},
    {"entity_id": "media_player.kitchen_speaker", "name": "Kitchen Speaker", "domain": "media_player"},
    {"entity_id": "media_player.living_room_tv", "name": "Living Room TV", "domain": "media_player"},
    {"entity_id": "media_player.theater_tv", "name": "Theater TV", "domain": "media_player"},
    {"entity_id": "media_player.basement", "name": "Basement", "domain": "media_player"},
]

_catalog_cache: list[dict] = []
_catalog_fetched_at: float = 0.0


async def fetch_catalog(force: bool = False) -> list[dict]:
    """Return the controllable-entity catalog, live from HA if a token is set
    (cached for CATALOG_TTL), else the static snapshot. Never raises."""
    global _catalog_cache, _catalog_fetched_at
    now = time.time()
    if not force and _catalog_cache and (now - _catalog_fetched_at) < CATALOG_TTL:
        return _catalog_cache
    token = _ha_token()
    if not token:
        _catalog_cache = list(_STATIC_CATALOG)
        _catalog_fetched_at = now
        return _catalog_cache
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{_ha_url()}/api/states",
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            states = r.json()
        catalog = []
        for s in states:
            eid = s.get("entity_id", "")
            domain = eid.split(".", 1)[0] if "." in eid else ""
            if domain in CONTROLLABLE_DOMAINS:
                catalog.append({
                    "entity_id": eid,
                    "name": (s.get("attributes", {}) or {}).get("friendly_name", eid),
                    "domain": domain,
                    "state": s.get("state", ""),
                })
        if catalog:
            _catalog_cache = catalog
            _catalog_fetched_at = now
            logger.info(f"HA catalog refreshed: {len(catalog)} controllable entities")
        return _catalog_cache or list(_STATIC_CATALOG)
    except Exception as e:
        logger.warning(f"HA catalog fetch failed ({e}); using last/static")
        return _catalog_cache or list(_STATIC_CATALOG)


def _build_action_tool(catalog: list[dict]) -> dict:
    """Build the tool schema whose enums constrain the model to REAL services +
    entities (plus NO_MATCH). llama-server's tool-use path forces a valid call."""
    domains_present = {e["domain"] for e in catalog}
    services: list[str] = []
    for d in domains_present:
        services.extend(SERVICES_BY_DOMAIN.get(d, []))
    entity_ids = [e["entity_id"] for e in catalog] + ["NO_MATCH"]
    return {
        "type": "function",
        "function": {
            "name": "control_device",
            "description": (
                "Control ONE smart-home device. Choose the entity_id that best "
                "matches what the user named, and the service for what they want "
                "done. service domain MUST match entity domain (e.g. light.* for a "
                "light.* entity). If no entity reasonably matches, set "
                "entity_id='NO_MATCH'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "enum": sorted(set(services))},
                    "entity_id": {"type": "string", "enum": entity_ids},
                    "data": {
                        "type": "object",
                        "description": "Optional service params; usually {}.",
                    },
                },
                "required": ["service", "entity_id"],
            },
        },
    }


def _catalog_for_prompt(catalog: list[dict]) -> str:
    return "\n".join(
        f"  {e['entity_id']}  (\"{e['name']}\", {e['domain']})" for e in catalog
    )


def _parse_action_json(text: str) -> dict | None:
    """Robustly pull the first JSON object out of a tool-call arg string —
    tolerates reasoning-model trailing text / 'Extra data'."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fall back to first balanced {...}
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


async def decide_action(user_text: str, catalog: list[dict] | None = None) -> dict | None:
    """Roux picks an exact action for a device command. Returns
    {"service","entity_id","data"} (entity_id may be 'NO_MATCH'), or None on error."""
    from shared.llm import llm_chat

    if catalog is None:
        catalog = await fetch_catalog()
    if not catalog:
        return None

    tool = _build_action_tool(catalog)
    system = (
        "You are Roux's smart-home control resolver. Map the user's spoken command "
        "to exactly one device action by calling control_device. Use ONLY entities "
        "from this list:\n" + _catalog_for_prompt(catalog) +
        "\nPick the single best entity. If the user clearly named no device on the "
        "list (e.g. garbled speech), use entity_id='NO_MATCH'."
        # Qwen3 thinking-disable: device resolution needs no chain-of-thought, and
        # the reasoning budget otherwise eats the token budget -> truncated JSON.
        " /no_think"
    )
    try:
        resp = await llm_chat(
            "companion",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            tools=[tool],
            tool_choice="required",
            # v53 spends up to ~256 tokens REASONING before the tool call; give the
            # JSON ample room AFTER that (512 still truncated ~20% -> false NO_MATCH).
            # max_tokens is a ceiling only — it stops early at the tool call, no latency
            # cost. (Server reasoning-budget can't be disabled per-request on this build.)
            max_tokens=1024,
            timeout=30,
            temperature=0.0,
        )
        if not resp.success or not resp.text:
            logger.warning("decide_action: empty/failed LLM response")
            return None
        action = _parse_action_json(resp.text)
        if not action or "service" not in action or "entity_id" not in action:
            logger.warning(f"decide_action: malformed action {action!r}")
            return None
        action.setdefault("data", {})
        return action
    except Exception as e:
        logger.warning(f"decide_action failed: {e}")
        return None
