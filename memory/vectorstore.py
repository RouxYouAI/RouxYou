"""FAISS vector store management."""
import json
import faiss
import numpy as np
from datetime import datetime
from pathlib import Path
from memory.mem_config import INDEX_PATH, METADATA_PATH, EMBEDDING_DIM
from memory.embedder import get_embedding


class MemoryStore:
    """Vector store for the RAG memory system."""

    def __init__(self):
        self.index = None
        self.metadata: list = []
        self._load_or_create()

    def _load_or_create(self):
        if INDEX_PATH.exists() and METADATA_PATH.exists():
            self.index = faiss.read_index(str(INDEX_PATH))
            with open(METADATA_PATH, "r", encoding="utf-8") as f:
                self.metadata = json.load(f)
        else:
            self.index = faiss.IndexFlatIP(EMBEDDING_DIM)
            self.metadata = []

    def _save(self):
        faiss.write_index(self.index, str(INDEX_PATH))
        with open(METADATA_PATH, "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=2, default=str)

    def _normalize(self, vec: np.ndarray) -> np.ndarray:
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec

    def add(self, text: str, source: str = None, extra_metadata: dict = None):
        embedding = get_embedding(text).reshape(1, -1)
        embedding = self._normalize(embedding)
        self.index.add(embedding)
        meta = {"text": text, "source": source,
                "added_at": datetime.now().isoformat(), **(extra_metadata or {})}
        self.metadata.append(meta)
        self._save()
        return len(self.metadata) - 1

    def add_batch(self, chunks: list):
        from embedder import get_embeddings_batch
        texts = [c["text"] for c in chunks]
        embeddings = get_embeddings_batch(texts)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        embeddings = embeddings / np.where(norms > 0, norms, 1)
        self.index.add(embeddings)
        for chunk in chunks:
            meta = {"text": chunk["text"], "source": chunk.get("source"),
                    "added_at": datetime.now().isoformat(),
                    **{k: v for k, v in chunk.items() if k not in ("text", "source")}}
            self.metadata.append(meta)
        self._save()

    def query(self, query_text: str, k: int = 5) -> list:
        if self.index.ntotal == 0:
            return []
        query_embedding = get_embedding(query_text).reshape(1, -1)
        query_embedding = self._normalize(query_embedding)
        similarities, indices = self.index.search(query_embedding, min(k, self.index.ntotal))
        results = []
        for sim, idx in zip(similarities[0], indices[0]):
            if idx < len(self.metadata):
                result = self.metadata[idx].copy()
                result["similarity"] = float(max(0, min(1, sim)))
                results.append(result)
        return results

    def list_recent(self, n: int = 10) -> list:
        return self.metadata[-n:][::-1]

    @property
    def count(self) -> int:
        return self.index.ntotal
