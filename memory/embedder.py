"""Embedding wrapper using Ollama — derives URL from central CONFIG."""
import requests
import numpy as np
from pathlib import Path
import sys

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import CONFIG

OLLAMA_BASE_URL = CONFIG.OLLAMA_HOST
EMBEDDING_MODEL = CONFIG.MODEL_EMBED


def get_embedding(text: str) -> np.ndarray:
    """Get embedding vector for text using Ollama.

    Ollama 0.18+ deprecated /api/embeddings; uses /api/embed with `input` (str or list)
    instead of `prompt`, returning `embeddings: [[...]]` instead of `embedding: [...]`.
    """
    response = requests.post(
        f"{OLLAMA_BASE_URL}/api/embed",
        json={"model": EMBEDDING_MODEL, "input": text},
    )
    response.raise_for_status()
    data = response.json()
    if "embeddings" in data and data["embeddings"]:
        return np.array(data["embeddings"][0], dtype=np.float32)
    if "embedding" in data:
        return np.array(data["embedding"], dtype=np.float32)
    raise RuntimeError(f"Unexpected embed response shape: {list(data.keys())}")


def get_embeddings_batch(texts: list) -> np.ndarray:
    """Get embeddings for multiple texts."""
    return np.vstack([get_embedding(t) for t in texts])
