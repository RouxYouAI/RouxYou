"""Topic-diversity gate — stops the self-propose attractor at GENERATION time.

Why this exists (2026-05-31): roux_reflection kept minting the SAME proposal over
and over — "add a topic cooldown to stop my self-propose loop" — 16 authored
proposals, the last several near-identical (see project_self_propose_substance_finding).
draft_queue's dedup only catches an EXACT pending/drafting topic; once one completes
or gets dismissed, the same idea re-enqueues in slightly different words. The executor
doesn't help: these get dismissed, not approved, so draining approved ones never slows
the minting. The fix has to sit at GENERATION: before reflection enqueues a draft,
compare the candidate topic against what it has recently proposed and, if it's a
near-duplicate, throttle it.

How it measures similarity — and why:
  * PRIMARY = embedding COSINE via the local CPU-pinned embedder (nomic-embed-text-cpu,
    ollama). The attractor topics are PARAPHRASES — semantically near-identical, only
    loosely overlapping lexically. Empirically (validated 2026-05-31 against the real
    16 authored proposals): cosine separates the family (0.70-0.94, median 0.91) from
    unrelated proposals (0.52-0.86, median 0.60) cleanly, while a lexical Jaccard does
    NOT (family 0.09-0.26 vs unrelated 0.01-0.15 — bands overlap, no usable threshold).
    Threshold 0.80 → catches 15/16, ~2/25 false-block; a false-block only skips one
    reflection cycle, so we bias toward catching. This matches the original
    "cosine >=0.75" spec (prop_07679ffcae61).
  * The embedder is CPU-pinned (num_gpu=0), so it can't evict roux-v53 off the 16GB
    card. The reflection path already calls bigbrain, so it isn't a purely-offline path
    — an embed call is well within budget, and reflection fires at most ~12x/day.
  * FALLBACK = lexical Jaccard, used ONLY if the embedder is unreachable, so the
    watchtower cron path never hard-fails on a model being down (feedback_offline_first).
    The fallback is conservative (high threshold, fail-open-ish): in degraded mode we'd
    rather miss a dup than false-block, since the env gate + oversight still backstop.

GENERAL, not keyword-hardcoded: we compare against the author's OWN recent topics, so
whatever it fixates on gets throttled — no literal "self-propose" matching that would
fail to generalize to the next attractor. Reversible/observable: assess_topic() returns
a full dict (score, method, matched text, threshold) so the caller logs exactly why.
"""

import hashlib
import json
import math
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

_BASE = Path(__file__).parent.parent
_ACTIVE = _BASE / "state" / "proposals_active.json"
_HISTORY = _BASE / "state" / "proposals_history.json"
_DRAFT_QUEUE = _BASE / "state" / "draft_queue.json"
_EMBED_CACHE = _BASE / "state" / "topic_embed_cache.json"

# Cosine >= this against any recent same-author topic => redundant. Tuned on the real
# authored corpus (2026-05-31): family scores 0.70-0.94, unrelated 0.52-0.86; 0.80
# catches 15/16 with ~2/25 false-block. Env-tunable.
COS_THRESHOLD = float(os.environ.get("ROUX_TOPIC_DIVERSITY_THRESHOLD", "0.80"))
# Lexical fallback threshold (only used if the embedder is down). Conservative: lexical
# barely separates, so keep it high to avoid false-blocks in degraded mode.
LEX_THRESHOLD = float(os.environ.get("ROUX_TOPIC_DIVERSITY_LEX_THRESHOLD", "0.50"))
# Only compare against topics proposed within this window (older fixations age out).
LOOKBACK_H = float(os.environ.get("ROUX_TOPIC_DIVERSITY_LOOKBACK_H", "168"))  # 7 days
# Cap how many recent topics we compare against (newest first) — bounds embed time.
MAX_COMPARE = int(os.environ.get("ROUX_TOPIC_DIVERSITY_MAX", "40"))

_EMBED_MODEL = os.environ.get("ROUX_EMBED_MODEL", "nomic-embed-text-cpu")
_EMBED_URL = os.environ.get("ROUX_EMBED_URL", "http://127.0.0.1:11434/api/embeddings")
_EMBED_TIMEOUT = float(os.environ.get("ROUX_EMBED_TIMEOUT", "20"))


# ---------------------------------------------------------------- lexical (fallback)
_STOP = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for", "with",
    "is", "are", "be", "been", "being", "it", "its", "this", "that", "these",
    "those", "as", "at", "by", "from", "into", "than", "then", "so", "if", "when",
    "while", "via", "per", "no", "not", "any", "all", "each", "more", "most",
    "such", "out", "her", "his", "their", "they", "them", "she", "he", "we", "you",
    "our", "your", "can", "will", "would", "should", "could", "may", "might",
    "has", "have", "had", "do", "does", "did", "up", "down", "over", "under",
}


def significant_tokens(text: str) -> set:
    if not text:
        return set()
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) >= 3 and w not in _STOP}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b) if inter else 0.0


# ---------------------------------------------------------------- embeddings (primary)
def _embed_cache_load() -> Dict:
    try:
        with open(_EMBED_CACHE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _embed_cache_save(cache: Dict) -> None:
    try:
        # Trim to the most recent ~500 entries to bound the file.
        if len(cache) > 500:
            cache = dict(list(cache.items())[-500:])
        _EMBED_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _EMBED_CACHE.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, _EMBED_CACHE)
    except Exception:
        pass  # cache is best-effort


def _key(text: str) -> str:
    return hashlib.sha1((_EMBED_MODEL + "\x00" + text).encode("utf-8")).hexdigest()


def embed(text: str, cache: Optional[Dict] = None) -> Optional[List[float]]:
    """Return the embedding vector for `text`, or None if the embedder is unreachable.
    Uses a content-addressed disk cache so recent topics aren't re-embedded each call."""
    text = (text or "").strip()
    if not text:
        return None
    k = _key(text[:2000])
    if cache is not None and k in cache:
        return cache[k]
    try:
        req = urllib.request.Request(
            _EMBED_URL,
            data=json.dumps({"model": _EMBED_MODEL, "prompt": text[:2000]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_EMBED_TIMEOUT) as resp:
            vec = json.load(resp).get("embedding")
        if vec and cache is not None:
            cache[k] = vec
        return vec or None
    except Exception:
        return None  # embedder down -> caller falls back to lexical


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------- recent-topic gather
def _to_epoch(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _load(path: Path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def gather_recent_topics(author_substr: str = "reflection",
                         lookback_hours: float = None,
                         max_items: int = None) -> List[Dict]:
    """Recent topic strings the author has proposed/enqueued, newest first.
    Sources: draft_queue (topic) + proposals active/history (title + first desc line),
    filtered by source/author containing author_substr, within the lookback window."""
    lookback_hours = LOOKBACK_H if lookback_hours is None else lookback_hours
    max_items = MAX_COMPARE if max_items is None else max_items
    cutoff = time.time() - lookback_hours * 3600.0
    out: List[Dict] = []

    dq = _load(_DRAFT_QUEUE) or {}
    for it in (dq.get("items") or []):
        if author_substr and author_substr not in str(it.get("author", "")):
            continue
        when = _to_epoch(it.get("enqueued_at"))
        if when and when < cutoff:
            continue
        topic = (it.get("topic") or "").strip()
        if topic:
            out.append({"text": topic, "when": when, "source": "draft_queue"})

    for path, tag in ((_ACTIVE, "active"), (_HISTORY, "history")):
        data = _load(path)
        if not isinstance(data, list):
            continue
        for p in data:
            if author_substr and author_substr not in str(p.get("source", "")):
                continue
            when = _to_epoch(p.get("created_at"))
            if when and when < cutoff:
                continue
            title = (p.get("title") or "").strip()
            desc = (p.get("description") or "").strip().splitlines()
            text = (title + " " + (desc[0] if desc else "")).strip()
            if text:
                out.append({"text": text, "when": when, "source": f"proposal:{tag}"})

    out.sort(key=lambda d: d.get("when", 0), reverse=True)
    return out[:max_items]


# ---------------------------------------------------------------- the gate
def assess_topic(candidate: str,
                 recent_texts: Optional[List[str]] = None,
                 author_substr: str = "reflection",
                 threshold: float = None,
                 lookback_hours: float = None) -> Dict:
    """Assess whether `candidate` is a near-duplicate of the author's recent topics.

    Primary metric: embedding cosine. Falls back to lexical Jaccard if the embedder is
    unreachable. `recent_texts` lets tests pass an explicit comparison set; otherwise
    gather_recent_topics() is used.

    Returns {redundant, score, threshold, method, matched_text, matched_source,
             n_compared}. method ∈ {cosine, lexical_jaccard, none}.
    """
    result = {
        "redundant": False, "score": 0.0, "threshold": None, "method": "none",
        "matched_text": None, "matched_source": None, "n_compared": 0,
    }
    candidate = (candidate or "").strip()
    if not candidate:
        return result

    if recent_texts is not None:
        recents = [{"text": t, "source": "explicit"} for t in recent_texts if t]
    else:
        recents = gather_recent_topics(author_substr=author_substr,
                                       lookback_hours=lookback_hours)
    result["n_compared"] = len(recents)
    if not recents:
        return result

    # ---- primary: cosine
    cache = _embed_cache_load()
    cand_vec = embed(candidate, cache)
    if cand_vec is not None:
        thr = COS_THRESHOLD if threshold is None else threshold
        best, best_item = 0.0, None
        for item in recents:
            v = embed(item["text"], cache)
            if v is None:
                continue
            s = cosine(cand_vec, v)
            if s > best:
                best, best_item = s, item
        _embed_cache_save(cache)
        if best_item is not None:
            result.update({
                "score": round(best, 4), "threshold": thr, "method": "cosine",
                "matched_text": best_item["text"][:200],
                "matched_source": best_item.get("source"),
                "redundant": best >= thr,
            })
            return result
        # embedder gave us a candidate vec but every recent failed — fall through

    # ---- fallback: lexical Jaccard (embedder down)
    thr = LEX_THRESHOLD if threshold is None else threshold
    cand_tok = significant_tokens(candidate)
    best, best_item = 0.0, None
    for item in recents:
        s = jaccard(cand_tok, significant_tokens(item["text"]))
        if s > best:
            best, best_item = s, item
    result.update({
        "score": round(best, 4), "threshold": thr, "method": "lexical_jaccard",
        "matched_text": (best_item or {}).get("text", "")[:200] or None,
        "matched_source": (best_item or {}).get("source"),
        "redundant": best >= thr,
    })
    return result
