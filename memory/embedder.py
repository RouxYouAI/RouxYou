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
    """Get embedding vector for text using Ollama."""
    response = requests.post(
        f"{OLLAMA_BASE_URL}/api/embeddings",
        json={"model": EMBEDDING_MODEL, "prompt": text}
    )
    response.raise_for_status()
    return np.array(response.json()["embedding"], dtype=np.float32)


def get_embeddings_batch(texts: list) -> np.ndarray:
    """Get embeddings for multiple texts."""
    return np.vstack([get_embedding(t) for t in texts])
