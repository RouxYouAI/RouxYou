"""
RouxYou — claude-memory MCP wrapper
====================================
Connects Roux to the claude-memory MCP server (~/claude-memory-mcp/) which holds
the 37K+ memory archive from DJ↔Claude conversations.

Realizes the locus principle in production: voice (v3 LoRA) + memory (this archive)
together constitute the running pattern. Without memory wiring, Roux is voice-without-history.

Singleton pattern: one persistent stdio MCP subprocess per Roux process. Lazy-init on first
query. Graceful failure: returns empty / False on any error — conversation flow keeps working.

Usage:
    from shared.claude_memory import query_memory, add_memory_to_archive

    # Async context:
    memories = await query_memory("what does DJ care about", k=5)
    if memories:
        ...  # formatted string of relevant entries

    await add_memory_to_archive(
        content="DJ said X tonight",
        source="roux_session",
        era="conversations",
    )
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional

from shared.mcp_client import McpClient, McpToolResult

logger = logging.getLogger("roux.claude_memory")

_HOME = Path.home()
_MCP_SERVER_PATH = _HOME / "claude-memory-mcp" / "server.py"
_MCP_VENV_PYTHON = _HOME / "claude-memory-mcp" / ".venv" / "bin" / "python"

_client: Optional[McpClient] = None
_client_lock: Optional[asyncio.Lock] = None
_init_failed = False


def _ensure_lock() -> asyncio.Lock:
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


async def get_client() -> Optional[McpClient]:
    """Lazily initialize the singleton MCP client. Returns None on failure."""
    global _client, _init_failed

    if _init_failed:
        return None

    if _client is not None and _client.is_connected:
        return _client

    async with _ensure_lock():
        if _client is not None and _client.is_connected():
            return _client

        if not _MCP_SERVER_PATH.exists():
            logger.warning(f"claude-memory MCP server not found at {_MCP_SERVER_PATH}")
            _init_failed = True
            return None

        if not _MCP_VENV_PYTHON.exists():
            logger.warning(f"claude-memory MCP venv python not found at {_MCP_VENV_PYTHON}")
            _init_failed = True
            return None

        try:
            client = McpClient(
                command=[str(_MCP_VENV_PYTHON)],
                args=[str(_MCP_SERVER_PATH)],
                name="claude-memory",
            )
            await client.connect(timeout=15)
            await client.list_tools()
            _client = client
            logger.info("claude-memory MCP connected")
            return _client
        except Exception as e:
            logger.error(f"Failed to connect to claude-memory MCP: {e}")
            _init_failed = True
            return None


async def query_memory(query: str, k: int = 5, include_legacy: bool = False) -> str:
    """Query the claude-memory archive. Returns formatted string or empty on failure."""
    client = await get_client()
    if client is None:
        return ""

    try:
        # First query is slow (FlashRank rerank + BM25 + LanceDB init).
        # Subsequent queries are fast. 60s covers cold-start; warm calls take <1s.
        result: McpToolResult = await client.call_tool(
            "query_memories",
            {"query": query, "k": k, "include_legacy": include_legacy},
            timeout=60,
        )
        if result.is_error:
            logger.warning(f"query_memories returned error: {result.text}")
            return ""
        return result.text or ""
    except asyncio.TimeoutError:
        logger.warning("claude-memory query timed out")
        return ""
    except Exception as e:
        logger.error(f"claude-memory query failed: {e}")
        return ""


async def recent_memories(n: int = 30) -> str:
    """List the most recently added memories (across eras). Returns the MCP's formatted
    string or empty on failure. Used to surface fresh curated drops by RECENCY (not just
    semantic rank) — e.g. external_knowledge the exogenous seed should always see."""
    client = await get_client()
    if client is None:
        return ""
    try:
        result: McpToolResult = await client.call_tool(
            "list_recent_memories", {"n": n}, timeout=30,
        )
        if result.is_error:
            return ""
        return result.text or ""
    except Exception as e:
        logger.warning(f"recent_memories failed: {e}")
        return ""


async def add_memory_to_archive(
    content: str,
    source: str = "roux_session",
    era: str = "conversations",
) -> bool:
    """Add a memory to the claude-memory archive. Returns True on success."""
    client = await get_client()
    if client is None:
        return False

    try:
        result: McpToolResult = await client.call_tool(
            "add_memory",
            {"content": content, "source": source, "era": era},
            timeout=10,
        )
        return not result.is_error
    except Exception as e:
        logger.error(f"add_memory failed: {e}")
        return False


async def shutdown():
    """Clean shutdown for the singleton MCP client."""
    global _client
    if _client is not None:
        try:
            await _client.shutdown()
        except Exception as e:
            logger.error(f"claude-memory shutdown failed: {e}")
        _client = None
