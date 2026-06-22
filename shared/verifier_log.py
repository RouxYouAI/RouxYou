"""
🛡️ VERIFIER LOG — Phase 39 Observability (Apr 6, 2026)
==========================================================
Append-only JSONL log of every silent semantic drift verification event.
Built so DJ can later compute false-positive / false-negative rates and
decide when Phase 2 (auto-remediation on verifier fail) is safe to ship.

Design (mirrors shared/blackbox.py):
  - Append-only file at state/verifier_log.jsonl
  - One JSON object per line for easy grep/tail/dashboard
  - Never raises — best-effort logging that can't break the verifier path
  - Tag events are appended as new lines (not mutations) so the log
    stays append-only and the audit trail is preserved

Schema for "verification" records:
  {
    "ts": 1775500000.123,             # epoch float
    "iso": "2026-04-06T10:35:00",     # human-readable
    "kind": "verification",
    "task_id": 1234,
    "query": "create a file at...",
    "intent": "execute",              # or chat/clarify/etc
    "intent_spec": {...},             # parsed constraints from extraction
    "plan_summary": "write_file -> /Desktop/foo.txt; verify_fix",
    "verdict": "pass" | "fail" | "uncertain" | "skipped",
    "reason": "...",
    "missing_constraints": [...],
    "verifier_model": "claude-sonnet-4-6",
    "extraction_model": "informed_chat",
    "latency_ms": 1234,               # verify_task wall-clock
    "tagged": null,                   # filled by tag_verification()
  }

Schema for "tag" records (appended later by DJ):
  {
    "ts": ...,
    "iso": "...",
    "kind": "tag",
    "task_id": 1234,                  # references original verification
    "tag": "true_positive" | "false_positive" | "true_negative" | "false_negative",
    "note": "optional human note"
  }

The compute_stats() function joins tag records back to their parent
verifications by task_id when computing FP/FN rates.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

_BASE = Path(__file__).parent.parent
LOG_PATH = _BASE / "state" / "verifier_log.jsonl"

_VALID_TAGS = {"true_positive", "false_positive", "true_negative", "false_negative"}


def _ensure_dir():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def record_verification(
    task_id: Any,
    query: str,
    intent: Optional[str],
    intent_spec: Optional[Dict],
    plan_steps: Optional[List[Dict]],
    verdict_dict: Dict,
    latency_ms: int,
) -> bool:
    """Append a verification event to the log. Best-effort, never raises.

    Returns True on success, False on any failure (which is silently logged
    to stderr — the verifier path itself is wrapped to ignore this).
    """
    try:
        _ensure_dir()
        record = {
            "ts": time.time(),
            "iso": datetime.now().isoformat(timespec="seconds"),
            "kind": "verification",
            "task_id": task_id,
            "query": (query or "")[:500],
            "intent": intent,
            "intent_spec": intent_spec,
            "plan_summary": _summarize_plan(plan_steps),
            "verdict": verdict_dict.get("verdict", "uncertain"),
            "reason": (verdict_dict.get("reason") or "")[:500],
            "missing_constraints": verdict_dict.get("missing_constraints", []) or [],
            "verifier_model": verdict_dict.get("verifier_model", ""),
            "extraction_model": (intent_spec or {}).get("_extraction_model", ""),
            "latency_ms": int(latency_ms or 0),
            "tagged": None,
        }
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return True
    except Exception as e:
        # Best-effort. Verifier path must not break on logging errors.
        try:
            import sys
            print(f"verifier_log.record_verification failed: {e}", file=sys.stderr)
        except Exception:
            pass
        return False


def tag_verification(task_id: Any, tag: str, note: str = "") -> bool:
    """Append a tag event for an existing verification.

    Returns False if tag is not in _VALID_TAGS or write fails.
    Tags are append-only — calling this twice for the same task_id
    keeps both records, and compute_stats() uses the most recent one.
    """
    if tag not in _VALID_TAGS:
        return False
    try:
        _ensure_dir()
        record = {
            "ts": time.time(),
            "iso": datetime.now().isoformat(timespec="seconds"),
            "kind": "tag",
            "task_id": task_id,
            "tag": tag,
            "note": (note or "")[:500],
        }
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return True
    except Exception:
        return False


def read_recent(n: int = 20, kind: Optional[str] = None) -> List[Dict]:
    """Read the most recent N records from the log.

    If `kind` is given, only records of that kind are returned (e.g.
    kind="verification" to skip tags). Records are returned in
    chronological order (oldest first within the returned slice).
    """
    if not LOG_PATH.exists():
        return []
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    out = []
    # Walk from the end so we can stop early once we have N matches
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if kind and rec.get("kind") != kind:
            continue
        out.append(rec)
        if len(out) >= n:
            break
    return list(reversed(out))


def compute_stats(window_hours: Optional[float] = 24.0) -> Dict[str, Any]:
    """Compute aggregate verifier stats over the last `window_hours`.

    Pass window_hours=None to scan all-time.

    Returns:
      {
        "window_hours": 24,
        "total": int,                          # total verifications in window
        "by_verdict": {pass, fail, uncertain, skipped},
        "fail_rate": float,                    # fail / (pass + fail)
        "mean_latency_ms": int,
        "max_latency_ms": int,
        "top_missing_constraints": [(constraint, count), ...],
        "tags": {true_positive, false_positive, true_negative, false_negative},
        "fp_rate": float | None,               # FP / (FP + TP) if any tagged
        "fn_rate": float | None,               # FN / (FN + TN) if any tagged
        "tagged_total": int,
      }
    """
    if not LOG_PATH.exists():
        return _empty_stats(window_hours)
    cutoff = (time.time() - window_hours * 3600) if window_hours else 0.0

    verifications: List[Dict] = []
    tags_by_task: Dict[Any, str] = {}  # latest tag wins

    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("ts", 0)
                if ts < cutoff:
                    continue
                if rec.get("kind") == "verification":
                    verifications.append(rec)
                elif rec.get("kind") == "tag":
                    tid = rec.get("task_id")
                    if tid is not None:
                        tags_by_task[tid] = rec.get("tag", "")
    except Exception:
        return _empty_stats(window_hours)

    if not verifications:
        return _empty_stats(window_hours)

    by_verdict = {"pass": 0, "fail": 0, "uncertain": 0, "skipped": 0}
    latencies = []
    missing_counter: Dict[str, int] = {}
    for v in verifications:
        verdict = v.get("verdict", "uncertain")
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
        lat = v.get("latency_ms", 0) or 0
        if lat > 0:
            latencies.append(lat)
        for c in v.get("missing_constraints", []) or []:
            key = (c if isinstance(c, str) else json.dumps(c))[:120]
            missing_counter[key] = missing_counter.get(key, 0) + 1

    total = len(verifications)
    pass_n = by_verdict["pass"]
    fail_n = by_verdict["fail"]
    fail_rate = (fail_n / (pass_n + fail_n)) if (pass_n + fail_n) > 0 else 0.0

    tag_counts = {t: 0 for t in _VALID_TAGS}
    for tag in tags_by_task.values():
        if tag in tag_counts:
            tag_counts[tag] += 1

    tp = tag_counts["true_positive"]
    fp = tag_counts["false_positive"]
    tn = tag_counts["true_negative"]
    fn = tag_counts["false_negative"]
    fp_rate = (fp / (fp + tp)) if (fp + tp) > 0 else None
    fn_rate = (fn / (fn + tn)) if (fn + tn) > 0 else None

    top_missing = sorted(missing_counter.items(), key=lambda kv: kv[1], reverse=True)[:10]

    return {
        "window_hours": window_hours,
        "total": total,
        "by_verdict": by_verdict,
        "fail_rate": round(fail_rate, 3),
        "mean_latency_ms": int(sum(latencies) / len(latencies)) if latencies else 0,
        "max_latency_ms": max(latencies) if latencies else 0,
        "top_missing_constraints": top_missing,
        "tags": tag_counts,
        "fp_rate": round(fp_rate, 3) if fp_rate is not None else None,
        "fn_rate": round(fn_rate, 3) if fn_rate is not None else None,
        "tagged_total": sum(tag_counts.values()),
    }


def _summarize_plan(plan_steps: Optional[List[Dict]]) -> str:
    """One-line summary of plan steps for the log entry."""
    if not plan_steps:
        return ""
    parts = []
    for s in plan_steps[:6]:
        if not isinstance(s, dict):
            continue
        action = s.get("action", "?")
        details = (s.get("details", "") or "")[:60]
        parts.append(f"{action}({details})" if details else action)
    if len(plan_steps) > 6:
        parts.append(f"...+{len(plan_steps) - 6}")
    return "; ".join(parts)


def _empty_stats(window_hours: Optional[float]) -> Dict[str, Any]:
    return {
        "window_hours": window_hours,
        "total": 0,
        "by_verdict": {"pass": 0, "fail": 0, "uncertain": 0, "skipped": 0},
        "fail_rate": 0.0,
        "mean_latency_ms": 0,
        "max_latency_ms": 0,
        "top_missing_constraints": [],
        "tags": {t: 0 for t in _VALID_TAGS},
        "fp_rate": None,
        "fn_rate": None,
        "tagged_total": 0,
    }
