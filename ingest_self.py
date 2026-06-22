"""
ingest_self.py — Bootstrap the RouxYou RAG memory index.

Run this once after cloning, and again whenever you make significant
changes to the codebase.

Usage:
    python ingest_self.py

What it does:
    1. Walks the RouxYou_Public source tree
    2. Extracts docstrings, comments, and function signatures from .py files
    3. Embeds each chunk using nomic-embed-text via Ollama
    4. Writes memory/memories/index.faiss + metadata.json

The resulting index lets the agents (and Roux) answer questions about
their own architecture, look up past patterns, and self-reference.

Requirements:
    - Ollama running at the host configured in config.yaml
    - nomic-embed-text pulled: ollama pull nomic-embed-text
"""

import sys
import time
from pathlib import Path

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "memory"))

# ---------------------------------------------------------------------------
# Pre-flight check
# ---------------------------------------------------------------------------

def check_ollama(ollama_host: str, embed_model: str) -> bool:
    """Verify Ollama is reachable and the embedding model is available."""
    import requests
    try:
        r = requests.get(f"{ollama_host}/api/tags", timeout=5)
        if r.status_code != 200:
            print(f"  [X] Ollama returned HTTP {r.status_code}")
            return False
        models = [m["name"] for m in r.json().get("models", [])]
        # nomic-embed-text appears as "nomic-embed-text:latest" in some versions
        matches = [m for m in models if embed_model.split(":")[0] in m]
        if not matches:
            print(f"  [X] Model '{embed_model}' not found.")
            print(f"    Available models: {', '.join(models) or 'none'}")
            print(f"    Fix: ollama pull {embed_model}")
            return False
        print(f"  [OK] Ollama reachable, using: {matches[0]}")
        return True
    except Exception as e:
        print(f"  [X] Cannot reach Ollama at {ollama_host}: {e}")
        print("    Fix: make sure Ollama is running (ollama serve)")
        return False


# ---------------------------------------------------------------------------
# Directories to ingest
# ---------------------------------------------------------------------------

# Files in these directories will be indexed for RAG self-knowledge.
# Add or remove entries here if you extend the codebase.
INGEST_DIRS = [
    PROJECT_ROOT / "shared",
    PROJECT_ROOT / "gateway",
    PROJECT_ROOT / "orchestrator",
    PROJECT_ROOT / "coder",
    PROJECT_ROOT / "worker",
    PROJECT_ROOT / "memory",
    PROJECT_ROOT / "services" / "roux",
    PROJECT_ROOT / "services" / "watchtower",
]

# Individual files at root level worth indexing
INGEST_FILES = [
    PROJECT_ROOT / "config.py",
    PROJECT_ROOT / "dashboard.py",
]

# Skip these even if found under an ingested directory
SKIP_PATTERNS = [
    "__pycache__", ".git", "venv", "staging", "archive",
    "memories", "logs", "state",
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("RouxYou RAG Bootstrap")
    print("=" * 60)

    # Load config
    try:
        from config import CONFIG
        ollama_host = CONFIG.OLLAMA_HOST
        embed_model = CONFIG.MODEL_EMBED
    except Exception as e:
        print(f"\n[X] Could not load config.yaml: {e}")
        print("  Make sure you've copied config.example.yaml → config.yaml")
        sys.exit(1)

    print(f"\nOllama host : {ollama_host}")
    print(f"Embed model : {embed_model}")
    print(f"Index path  : memory/memories/index.faiss\n")

    # Pre-flight
    print("Checking Ollama...")
    if not check_ollama(ollama_host, embed_model):
        sys.exit(1)

    # Load memory system
    try:
        from memory.vectorstore import MemoryStore
        from memory.ingest import ingest_file
    except ImportError as e:
        print(f"\n[X] Could not import memory modules: {e}")
        print("  Run: pip install faiss-cpu numpy")
        sys.exit(1)

    store = MemoryStore()
    print(f"Index loaded — {store.count} existing chunks\n")

    # Collect all .py and .md files to ingest
    to_ingest = []

    for directory in INGEST_DIRS:
        if not directory.exists():
            print(f"  Skipping (not found): {directory.relative_to(PROJECT_ROOT)}")
            continue
        for f in sorted(directory.rglob("*.py")):
            if any(skip in str(f) for skip in SKIP_PATTERNS):
                continue
            to_ingest.append(f)
        for f in sorted(directory.rglob("*.md")):
            if any(skip in str(f) for skip in SKIP_PATTERNS):
                continue
            to_ingest.append(f)

    for f in INGEST_FILES:
        if f.exists():
            to_ingest.append(f)

    # Deduplicate
    to_ingest = list(dict.fromkeys(to_ingest))

    print(f"Found {len(to_ingest)} files to index\n")

    # Ingest
    total_chunks = 0
    total_files = 0
    errors = []

    for i, file_path in enumerate(to_ingest, 1):
        rel = file_path.relative_to(PROJECT_ROOT)
        try:
            chunks = ingest_file(file_path, store,
                                 project="rouxyou",
                                 source_type="auto",
                                 dedupe=True)
            if chunks > 0:
                total_chunks += chunks
                total_files += 1
                print(f"  [{i:3d}/{len(to_ingest)}] {rel}  (+{chunks} chunks)")
            else:
                print(f"  [{i:3d}/{len(to_ingest)}] {rel}  (skipped — already indexed)")
        except Exception as e:
            errors.append((str(rel), str(e)))
            print(f"  [{i:3d}/{len(to_ingest)}] {rel}  [X] ERROR: {e}")

    # Summary
    print("\n" + "=" * 60)
    print(f"Done.")
    print(f"  Files processed : {total_files}")
    print(f"  Chunks added    : {total_chunks}")
    print(f"  Index size now  : {store.count} chunks")
    if errors:
        print(f"\n  Errors ({len(errors)}):")
        for fname, err in errors[:10]:
            print(f"    {fname}: {err}")
    print("=" * 60)

    if total_chunks == 0 and store.count == 0:
        print("\n[!]  No chunks were added and the index is empty.")
        print("   Check that Ollama is running and nomic-embed-text is pulled.")
        sys.exit(1)

    print("\n[OK] RAG index ready. Start the system with: python launch.py")


if __name__ == "__main__":
    main()
