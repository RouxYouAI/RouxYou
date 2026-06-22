"""Ingestion pipeline for the RouxYou RAG memory system."""
import re
import ast
import hashlib
from pathlib import Path
from datetime import datetime
from memory.mem_config import RAW_MEMORIES_DIR, CHUNK_SIZE, CHUNK_OVERLAP, MEMORIES_DIR
from memory.vectorstore import MemoryStore

HASH_FILE = MEMORIES_DIR / "ingested_hashes.txt"


def get_content_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def load_ingested_hashes() -> set:
    if HASH_FILE.exists():
        return set(HASH_FILE.read_text().strip().split("\n"))
    return set()


def save_hash(content_hash: str):
    with open(HASH_FILE, "a") as f:
        f.write(content_hash + "\n")


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list:
    words = text.split()
    words_per_chunk = int(chunk_size / 1.3)
    words_overlap = int(overlap / 1.3)
    chunks = []
    start = 0
    while start < len(words):
        end = start + words_per_chunk
        chunk = " ".join(words[start:end])
        if chunk.strip():
            chunks.append(chunk)
        start = end - words_overlap
        if start >= len(words) - words_overlap:
            break
    return chunks if chunks else ([text] if text.strip() else [])


def parse_conversation(text: str) -> list:
    pattern = r"(?:^|\n)((?:Human|Assistant|User|AI):\s*)"
    parts = re.split(pattern, text, flags=re.IGNORECASE)
    if len(parts) <= 1:
        return [{"speaker": "unknown", "content": text.strip()}]
    turns = []
    i = 1
    while i < len(parts) - 1:
        speaker = parts[i].strip().rstrip(":").lower()
        content = parts[i + 1].strip()
        if content:
            if speaker in ("human", "user"):
                speaker = "user"
            elif speaker in ("assistant", "ai"):
                speaker = "assistant"
            turns.append({"speaker": speaker, "content": content})
        i += 2
    return turns


def extract_python_context(file_path: Path) -> list:
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    extracted = []
    try:
        tree = ast.parse(content)
        if ast.get_docstring(tree):
            extracted.append({"type": "docstring", "context": "module",
                               "content": ast.get_docstring(tree)})
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and ast.get_docstring(node):
                extracted.append({"type": "docstring", "context": f"class {node.name}",
                                   "content": ast.get_docstring(node)})
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and ast.get_docstring(node):
                extracted.append({"type": "docstring", "context": f"function {node.name}",
                                   "content": ast.get_docstring(node)})
    except SyntaxError:
        pass
    for pattern in [r"#\s*(TODO|FIXME|NOTE|IMPORTANT|WARNING)[:.]?\s*(.+)",
                    r"#\s*(Why|Because|Reason)[:.]?\s*(.+)"]:
        for match in re.finditer(pattern, content, re.IGNORECASE):
            extracted.append({"type": "comment", "context": match.group(1).upper(),
                               "content": match.group(2).strip()})
    return extracted


def ingest_file(file_path: Path, store: MemoryStore, source_type: str = "auto",
                project: str = None, dedupe: bool = True) -> int:
    suffix = file_path.suffix.lower()
    chunks_added = 0
    ingested_hashes = load_ingested_hashes() if dedupe else set()

    if source_type == "auto":
        source_type = "code_python" if suffix == ".py" else "document"

    if source_type == "code_python":
        extracted = extract_python_context(file_path)
        for item in extracted:
            content = f"[{item['context']}] {item['content']}"
            h = get_content_hash(content)
            if dedupe and h in ingested_hashes:
                continue
            store.add(text=content, source=str(file_path),
                      extra_metadata={"source_type": "code_comment",
                                      "code_type": item["type"],
                                      "project": project,
                                      "file_name": file_path.name,
                                      "era": "rouxyou"})
            save_hash(h)
            chunks_added += 1
    else:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        turns = parse_conversation(text)
        for turn in turns:
            for chunk in chunk_text(turn["content"]):
                h = get_content_hash(chunk)
                if dedupe and h in ingested_hashes:
                    continue
                store.add(text=chunk, source=str(file_path),
                          extra_metadata={"speaker": turn["speaker"],
                                          "source_type": source_type,
                                          "project": project,
                                          "file_name": file_path.name,
                                          "era": "rouxyou"})
                save_hash(h)
                chunks_added += 1

    return chunks_added


def ingest_directory(dir_path: Path = None, store: MemoryStore = None,
                     project: str = None, recursive: bool = False) -> dict:
    dir_path = dir_path or RAW_MEMORIES_DIR
    store = store or MemoryStore()
    supported = ["*.txt", "*.md", "*.py"]
    stats = {"files_processed": 0, "chunks_added": 0, "errors": []}

    for pattern in supported:
        glob_fn = dir_path.rglob if recursive else dir_path.glob
        for file_path in glob_fn(pattern):
            if any(skip in str(file_path) for skip in
                   ["node_modules", "__pycache__", ".git", "venv", ".env",
                    "memories/", "staging/", "archive/"]):
                continue
            try:
                chunks = ingest_file(file_path, store, project=project)
                if chunks > 0:
                    stats["files_processed"] += 1
                    stats["chunks_added"] += chunks
            except Exception as e:
                stats["errors"].append({"file": str(file_path), "error": str(e)})

    return stats


def ingest_text(text: str, source: str = "manual", store: MemoryStore = None,
                source_type: str = "conversation", project: str = None) -> int:
    store = store or MemoryStore()
    turns = parse_conversation(text)
    ingested_hashes = load_ingested_hashes()
    chunks_added = 0

    for turn in turns:
        for chunk in chunk_text(turn["content"]):
            h = get_content_hash(chunk)
            if h in ingested_hashes:
                continue
            store.add(text=chunk, source=source,
                      extra_metadata={"speaker": turn["speaker"],
                                      "source_type": source_type,
                                      "project": project,
                                      "ingested_at": datetime.now().isoformat(),
                                      "era": "rouxyou"})
            save_hash(h)
            chunks_added += 1

    return chunks_added


if __name__ == "__main__":
    store = MemoryStore()
    stats = ingest_directory(store=store)
    print(f"Ingested {stats['files_processed']} files, {stats['chunks_added']} chunks")
