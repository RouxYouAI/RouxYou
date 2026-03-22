"""Configuration for the RAG memory system."""
from pathlib import Path
import sys

# Always resolve relative to this file — no fragile relative imports
_HERE = Path(__file__).parent
_PROJECT_ROOT = _HERE.parent

# Ensure project root is importable
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import CONFIG

# Paths
PROJECT_ROOT   = _HERE
MEMORIES_DIR   = _HERE / "memories"
RAW_MEMORIES_DIR = MEMORIES_DIR / "raw"
INDEX_PATH     = MEMORIES_DIR / "index.faiss"
METADATA_PATH  = MEMORIES_DIR / "metadata.json"

# Embedding settings — derive Ollama URL from central CONFIG
OLLAMA_BASE_URL  = CONFIG.OLLAMA_HOST
EMBEDDING_MODEL  = CONFIG.MODEL_EMBED
EMBEDDING_DIM    = 768

# Chunking settings
CHUNK_SIZE    = 500   # tokens (approximate)
CHUNK_OVERLAP = 50

# Ensure directories exist
MEMORIES_DIR.mkdir(exist_ok=True)
RAW_MEMORIES_DIR.mkdir(exist_ok=True)
