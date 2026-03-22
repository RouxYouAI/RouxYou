"""
RAG HTTP API — FAISS vector store with era filtering
=====================================================
HTTP bridge so RouxYou agents can query and write to the
shared knowledge base.

ACCESS POLICY:
  READ:  Era-filtered by default. Only returns memories from approved eras.
         Legacy memories available via include_all_eras=true.
  WRITE: Namespaced to source="mission_control", era="rouxyou".

ERA SYSTEM:
  Default eras (always visible):
    rouxyou       — RouxYou codebase + agent writes
    claude_core   — Claude calibration & core docs
    conversations — Conversation exports
    security      — Security research

  Legacy eras (hidden by default):
    agent_zero, trade_bot, websocket_agent, legacy_projects,
    network_logs, legacy

Port: configured via CONFIG.PORT_RAG
"""

import sys
from pathlib import Path
from collections import Counter

_HERE = Path(__file__).parent
_PROJECT_ROOT = _HERE.parent
for _p in [str(_PROJECT_ROOT), str(_HERE)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Set

from memory.vectorstore import MemoryStore
from memory.ingest import ingest_text
from shared.logger import get_logger
from shared.lifecycle import register_process
from config import CONFIG

logger = get_logger("rag_api")
store = MemoryStore()

PORT = CONFIG.PORT_RAG

app = FastAPI(title="RouxYou RAG Memory API")

DEFAULT_ERAS: Set[str] = {"rouxyou", "claude_core", "conversations", "security"}
ALL_ERAS: Set[str] = DEFAULT_ERAS | {
    "agent_zero", "trade_bot", "websocket_agent",
    "legacy_projects", "network_logs", "legacy"
}


class QueryRequest(BaseModel):
    query: str
    k: int = 5
    include_all_eras: bool = False
    extra_eras: Optional[List[str]] = None


class WriteRequest(BaseModel):
    content: str
    context: Optional[str] = None


class QueryResult(BaseModel):
    text: str
    source: str
    similarity: float
    speaker: Optional[str] = None
    source_type: Optional[str] = None
    project: Optional[str] = None
    era: Optional[str] = None


@app.get("/health")
async def health():
    era_counts = Counter(m.get("era", "unknown") for m in store.metadata)
    default_visible = sum(c for e, c in era_counts.items() if e in DEFAULT_ERAS)
    return {
        "status": "ok",
        "total_memories": store.count,
        "default_visible": default_visible,
        "filtered_out": store.count - default_visible,
        "service": "rag_api",
        "port": PORT,
    }


@app.post("/query")
async def query_memories(request: QueryRequest):
    try:
        if request.include_all_eras:
            allowed_eras = ALL_ERAS
        else:
            allowed_eras = set(DEFAULT_ERAS)
            if request.extra_eras:
                allowed_eras.update(request.extra_eras)

        fetch_k = min(request.k * 8, max(store.count, 1))
        raw_results = store.query(request.query, k=fetch_k)

        if not raw_results:
            return {"results": [], "count": 0, "query": request.query,
                    "eras_searched": sorted(allowed_eras)}

        filtered = []
        skipped = 0
        for r in raw_results:
            era = r.get("era", "legacy")
            if era in allowed_eras:
                filtered.append(r)
                if len(filtered) >= request.k:
                    break
            else:
                skipped += 1

        formatted = []
        for r in filtered:
            source_val = r.get("source", "unknown")
            formatted.append({
                "text": r.get("text", "")[:500],
                "source": str(source_val) if isinstance(source_val, dict) else source_val,
                "similarity": round(r.get("similarity", 0), 3),
                "speaker": r.get("speaker"),
                "source_type": r.get("source_type"),
                "project": r.get("project"),
                "era": r.get("era"),
            })

        top_sim = formatted[0]["similarity"] if formatted else 0
        logger.info(
            f"RAG query: '{request.query[:50]}' → {len(formatted)}/{request.k} results "
            f"(skipped {skipped} legacy, top sim: {top_sim:.1%})"
        )

        return {"results": formatted, "count": len(formatted),
                "skipped_legacy": skipped, "query": request.query,
                "eras_searched": sorted(allowed_eras)}

    except Exception as e:
        logger.error(f"RAG query error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/write")
async def write_memory(request: WriteRequest):
    try:
        source_label = f"mission_control:{request.context or 'task'}"
        chunks_added = ingest_text(text=request.content, source=source_label,
                                   store=store, source_type="mission_control", project=None)
        for m in store.metadata[-chunks_added:]:
            m["era"] = "rouxyou"
        store._save()
        logger.info(f"RAG write: {chunks_added} chunk(s) (era: rouxyou, ctx: {request.context or 'task'})")
        return {"success": True, "chunks_added": chunks_added,
                "source": source_label, "era": "rouxyou"}
    except Exception as e:
        logger.error(f"RAG write error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats")
async def memory_stats():
    era_counts = Counter(m.get("era", "unknown") for m in store.metadata)
    source_types = Counter(m.get("source_type", "unknown") for m in store.metadata)
    default_visible = sum(c for e, c in era_counts.items() if e in DEFAULT_ERAS)
    return {
        "total_memories": store.count,
        "default_visible": default_visible,
        "filtered_out": store.count - default_visible,
        "by_era": dict(era_counts.most_common()),
        "by_source_type": dict(source_types.most_common()),
        "default_eras": sorted(DEFAULT_ERAS),
        "all_eras": sorted(ALL_ERAS),
    }


@app.get("/eras")
async def list_eras():
    era_counts = Counter(m.get("era", "unknown") for m in store.metadata)
    descriptions = {
        "rouxyou": "RouxYou codebase & agent memories",
        "claude_core": "Claude calibration & core documents",
        "conversations": "Conversation history",
        "security": "Security research",
        "agent_zero": "Legacy Agent Zero project",
        "trade_bot": "Legacy crypto trading bot",
        "websocket_agent": "Legacy WebSocket communicator",
        "legacy_projects": "Other archived projects",
        "network_logs": "Router/modem diagnostic logs",
        "legacy": "Uncategorized legacy content",
    }
    return {"eras": [{"era": e, "count": c, "default": e in DEFAULT_ERAS,
                      "description": descriptions.get(e, "Unknown era")}
                     for e, c in era_counts.most_common()]}


@app.get("/audit/mission_control")
async def audit_mission_control():
    mc = [{"index": i, "text": m.get("text", "")[:200], "source": m.get("source", ""),
            "added_at": m.get("added_at", ""), "era": m.get("era", "unknown")}
          for i, m in enumerate(store.metadata) if m.get("source_type") == "mission_control"]
    return {"count": len(mc), "memories": mc}


@app.on_event("startup")
async def startup_event():
    register_process("rag_api")
    era_counts = Counter(m.get("era", "unknown") for m in store.metadata)
    default_visible = sum(c for e, c in era_counts.items() if e in DEFAULT_ERAS)
    logger.info(f"RAG API on port {PORT} — {store.count} total, {default_visible} default visible")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
