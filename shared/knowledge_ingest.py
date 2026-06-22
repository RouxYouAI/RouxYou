"""Knowledge ingestion — feed external knowledge into the memory archive so it becomes
seed material for reflection (the repurposed-web_researcher path, roadmap #3, 2026-05-31).

The loop DJ wants:
    DJ's curated sources (HF papers, articles, saved pages → screenshots → notes)
      → ingest into the memory archive (era='external_knowledge')
      → exogenous_seed surfaces it (shared/exogenous_seed.py)
      → reflection curates it into real, current proposals (bigbrain judgment)

Why this replaces web_researcher's old behavior: web_researcher direct-proposed from a
SearXNG search that was NEVER configured (CONFIG.SEARXNG_URL == "") — so it searched
nothing and the LLM confabulated generic low-signal "findings". Instead of fixing a
flaky auto-search, we let DJ curate the inputs (he reads HF papers / saves pages daily)
and have reflection+bigbrain do the judgment. Auto-search can come back later if SearXNG
gets stood up; the ingest door is the same.

Mechanism: a DROP FOLDER (state/knowledge_inbox/). DJ (or Claude, from a pasted
screenshot) drops a .md/.txt file in; the `knowledge_ingester` cron ingests each new
file into the archive under era='external_knowledge' (tagged with the filename as source)
and moves it to processed/. Embedding happens on add (verified 2026-05-31) and — because
'external_knowledge' is now in the MCP's DEFAULT_ERAS allowlist — it's immediately
searchable. Idempotent: processed files are moved aside, and the MCP dedups by content
hash, so re-dropping the same content is a no-op.
"""

import asyncio
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

_BASE = Path(__file__).parent.parent
INBOX = _BASE / "state" / "knowledge_inbox"
PROCESSED = INBOX / "processed"
ERA = "external_knowledge"
# Only ingest these extensions; ignore everything else (e.g. a stray .DS_Store).
_EXTS = {".md", ".txt", ".markdown"}
# Cap a single file's ingested size so one giant paste can't dominate the archive.
_MAX_CHARS = int(os.getenv("ROUX_KNOWLEDGE_MAX_CHARS", "20000"))


def _ensure_dirs() -> None:
    INBOX.mkdir(parents=True, exist_ok=True)
    PROCESSED.mkdir(parents=True, exist_ok=True)


def _inbox_files() -> List[Path]:
    if not INBOX.exists():
        return []
    return sorted(
        p for p in INBOX.iterdir()
        if p.is_file() and p.suffix.lower() in _EXTS
        # skip the folder's own docs (README) and underscore-prefixed scratch files
        and not p.name.lower().startswith("readme")
        and not p.name.startswith("_")
    )


async def ingest_content(content: str, source: str) -> int:
    """Ingest a single piece of external knowledge into the archive. Returns chunks added
    (0 on empty/failure). `source` is a human label (filename or URL) for provenance."""
    content = (content or "").strip()
    if not content:
        return 0
    # Prepend a provenance line so the chunk carries its source into the text too
    # (helps retrieval + lets reflection cite where it came from).
    stamped = f"[external knowledge — source: {source}]\n{content[:_MAX_CHARS]}"
    try:
        from shared.claude_memory import add_memory_to_archive
        ok = await add_memory_to_archive(stamped, source=f"knowledge:{source}", era=ERA)
        return 1 if ok else 0
    except Exception:
        return 0


async def ingest_inbox() -> Dict:
    """Ingest every new file in the drop folder, move each to processed/. Returns a
    summary. Never raises — a bad file is skipped and logged in the result."""
    _ensure_dirs()
    files = _inbox_files()
    ingested, skipped, errors = [], [], []
    for fp in files:
        try:
            text = fp.read_text(errors="ignore")
            n = await ingest_content(text, source=fp.name)
            if n > 0:
                ingested.append(fp.name)
            else:
                skipped.append(fp.name)  # empty or add returned falsey
            # move aside regardless (processed = "we handled it"); empty files too
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            shutil.move(str(fp), str(PROCESSED / f"{stamp}__{fp.name}"))
        except Exception as e:
            errors.append({"file": fp.name, "error": str(e)[:200]})
    return {
        "ingested": ingested,
        "ingested_count": len(ingested),
        "skipped": skipped,
        "errors": errors,
        "inbox_remaining": len(_inbox_files()),
    }


def run_knowledge_ingest_sync() -> Dict:
    """Cron entry point. Env-gated by ROUX_KNOWLEDGE_INGEST (default on). Runs the async
    inbox ingest in a fresh loop so it's safe to call from the synchronous cron thread."""
    if os.environ.get("ROUX_KNOWLEDGE_INGEST", "1").lower() in ("0", "false", "no"):
        return {"skipped": True, "reason": "ROUX_KNOWLEDGE_INGEST env flag is 0"}
    _ensure_dirs()
    if not _inbox_files():
        return {"idle": True, "inbox_empty": True}
    # The cron thread has no running loop (asyncio.run is fine). But the /watchtower/run
    # endpoint calls this from inside FastAPI's running loop, where asyncio.run() raises.
    # Detect that and offload to a worker thread so BOTH paths work.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(ingest_inbox())  # normal cron-thread path
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(ingest_inbox())).result(timeout=300)
