"""
Memory Agent — FAISS-backed episodic memory service
Port: 8004
"""

import sys
from pathlib import Path

# Robust path setup — works regardless of working directory
_HERE = Path(__file__).parent
_PROJECT_ROOT = _HERE.parent
for _p in [str(_PROJECT_ROOT), str(_HERE)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import FastAPI, HTTPException
import uvicorn
from typing import Dict, Any, Optional
from pydantic import BaseModel

from shared.logger import get_logger
from shared.lifecycle import register_process
from shared.communication import Message, get_communicator
from config import CONFIG

try:
    from memory.vectorstore import MemoryStore
    from memory.ingest import ingest_text
except ImportError as e:
    print(f"ERROR: Could not import FAISS modules: {e}")
    print("Make sure vectorstore.py and ingest.py are in memory/")
    sys.exit(1)

PORT = CONFIG.PORT_MEMORY


class MemoryAgent:
    def __init__(self):
        self.logger = get_logger("memory")
        self.comm = get_communicator("memory")
        self.logger.info("Memory Agent initializing...")
        try:
            self.store = MemoryStore()
            self.logger.info(f"FAISS vectorstore loaded: {self.store.count} memories")
        except Exception as e:
            self.logger.error(f"Failed to initialize FAISS store: {e}")
            raise

    def add_memory(self, content: str, source: str = "conversation",
                   project: str = None, metadata: Dict = None) -> Dict:
        try:
            chunks = ingest_text(content, source=source, store=self.store, project=project)
            self.logger.info(f"Ingested {chunks} chunks (total: {self.store.count})")
            return {"success": True, "chunks_added": chunks,
                    "total_memories": self.store.count}
        except Exception as e:
            self.logger.error(f"Failed to add memory: {e}")
            return {"success": False, "error": str(e)}

    def query_memories(self, query: str, k: int = 5, project: str = None) -> Dict:
        try:
            results = self.store.query(query, k=k)
            memories = [{
                "content": r.get("text", ""),
                "source": r.get("source", "unknown"),
                "project": r.get("project"),
                "distance": r.get("distance"),
                "metadata": r.get("metadata", {}),
            } for r in results]
            return {"success": True, "query": query,
                    "results": memories, "count": len(memories)}
        except Exception as e:
            self.logger.error(f"Failed to query memories: {e}")
            return {"success": False, "error": str(e), "results": []}

    def get_stats(self) -> Dict:
        return {"success": True, "total_memories": self.store.count,
                "index_type": "FAISS", "status": "online"}

    def reload_index(self) -> Dict:
        try:
            self.store = MemoryStore()
            return {"success": True, "total_memories": self.store.count}
        except Exception as e:
            return {"success": False, "error": str(e)}


app = FastAPI(title="RouxYou Memory Agent")
memory_agent = MemoryAgent()


@app.get("/health")
async def health():
    return {"status": "healthy", "agent": "memory",
            "memories": memory_agent.store.count}


@app.get("/stats")
async def stats():
    return memory_agent.get_stats()


@app.post("/message")
async def handle_message(message: Message):
    try:
        if message.task in ["query_memories", "recall", "search", "search_memory"]:
            query = message.data.get("query")
            if not query:
                raise HTTPException(status_code=400, detail="Missing 'query' in data")
            return memory_agent.query_memories(query, message.data.get("k", 5),
                                               message.data.get("project"))

        elif message.task in ["add_memory", "remember", "memorize", "save_memory"]:
            content = message.data.get("content")
            if not content:
                raise HTTPException(status_code=400, detail="Missing 'content' in data")
            return memory_agent.add_memory(content, message.data.get("source", "conversation"),
                                           message.data.get("project"), message.data.get("metadata"))

        elif message.task == "get_stats":
            return memory_agent.get_stats()

        elif message.task == "reload_index":
            return memory_agent.reload_index()

        raise HTTPException(status_code=400, detail=f"Unknown task: {message.task}")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    register_process("memory-agent")
    memory_agent.logger.info(f"Starting Memory Agent on port {PORT}...")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
