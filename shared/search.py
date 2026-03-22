"""
Search Helper — Unified web search with provider abstraction
=============================================================
Supports: searxng | duckduckgo | none

Usage:
    from shared.search import web_search
    results = await web_search("Python async patterns")
    # Returns: [{"title": ..., "url": ..., "snippet": ...}, ...]
    # Returns: [] if provider is "none" or provider is unavailable
"""

import asyncio
import logging
from typing import List, Dict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import CONFIG

logger = logging.getLogger("search")


async def web_search(query: str, max_results: int = 5) -> List[Dict]:
    """
    Perform a web search using the configured provider.
    Always returns a list of dicts with keys: title, url, snippet.
    Returns [] on any failure — never raises.
    """
    provider = CONFIG.SEARCH_PROVIDER.lower()

    if provider == "none" or not provider:
        logger.debug("Web search disabled (provider=none)")
        return []

    if provider == "searxng":
        return await _search_searxng(query, max_results)

    if provider == "duckduckgo":
        return await _search_duckduckgo(query, max_results)

    logger.warning(f"Unknown search provider: {provider}. Set search.provider in config.yaml.")
    return []


def web_search_sync(query: str, max_results: int = 5) -> List[Dict]:
    """Synchronous wrapper for use in non-async contexts."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're inside an async context — run in executor
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, web_search(query, max_results))
                return future.result(timeout=30)
        else:
            return loop.run_until_complete(web_search(query, max_results))
    except Exception as e:
        logger.warning(f"web_search_sync failed: {e}")
        return []


async def _search_searxng(query: str, max_results: int) -> List[Dict]:
    if not CONFIG.SEARXNG_URL:
        logger.warning("SearXNG provider selected but searxng_url is not set in config.yaml")
        return []

    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            params = {"q": query, "format": "json", "categories": "general,it"}
            headers = {"User-Agent": "RouxYou/1.0"}
            async with session.get(
                f"{CONFIG.SEARXNG_URL}/search",
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"SearXNG returned {resp.status}")
                    return []
                data = await resp.json()
                results = []
                for hit in data.get("results", [])[:max_results]:
                    results.append({
                        "title": hit.get("title", ""),
                        "url": hit.get("url", ""),
                        "snippet": hit.get("content", "")[:500],
                    })
                return results
    except Exception as e:
        logger.warning(f"SearXNG search failed: {e}")
        return []


async def _search_duckduckgo(query: str, max_results: int) -> List[Dict]:
    try:
        import asyncio as _asyncio
        loop = _asyncio.get_running_loop()

        def _ddg_sync():
            try:
                try:
                    from ddgs import DDGS
                except ImportError:
                    from duckduckgo_search import DDGS
                with DDGS() as ddgs:
                    hits = list(ddgs.text(query, max_results=max_results))
                    return [
                        {
                            "title": h.get("title", ""),
                            "url": h.get("href", ""),
                            "snippet": h.get("body", "")[:500],
                        }
                        for h in hits
                    ]
            except ImportError:
                logger.warning(
                    "duckduckgo-search not installed. "
                    "Run: pip install duckduckgo-search"
                )
                return []

        results = await loop.run_in_executor(None, _ddg_sync)
        return results

    except Exception as e:
        logger.warning(f"DuckDuckGo search failed: {e}")
        return []


def search_available() -> bool:
    """Quick check — is web search configured and likely functional?"""
    provider = CONFIG.SEARCH_PROVIDER.lower()
    if provider == "none":
        return False
    if provider == "searxng":
        return bool(CONFIG.SEARXNG_URL)
    if provider == "duckduckgo":
        try:
            import duckduckgo_search  # noqa
            return True
        except ImportError:
            return False
    return False
