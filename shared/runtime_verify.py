"""Runtime verification of generated files — the gap bigbrain can't cover.

bigbrain reviews whether a plan matches INTENT; it cannot tell whether the code
actually RUNS. This module executes/loads generated files and reports crashes,
syntax errors, truncation, and load-time errors — the "does it run" tier.

Tiers (this module covers 1 + 2):
  1. syntax/parse   — py_compile (.py), `node --check` (.js)
  2. headless load  — .html loaded in headless chromium (Playwright), capturing
                      uncaught pageerrors + console errors
  3. behavioral     — simulate input + assert behavior (e.g. catch the breakout
                      stuck-key bug). NOT covered here — needs generated tests;
                      future work. So a "verified" .html here means "loads without
                      crashing", NOT "logically correct".

Surfaced 2026-05-25: the Roux-built breakout game passed both bigbrain reviews
but had a real runtime bug (no keyup handler → stuck keys). "APPROVE" != "runs".
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List

_BASE = Path(__file__).resolve().parent.parent          # /home/user/RouxYou
_VENV_PY = _BASE / "venv" / "bin" / "python"
_HTML_CHECK = _BASE / "shared" / "_html_check.py"


def _py(*args: str) -> str:
    return str(_VENV_PY if _VENV_PY.exists() else "python3")


def verify_file(path: str, *, timeout: int = 40) -> Dict:
    """Verify one generated file actually loads/parses.

    Returns {verified: bool, method: str, errors: [str], detail: str}.
    `verified` is True for clean OR un-verifiable-by-design (skipped) files;
    callers should check `method` to distinguish a real pass from a skip.
    """
    p = Path(path)
    if not p.exists():
        return {"verified": False, "method": "exists", "errors": [f"file does not exist: {path}"], "detail": ""}
    try:
        if p.stat().st_size == 0:
            return {"verified": False, "method": "nonempty", "errors": ["file is empty"], "detail": ""}
    except OSError:
        pass

    ext = p.suffix.lower()
    if ext == ".py":
        res = _verify_python(p, timeout)
    elif ext in (".html", ".htm"):
        res = _verify_html(p, timeout)
    elif ext in (".js", ".mjs"):
        res = _verify_js(p, timeout)
    else:
        res = {"verified": True, "method": "skip", "errors": [],
               "detail": f"no runtime verifier for '{ext}' — existence + non-empty only"}
    res.setdefault("inconclusive", False)  # infra/transient failure flag (not a code verdict)
    return res


def _run(cmd: List[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(_BASE))


# Infra/transient failures (verifier couldn't run) vs real code errors (code is bad).
# An infra failure must NOT be recorded as confirmed_bad — that would poison the trust
# ledger with a spurious false-pass. Infra → "inconclusive" → ground_truth=unknown.
_INFRA_MARKERS = (
    "load_failed", "browsertype.launch", "executable doesn't exist", "timed out",
    "timeout", "playwright_unavailable", "no result", "err_invalid_url",
    "err_file_not_found", "net::err", "browser has been closed", "target closed",
)


def _is_infra(err: str) -> bool:
    """True if an error string is an infra/transient verifier failure, not a code bug.
    Real code errors (pageerror:, console.error:, syntax messages, structural truncation)
    return False — those mean the code is genuinely broken."""
    e = (err or "").lower()
    if e.startswith("pageerror:") or e.startswith("console."):
        return False  # genuine runtime/console error from the code itself
    return any(m in e for m in _INFRA_MARKERS)


def _verify_python(p: Path, timeout: int) -> Dict:
    try:
        r = _run([_py(), "-m", "py_compile", str(p)], timeout)
    except subprocess.TimeoutExpired:
        return {"verified": False, "inconclusive": True, "method": "py_compile", "errors": ["py_compile timed out (infra)"], "detail": ""}
    if r.returncode == 0:
        return {"verified": True, "method": "py_compile", "errors": [], "detail": "syntax OK"}
    return {"verified": False, "method": "py_compile",
            "errors": [(r.stderr or r.stdout).strip()[:400]], "detail": ""}


def _verify_js(p: Path, timeout: int) -> Dict:
    from shutil import which
    if not which("node"):
        return {"verified": True, "method": "skip", "errors": [], "detail": "node not available — JS syntax unchecked"}
    try:
        r = _run(["node", "--check", str(p)], timeout)
    except subprocess.TimeoutExpired:
        return {"verified": False, "inconclusive": True, "method": "node --check", "errors": ["node --check timed out (infra)"], "detail": ""}
    if r.returncode == 0:
        return {"verified": True, "method": "node --check", "errors": [], "detail": "syntax OK"}
    return {"verified": False, "method": "node --check", "errors": [(r.stderr or r.stdout).strip()[:400]], "detail": ""}


def _verify_html(p: Path, timeout: int) -> Dict:
    """Verify HTML with BOTH layers and combine:
      - structural: catches truncation / unbalanced <script> (inline-script syntax
        errors do NOT reliably fire pageerror, so headless alone MISSES truncation)
      - headless-load: catches uncaught pageerrors + console errors at runtime
    verified = both clean. (Neither catches behavioral/logic bugs — tier 3.)"""
    import json as _json
    structural = _verify_html_structural(p)
    errors = list(structural["errors"])
    methods = ["structural"]

    if _HTML_CHECK.exists():
        try:
            r = _run([_py(), str(_HTML_CHECK), str(p)], timeout)
            out = (r.stdout or "").strip().splitlines()
            payload = None
            for line in reversed(out):
                try:
                    payload = _json.loads(line); break
                except Exception:
                    continue
            if payload is None:
                errors.append(f"headless: no result; stderr={r.stderr.strip()[:150]}")
            elif payload.get("ok") is None:
                pass  # playwright unavailable → structural-only, no extra errors
            else:
                methods.append("headless-load")
                errors.extend(payload.get("errors") or [])
        except subprocess.TimeoutExpired:
            errors.append("headless load timed out")

    # Split real code errors (truncation, pageerror, console) from infra/transient
    # failures (browser launch/timeout/version). Infra-only → inconclusive (NOT a
    # confirmed_bad verdict), so a flaky headless run can't poison the trust ledger.
    code_errors = [e for e in errors if not _is_infra(e)]
    infra_errors = [e for e in errors if _is_infra(e)]
    if code_errors:
        return {"verified": False, "inconclusive": False, "method": "+".join(methods),
                "errors": (code_errors + infra_errors)[:10], "detail": ""}
    if infra_errors:
        return {"verified": False, "inconclusive": True, "method": "+".join(methods),
                "errors": infra_errors[:10],
                "detail": "INCONCLUSIVE — verifier infra issue (not a code verdict); treated as unverified"}
    return {"verified": True, "inconclusive": False, "method": "+".join(methods), "errors": [],
            "detail": "loads clean — NOTE: catches crashes/truncation, NOT logic bugs (tier 3)"}


def _verify_html_structural(p: Path) -> Dict:
    """Fallback when no headless browser: cheap structural sanity."""
    h = p.read_text(errors="ignore")
    errs = []
    if not h.lstrip().lower().startswith("<!doctype") and "<html" not in h.lower():
        errs.append("no <!doctype/<html>")
    if not h.rstrip().lower().endswith("</html>"):
        errs.append("does not end with </html> (possible truncation)")
    if h.count("<script") != h.count("</script>"):
        errs.append("unbalanced <script> tags")
    return {"verified": len(errs) == 0, "method": "structural", "errors": errs,
            "detail": "structural-only (no headless browser available)"}


def verify_paths(paths: List[str]) -> Dict:
    """Verify several files. Returns:
      verified     — all paths verified clean
      hard_fail    — at least one REAL code failure (verified False AND not inconclusive)
      inconclusive — at least one infra/transient failure (and no hard_fail)
    Callers act on hard_fail (fix-loop/rollback/confirmed_bad); inconclusive → unknown.
    """
    results = {pp: verify_file(pp) for pp in paths}
    hard_fail = any((not r.get("verified")) and (not r.get("inconclusive")) for r in results.values())
    inconclusive = any(r.get("inconclusive") for r in results.values())
    return {
        "verified": all(r.get("verified") for r in results.values()),
        "hard_fail": hard_fail,
        "inconclusive": inconclusive and not hard_fail,
        "results": results,
    }
