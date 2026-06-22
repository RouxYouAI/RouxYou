"""Behavioral verification (tier-3, deterministic smoke) — exercise the running app.

runtime_verify (tiers 1-2) answers "does it load without crashing." This answers the
next question: "does it crash when you USE it?" — by simulating input (keys, clicks) in
headless chromium and capturing errors thrown DURING interaction. Catches the class that
loads clean but breaks on use (buggy event handlers).

NOT correctness ("does it do the right thing") — that needs spec-derived assertions, a
separate, less-reliable layer (LLM-generated tests). This smoke is deterministic + reliable.

Advisory by design for now: a new, fallible verification layer earns trust (measured in the
trust ledger) before it's allowed to hard-block — same discipline as tier graduation.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Dict

from shared.runtime_verify import _is_infra  # reuse infra-vs-code classifier

_BASE = Path(__file__).resolve().parent.parent
_VENV_PY = _BASE / "venv" / "bin" / "python"
_CHECK = _BASE / "shared" / "_behavioral_check.py"


def _py() -> str:
    return str(_VENV_PY if _VENV_PY.exists() else "python3")


def behavioral_check(path: str, *, timeout: int = 60) -> Dict:
    """Interaction-smoke a single HTML file. Returns:
      {applicable, clean, inconclusive, interactions, errors, detail}
    Only applies to .html/.htm; everything else → applicable=False (skip).
    `clean` = no real code errors during interaction; `inconclusive` = infra/transient.
    """
    p = Path(path)
    if p.suffix.lower() not in (".html", ".htm"):
        return {"applicable": False, "clean": True, "inconclusive": False,
                "interactions": 0, "errors": [], "detail": "non-HTML — behavioral smoke skipped"}
    if not _CHECK.exists():
        return {"applicable": False, "clean": True, "inconclusive": False,
                "interactions": 0, "errors": [], "detail": "behavioral checker missing"}
    try:
        r = subprocess.run([_py(), str(_CHECK), str(p)], capture_output=True, text=True,
                           timeout=timeout, cwd=str(_BASE))
    except subprocess.TimeoutExpired:
        return {"applicable": True, "clean": False, "inconclusive": True,
                "interactions": 0, "errors": ["behavioral smoke timed out (infra)"], "detail": ""}
    payload = None
    for line in reversed((r.stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line); break
        except Exception:
            continue
    if payload is None:
        return {"applicable": True, "clean": False, "inconclusive": True,
                "interactions": 0, "errors": [f"no result; stderr={r.stderr.strip()[:150]}"], "detail": ""}
    if payload.get("ok") is None:   # playwright unavailable
        return {"applicable": False, "clean": True, "inconclusive": True,
                "interactions": 0, "errors": ["playwright unavailable"], "detail": "skipped"}

    inter = payload.get("interaction_errors") or []
    code_errs = [e for e in inter if not _is_infra(e)]
    infra_errs = [e for e in inter if _is_infra(e)]
    if code_errs:
        return {"applicable": True, "clean": False, "inconclusive": False,
                "interactions": payload.get("interactions", 0), "errors": code_errs[:10],
                "detail": "crashed DURING interaction (loads clean but breaks on use)"}
    if infra_errs:
        return {"applicable": True, "clean": False, "inconclusive": True,
                "interactions": payload.get("interactions", 0), "errors": infra_errs[:10], "detail": "infra"}
    return {"applicable": True, "clean": True, "inconclusive": False,
            "interactions": payload.get("interactions", 0), "errors": [],
            "detail": f"survived {payload.get('interactions',0)} interactions clean — NOTE: smoke only, not correctness"}
