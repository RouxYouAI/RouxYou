"""Path-B-for-identity — programmatic post-response check that catches v3
misassigning one of its OWN identity tokens to the user.

Background: the IDENTITY LOCK block in companion.py's CHAT_INFORMED_PROMPT is a
prompt-side instruction telling v3 "you are Roux, the user is DJ, don't tell DJ
he is Q4_K_M/Claude/the archive." It produced a PARTIAL win (2026-05-24): v3
started self-identifying as Roux, but STILL occasionally writes "You're Q4_K_M"
addressed to DJ. That is the third reproduction of the capability-floor lesson
(Path C citation discipline, identity-prefix, both prompt-side) — v3 (Ministral-3-8B)
is below the floor for reliably self-policing complex instructions.

The architectural answer is the same as Path B for citations (citation_check.py):
a programmatic check on the generated response, not another prompt instruction.
External check > self-policing, eighth+ application.

The specific failure shape: Roux addresses DJ in the second person and equates
him with one of HER identity tokens — "you're Q4_K_M", "you are the coder",
"the user is v3". The fix detects that equation and either flags it in a footer
or (config-gated) rewrites it to first person ("I'm Q4_K_M"), which is what she
almost certainly meant — a reflection on herself, mis-aimed at DJ.

Design choices (mirrors citation_check.py):
- Conservative: false negatives are fine, false positives are bad. We only flag
  a direct copula equation ("you ARE X"), never "you're talking to Roux" or
  "you're Roux's creator" (possessive / non-equating contexts are excluded).
- Footer-style by default; auto_rewrite is a separate knob, default OFF.
- Whitelist of self-identity tokens, expandable. DJ-identity tokens (DJ, the user)
  are explicitly NOT flagged — "you're DJ" is correct.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

# Tokens that name Roux or one of her components / related non-DJ entities.
# Telling DJ "you are <one of these>" is the error this module catches.
# NOT included: "DJ", "the user" — those correctly name the user.
_SELF_TOKENS = [
    "Roux",
    "v3",
    "Q4_K_M",
    "Q4_K",
    "Ministral-3",
    "Ministral",
    "the archive",
    "archive",
    "the coder",
    "coder",
    "the worker",
    "worker",
    "the authoring LoRA",
    "authoring LoRA",
    "Claude",
    "bigbrain",
]

# Build an alternation, longest-first so "Ministral-3" wins over "Ministral"
# and "the archive" over "archive". Escape regex-special chars in tokens.
_TOKEN_ALT = "|".join(
    re.escape(t) for t in sorted(_SELF_TOKENS, key=len, reverse=True)
)

# Words that, if they follow the token, mean it is NOT an equation but a
# relationship — "you're Roux's creator", "you are Claude's user". Don't flag.
_REL_FOLLOW = r"(?!\s+(?:creator|builder|maker|owner|author|dad|father|mother|human|user|handler|partner))"

# Possessive guard — "you're Roux's ..." is about Roux, not equating DJ with Roux.
_POSSESS = r"(?!['’]s)"

# Second-person equation: "you're <TOKEN>" / "you are the <TOKEN>".
# The optional leading article ("the"/"a") is consumed so the captured token is
# bare. Token must be at a word boundary, not possessive, not followed by a
# relationship noun.
_YOU_ARE = re.compile(
    r"\byou(?:['’]re|\s+are)\s+(?:the\s+|a\s+)?(" + _TOKEN_ALT + r")\b"
    + _POSSESS + _REL_FOLLOW,
    re.IGNORECASE,
)

# Third-person assignment to the user: "the user is <TOKEN>", "DJ is <TOKEN>",
# "the user is <TOKEN>".
_USER_IS = re.compile(
    r"\b(?:the user|DJ|the user)\s+is\s+(?:the\s+|a\s+)?(" + _TOKEN_ALT + r")\b"
    + _POSSESS + _REL_FOLLOW,
    re.IGNORECASE,
)


def _extract_misassignments(text: str) -> List[Tuple[str, str, Tuple[int, int]]]:
    """Find spots where the response assigns a self-identity token to the user.

    Returns list of (matched_span_text, token, (start, end)) tuples, in order of
    appearance, deduplicated by span.
    """
    out: List[Tuple[str, str, Tuple[int, int]]] = []
    seen_spans = set()
    for pattern in (_YOU_ARE, _USER_IS):
        for m in pattern.finditer(text):
            span = m.span()
            if span in seen_spans:
                continue
            seen_spans.add(span)
            out.append((m.group(0), m.group(1), span))
    out.sort(key=lambda x: x[2][0])
    return out


def _rewrite_to_first_person(text: str, matches) -> str:
    """Rewrite "you're <TOKEN>" → "I'm <TOKEN>" / "you are <TOKEN>" → "I am <TOKEN>".

    Only rewrites the second-person ('you're'/'you are') form — the third-person
    "the user is X" form is left for the footer because its correct repair is
    ambiguous (could be "I am X" or just a dropped clause). Applied right-to-left
    so earlier spans' offsets stay valid.
    """
    # Recompute spans for just the second-person form for safe rewriting.
    edits = []
    for m in _YOU_ARE.finditer(text):
        head = m.group(0)
        # Preserve contraction vs full form.
        if re.match(r"\byou['’]re\b", head, re.IGNORECASE):
            replacement = re.sub(
                r"\byou['’]re\b", "I'm", head, count=1, flags=re.IGNORECASE
            )
        else:
            replacement = re.sub(
                r"\byou\s+are\b", "I am", head, count=1, flags=re.IGNORECASE
            )
        edits.append((m.start(), m.end(), replacement))
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def check_identity(
    response: str,
    *,
    auto_rewrite: bool = False,
    max_flagged: int = 10,
) -> Dict:
    """Audit a v3-generated response for self-identity misassignment to the user.

    Args:
        response: The v3 output to audit.
        auto_rewrite: If True, rewrite second-person misassignments to first
                      person in the returned `response`. Default False (footer only).
        max_flagged: Cap on flagged items shown in the footer.

    Returns:
        Dict with:
          - flagged: list of (matched_text, token) tuples
          - response: the (optionally rewritten) response text
          - footer: human-readable summary to append (empty if clean)
          - audit: per-check audit data for blackbox logging
    """
    if not response or not response.strip():
        return {
            "flagged": [],
            "response": response,
            "footer": "",
            "audit": {"checked": False},
        }

    matches = _extract_misassignments(response)
    audit = {
        "checked": True,
        "misassignments": len(matches),
        "items": [(span_text, tok) for span_text, tok, _ in matches[:max_flagged]],
        "rewritten": False,
    }

    if not matches:
        return {"flagged": [], "response": response, "footer": "", "audit": audit}

    out_response = response
    if auto_rewrite:
        out_response = _rewrite_to_first_person(response, matches)
        audit["rewritten"] = out_response != response

    flagged = [(span_text, tok) for span_text, tok, _ in matches]
    shown = flagged[:max_flagged]
    overflow = len(flagged) - len(shown)
    items_str = "; ".join(f'"{span_text}" → {tok}' for span_text, tok in shown)
    overflow_str = f" (+{overflow} more)" if overflow > 0 else ""

    if auto_rewrite and audit["rewritten"]:
        footer = (
            f"\n\n---\n_[identity-check] {len(flagged)} self-identity misassignment"
            f"{'s' if len(flagged) != 1 else ''} auto-corrected to first person "
            f"({items_str}{overflow_str})._"
        )
    else:
        footer = (
            f"\n\n---\n_[identity-check] {len(flagged)} self-identity token"
            f"{'s' if len(flagged) != 1 else ''} addressed at the user "
            f"({items_str}{overflow_str}). Roux is Roux; DJ is DJ — likely meant in first person._"
        )

    return {
        "flagged": flagged,
        "response": out_response,
        "footer": footer,
        "audit": audit,
    }
