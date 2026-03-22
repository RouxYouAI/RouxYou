"""
CONVERSATION MANAGER
Persistent, multi-conversation chat history with archival and title generation.

Storage layout:
  state/conversations/
    index.json              <- [{id, title, created_at, updated_at, msg_count, pinned}, ...]
    <uuid>.json             <- full message array for one conversation

Active conversation ID is tracked in index.json["active_id"].
Both the dashboard and companion.py use this module as the single source of truth.
"""

import json
import time
import uuid
import aiohttp
from pathlib import Path
from typing import Optional, Dict, Any, List

import sys, os
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import CONFIG

STATE_DIR = Path(__file__).parent.parent / "state" / "conversations"
INDEX_FILE = STATE_DIR / "index.json"
STATE_DIR.mkdir(parents=True, exist_ok=True)


def _load_index() -> Dict:
    if INDEX_FILE.exists():
        try:
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    data = {"active_id": None, "conversations": data}
                if "active_id" not in data:
                    data["active_id"] = None
                if "conversations" not in data:
                    data["conversations"] = []
                return data
        except:
            pass
    return {"active_id": None, "conversations": []}

def _save_index(index: Dict):
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)

def _conv_file(conv_id: str) -> Path:
    return STATE_DIR / f"{conv_id}.json"


def create_conversation(title: str = "New conversation") -> str:
    index = _load_index()
    conv_id = uuid.uuid4().hex[:12]
    now = time.time()
    entry = {
        "id": conv_id,
        "title": title,
        "created_at": now,
        "updated_at": now,
        "msg_count": 0,
        "pinned": False,
    }
    index["conversations"].insert(0, entry)
    index["active_id"] = conv_id
    _save_index(index)
    with open(_conv_file(conv_id), "w", encoding="utf-8") as f:
        json.dump([], f)
    return conv_id

def get_active_conversation_id() -> Optional[str]:
    index = _load_index()
    if index["active_id"] and _conv_file(index["active_id"]).exists():
        return index["active_id"]
    return create_conversation()

def set_active_conversation(conv_id: str) -> bool:
    index = _load_index()
    if not any(c["id"] == conv_id for c in index["conversations"]):
        return False
    index["active_id"] = conv_id
    _save_index(index)
    return True

def list_conversations(limit: int = 50) -> List[Dict]:
    index = _load_index()
    convs = index["conversations"]
    pinned = sorted([c for c in convs if c.get("pinned")], key=lambda c: c.get("updated_at", 0), reverse=True)
    unpinned = sorted([c for c in convs if not c.get("pinned")], key=lambda c: c.get("updated_at", 0), reverse=True)
    return (pinned + unpinned)[:limit]

def search_conversations(query: str, limit: int = 20) -> List[Dict]:
    """Search conversations by title (case-insensitive substring match)."""
    query_lower = query.lower()
    index = _load_index()
    results = []
    for conv in index["conversations"]:
        if query_lower in conv.get("title", "").lower():
            results.append(conv)
            if len(results) >= limit:
                break
    return results

def delete_conversation(conv_id: str) -> bool:
    index = _load_index()
    index["conversations"] = [c for c in index["conversations"] if c["id"] != conv_id]
    if index["active_id"] == conv_id:
        index["active_id"] = None
    _save_index(index)
    f = _conv_file(conv_id)
    if f.exists():
        f.unlink()
    return True

def pin_conversation(conv_id: str, pinned: bool = True) -> bool:
    index = _load_index()
    for conv in index["conversations"]:
        if conv["id"] == conv_id:
            conv["pinned"] = pinned
            _save_index(index)
            return True
    return False

def _load_messages(conv_id: str) -> List[Dict]:
    f = _conv_file(conv_id)
    if f.exists():
        try:
            with open(f, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except:
            pass
    return []

def _save_messages(conv_id: str, messages: List[Dict]):
    with open(_conv_file(conv_id), "w", encoding="utf-8") as f:
        json.dump(messages, f, indent=2)

def add_message(role: str, content: str, metadata: Dict = None, conv_id: str = None) -> None:
    if conv_id is None:
        conv_id = get_active_conversation_id()
    messages = _load_messages(conv_id)
    messages.append({
        "role": role,
        "content": content,
        "timestamp": time.time(),
        "metadata": metadata or {}
    })
    _save_messages(conv_id, messages)
    index = _load_index()
    for conv in index["conversations"]:
        if conv["id"] == conv_id:
            conv["updated_at"] = time.time()
            conv["msg_count"] = len(messages)
            if conv["title"] == "New conversation" and role == "user":
                conv["title"] = content[:60].strip()
                if len(content) > 60:
                    conv["title"] += "..."
            break
    _save_index(index)

def get_messages(limit: int = 50, conv_id: str = None) -> List[Dict]:
    if conv_id is None:
        conv_id = get_active_conversation_id()
    return _load_messages(conv_id)[-limit:]

def get_all_messages(conv_id: str = None) -> List[Dict]:
    if conv_id is None:
        conv_id = get_active_conversation_id()
    return _load_messages(conv_id)

async def generate_title(conv_id: str = None) -> str:
    if conv_id is None:
        conv_id = get_active_conversation_id()
    messages = _load_messages(conv_id)
    if not messages:
        return "Empty conversation"
    preview_msgs = messages[:6]
    preview_text = ""
    for msg in preview_msgs:
        role = "User" if msg["role"] == "user" else "AI"
        preview_text += f"{role}: {msg['content'][:150]}\n"
    prompt = f"Generate a short, descriptive title (max 8 words) for this conversation. Return ONLY the title text, nothing else.\n\nConversation:\n{preview_text}\n\nTitle:"
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": CONFIG.MODEL_ROUTER,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 30}
            }
            async with session.post(
                f"{CONFIG.OLLAMA_HOST}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    title = result.get("response", "").strip().strip('"\'')
                    title = title.split("\n")[0].strip()
                    if len(title) > 80:
                        title = title[:77] + "..."
                    if title:
                        index = _load_index()
                        for conv in index["conversations"]:
                            if conv["id"] == conv_id:
                                conv["title"] = title
                                break
                        _save_index(index)
                        return title
    except Exception:
        pass
    for msg in messages:
        if msg["role"] == "user":
            fallback = msg["content"][:50]
            return fallback + "..." if len(msg["content"]) > 50 else fallback
    return "Untitled conversation"

def update_title(conv_id: str, title: str) -> bool:
    index = _load_index()
    for conv in index["conversations"]:
        if conv["id"] == conv_id:
            conv["title"] = title
            _save_index(index)
            return True
    return False

def clear_conversation(conv_id: str = None):
    if conv_id is None:
        conv_id = get_active_conversation_id()
    _save_messages(conv_id, [])
    index = _load_index()
    for conv in index["conversations"]:
        if conv["id"] == conv_id:
            conv["msg_count"] = 0
            break
    _save_index(index)

# Compatibility wrappers
def add_user_message(content: str, intent: str = None):
    add_message("user", content, {"intent": intent} if intent else {})

def add_assistant_message(content: str, intent: str = None, executed: bool = False):
    metadata = {}
    if intent: metadata["intent"] = intent
    if executed: metadata["executed"] = executed
    add_message("assistant", content, metadata)

def get_recent_messages(limit: int = 20) -> List[Dict]:
    return get_messages(limit)


async def generate_title(conv_id: str = None) -> str:
    """Use the router LLM to generate a concise title for a conversation."""
    if conv_id is None:
        conv_id = get_active_conversation_id()

    messages = _load_messages(conv_id)
    if not messages:
        return "Empty conversation"

    preview_msgs = messages[:6]
    preview_text = ""
    for msg in preview_msgs:
        role = "User" if msg["role"] == "user" else "AI"
        preview_text += f"{role}: {msg['content'][:150]}\n"

    prompt = f"""Generate a short, descriptive title (max 8 words) for this conversation.
Return ONLY the title text, nothing else. No quotes, no explanation.

Conversation:
{preview_text}

Title:"""

    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": CONFIG.MODEL_ROUTER,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 30}
            }
            async with session.post(
                f"{CONFIG.OLLAMA_HOST}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    title = result.get("response", "").strip().strip('"\'')
                    title = title.split("\n")[0].strip()
                    if len(title) > 80:
                        title = title[:77] + "..."
                    if title:
                        index = _load_index()
                        for conv in index["conversations"]:
                            if conv["id"] == conv_id:
                                conv["title"] = title
                                break
                        _save_index(index)
                        return title
    except Exception:
        pass

    # Fallback: first user message
    for msg in messages:
        if msg["role"] == "user":
            fallback = msg["content"][:50]
            return fallback + "..." if len(msg["content"]) > 50 else fallback

    return "Untitled conversation"


def migrate_existing_history():
    """
    One-time migration: import chat_history.json and conversation.json
    into the new conversation system.
    """
    old_files = [
        Path(__file__).parent.parent / "state" / "chat_history.json",
        Path(__file__).parent.parent / "state" / "conversation.json",
    ]

    for old_file in old_files:
        if not old_file.exists():
            continue

        try:
            with open(old_file, "r", encoding="utf-8") as f:
                old_messages = json.load(f)

            if not old_messages or not isinstance(old_messages, list):
                continue

            index = _load_index()
            if any("migrated" in c.get("title", "") for c in index["conversations"]):
                continue

            conv_id = uuid.uuid4().hex[:12]
            now = time.time()

            title = "Migrated chat history"
            for msg in old_messages:
                if msg.get("role") == "user":
                    title = msg["content"][:60]
                    if len(msg["content"]) > 60:
                        title += "..."
                    break

            entry = {
                "id": conv_id,
                "title": title,
                "created_at": old_messages[0].get("timestamp", now) if old_messages else now,
                "updated_at": old_messages[-1].get("timestamp", now) if old_messages else now,
                "msg_count": len(old_messages),
                "pinned": False,
            }

            index["conversations"].append(entry)
            _save_index(index)
            _save_messages(conv_id, old_messages)
            old_file.rename(old_file.with_suffix(".json.bak"))

        except Exception:
            continue
