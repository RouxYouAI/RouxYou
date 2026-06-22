"""Path B citation check — programmatic post-response audit of v3 chat output
for unanchored fact-shaped claims (numbers, paths, prop_IDs, port mentions).

Background: Path C (prompt-side citation discipline) was insufficient because
v3 (Ministral-3-8B) is below the capability floor documented in the Princeton
continual-harness paper for self-policing complex instructions. v3 agrees with
discipline rules when asked but confabulates specific numbers/paths during
generation. The fix has to be programmatic, not prompt-based — same architectural
lesson as the asymmetric-trust / external-teacher pattern.

This module extracts claim-tokens from a generated response, checks whether
each appears in the grounding context (retrieved RAG + conversation history +
static anchors), and produces a footer flagging the unanchored ones.

MVP design choices (2026-05-24):
- Footer-style annotation, not inline rewriting. Less risk of mangling response.
- Token extraction is conservative — only fact-shaped tokens, not every number.
- Substring match (case-insensitive). Could be tightened to word-boundary later.
- Caps the flagged-claims list at 10 to avoid wall-of-text footers on confab-heavy responses.

Future iterations:
- Inline annotation (replace fab claim with [unanchored: <claim>] markup)
- Embedding-based fuzzy match instead of substring
- Differentiate "low-confidence fact" from "explicitly hedged claim" (parse for
  hedge phrases like "I think", "probably", "[no external citation]" that
  pre-mark uncertainty)
"""
from __future__ import annotations

import re
from typing import List, Dict, Tuple

# Tokens we consider fact-shaped enough to require grounding.
# Tuned conservatively — false negatives are fine (a confab slips through);
# false positives are bad (flagging legitimate grounded claims as fab).

# Numeric token: integers with comma-grouping (e.g. "6,593") OR 3+ digit integers
# OR decimals OR percentages. Single/double-digit numbers excluded — too noisy
# (would catch "I have 3 options", "step 2 of 4", etc.).
_NUM_PATTERN = re.compile(r"""
    (?<!\w)                             # word boundary
    (?:
        \d{1,3}(?:,\d{3})+              # 6,593 / 16,673 (comma-grouped)
        | \d{3,}(?:\.\d+)?              # 8001 / 4096.5 (3+ digits)
        | \d+\.\d+                      # 3.14 (any decimal)
        | \d+%                          # 84%
    )
    (?!\w)
""", re.VERBOSE)

# Absolute path tokens
_PATH_PATTERN = re.compile(r"(?:/(?:home|mnt|opt|usr|var|tmp|etc)/[A-Za-z0-9_./\-]+)")

# Relative path tokens — known Roux top-level dirs followed by subpath.
# Catches things like `services/coder/coder.py` or `shared/authoring.py` that
# v3 confabulates as if they were real files. Matches the pattern
# `shared/authoring.py::validate_evidence_citations` already uses.
_REL_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_/])(?:shared|state|services|governance|orchestrator|coder|"
    r"worker|memory|gateway|tests|bin|scripts|docs|calibration|skills|logs)"
    r"/[A-Za-z0-9_./\-]+(?:\.[A-Za-z0-9_]+)"
)

# Bare filenames with recognized extensions (e.g. "config.py", "Q4_K_observer.py").
# Lowercased start to skip sentence-cased prose. Conservative on extensions to
# avoid noise (don't catch every word ending with a period + letters).
_BARE_FILE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_/])[A-Za-z][A-Za-z0-9_]*\."
    r"(?:py|md|json|jsonl|yaml|yml|txt|sh|toml|cfg|conf|log)\b"
)

# Proposal IDs as used by RouxYou
_PROPID_PATTERN = re.compile(r"\bprop_[a-f0-9]{12}\b")

# "port NNNN" or ":NNNN" (in service-port context)
_PORT_PATTERN = re.compile(r"\b(?:port[\s:]+)?(\d{4,5})\b")

# Phase-style labels (e.g., "Phase 26", "phase 19") — v3 invents these
_PHASE_PATTERN = re.compile(r"\bPhase\s+\d+\b", re.IGNORECASE)

# Date-shaped (yyyy-mm-dd or "Feb 21 2026" style) — v3 invents specific dates
_DATE_PATTERN = re.compile(
    r"\b(?:20\d{2}-\d{2}-\d{2}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+20\d{2})\b",
    re.IGNORECASE,
)


def _extract_claims(text: str) -> List[Tuple[str, str]]:
    """Extract fact-shaped tokens. Returns list of (claim_text, claim_type) tuples,
    deduplicated by lowercased token (first occurrence wins — earlier patterns
    in the list assign the kind label).

    Dedup is cross-kind (Bug 1 fix 2026-05-24): without this, the same token
    like '8007' got flagged twice — once as 'number' and once as 'port' — which
    inflated the audit count and the footer noise."""
    seen = set()  # lowercased tokens already seen
    out: List[Tuple[str, str]] = []
    for pattern, kind in [
        (_PATH_PATTERN, "path"),
        (_REL_PATH_PATTERN, "path"),
        (_PROPID_PATTERN, "prop_id"),
        (_BARE_FILE_PATTERN, "filename"),
        (_PHASE_PATTERN, "phase"),
        (_DATE_PATTERN, "date"),
        (_NUM_PATTERN, "number"),
    ]:
        for m in pattern.finditer(text):
            tok = m.group(0).strip()
            # Strip trailing sentence-punctuation (path regex was capturing
            # the period in "/home/user/foo.py." style sentences). Don't strip
            # internal characters — only trailing.
            tok = tok.rstrip(".,;:!?)\"'")
            if not tok:
                continue
            key = tok.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append((tok, kind))
    # Port pattern runs after — only adds tokens not already classified.
    # Requires the word "port" in the matched span to avoid catching every
    # 4-5 digit number as a port reference.
    for m in _PORT_PATTERN.finditer(text):
        full_match = m.group(0)
        port_num = m.group(1)
        if "port" in full_match.lower():
            key = port_num.lower()
            if key not in seen:
                seen.add(key)
                out.append((port_num, "port"))
    return out


def _is_anchored(claim: str, grounding_text: str) -> bool:
    """Substring match (case-insensitive). Cheap baseline; can be tightened later."""
    if not grounding_text:
        return False
    return claim.lower() in grounding_text.lower()


def check_citations(
    response: str,
    grounding_sources: Dict[str, str],
    *,
    max_flagged: int = 10,
) -> Dict:
    """Audit a v3-generated response against grounding context.

    Args:
        response: The v3 output to audit.
        grounding_sources: Dict of {label: text} — e.g. {"retrieved_context": "...",
                           "conversation_history": "...", "static_anchors": "..."}.
                           Anything in these is considered grounded.
        max_flagged: Cap on flagged claims to include in the footer (avoid spam).

    Returns:
        Dict with:
          - flagged: list of (claim, kind) tuples that aren't anchored
          - footer: human-readable summary string to append to response (empty if all clean)
          - audit: full per-claim audit data (for blackbox logging)
    """
    if not response or not response.strip():
        return {"flagged": [], "footer": "", "audit": {"claims_checked": 0}}

    grounding_combined = "\n".join(grounding_sources.values())

    claims = _extract_claims(response)
    flagged: List[Tuple[str, str]] = []
    anchored: List[Tuple[str, str]] = []

    for claim, kind in claims:
        if _is_anchored(claim, grounding_combined):
            anchored.append((claim, kind))
        else:
            flagged.append((claim, kind))

    audit = {
        "claims_checked": len(claims),
        "anchored": len(anchored),
        "flagged": len(flagged),
        "flagged_items": flagged[:max_flagged],
    }

    if not flagged:
        return {"flagged": [], "footer": "", "audit": audit}

    # Build footer — concise, dashboard-renderable
    shown = flagged[:max_flagged]
    overflow = len(flagged) - len(shown)
    items_str = ", ".join(f"{tok} ({kind})" for tok, kind in shown)
    overflow_str = f" (+{overflow} more)" if overflow > 0 else ""
    footer = (
        f"\n\n---\n_[citation-check] {len(flagged)} fact-shaped claim"
        f"{'s' if len(flagged) != 1 else ''} not anchored in retrieved context: "
        f"{items_str}{overflow_str}. These may be confabulated — verify before trusting._"
    )

    return {"flagged": flagged, "footer": footer, "audit": audit}
