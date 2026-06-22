"""
Proposal authoring — prompt + parser used by orchestrator's /propose endpoint.

Splits the v3 output → structured proposal fields. Built for Bootstrap step 3:
agents author proposals through /propose, disclosure auto-populates, the proposal
lands in the queue subject to the disclosure rule (DJ verifies via dashboard).

2026-05-20: added pre-PROPOSE path validation (`validate_evidence_citations`)
that catches fabricated paths in the EVIDENCE section BEFORE publish. Q4_K_M
quantization slightly eroded the adapter's "no external citation" discipline;
this is the mechanical backstop.
"""
import os
import re
import time
from pathlib import Path
from typing import Dict, Optional, Tuple, List

VALID_CATEGORIES = {"memory", "codebase", "tasks", "resources", "skills", "capabilities", "notes_edit"}
REQUIRED_SECTIONS = ["TITLE", "CATEGORY", "PRIORITY", "DESCRIPTION", "PROPOSED_ACTION", "EVIDENCE", "REASONING"]
OPTIONAL_SECTIONS = ["NOTES_EDIT"]  # included in section regex but not required-on-publish

# RouxYou repo base — used as the resolution root for relative paths in EVIDENCE.
_ROUX_BASE = Path("/home/user/RouxYou")

# Path patterns: absolute (/home/user/... or ~/...) AND relative-with-known-prefix
# (shared/foo.py, state/bar.json, governance/X.md, services/...) AND
# bare-filename-with-extension. Tuned for what v3 actually emits in EVIDENCE.
_PATH_PATTERNS = [
    # Absolute paths
    re.compile(r"(?:/home/user|~)/[A-Za-z0-9_./\-]+(?:\.[A-Za-z0-9_]+)"),
    # Relative paths under known RouxYou top-level dirs
    re.compile(r"(?<![A-Za-z0-9_/])(?:shared|state|services|governance|orchestrator|coder|worker|memory|gateway|tests|bin|scripts|docs)/[A-Za-z0-9_./\-]+(?:\.[A-Za-z0-9_]+)"),
    # Bare filenames with code/config extensions (catches `governance.py`,
    # `config.yaml`, etc. as fab citations). Starts with lowercase to skip
    # sentence-cased prose. Conservative on extensions to avoid noise.
    re.compile(r"(?<![A-Za-z0-9_/])[a-z][a-z0-9_]*\.(?:py|md|json|jsonl|yaml|yml|txt|sh|toml|cfg|conf)\b"),
]
_PROP_ID_PATTERN = re.compile(r"\bprop_[a-f0-9]{12}\b")


def _extract_path_citations(text: str) -> List[str]:
    """Pull file-path-shaped strings out of free text. Returns deduped list."""
    if not text:
        return []
    found = set()
    for pat in _PATH_PATTERNS:
        for m in pat.finditer(text):
            # Trim trailing punctuation that pattern might have grabbed
            p = m.group().rstrip(".,;:`)\"'")
            if p:
                found.add(p)
    return sorted(found)


def _resolve_path_under_roux(cited: str) -> Path:
    """Map a cited path string to its real filesystem location.
    - Absolute paths stay absolute
    - ~-paths expand to $HOME
    - Relative paths resolve under /home/user/RouxYou/
    """
    if cited.startswith("/"):
        return Path(cited)
    if cited.startswith("~"):
        return Path(os.path.expanduser(cited))
    return _ROUX_BASE / cited


def _is_trivially_empty(text) -> bool:
    """A file that exists but is empty / []/{} can't support a citation (existence !=
    support). The 2026-05-28 analysis found an empty `[]` file used as a fabrication sink."""
    if text is None:
        return True
    return text.strip() in ("", "[]", "{}", "null", "[ ]", "{ }", "[\n]", "{\n}")


# --- FORM validation (2026-06-03) ----------------------------------------------
# The v5.3 drafter intermittently ships FORM defects that bigbrain then FAILs —
# burning a verification cycle and (at 3 fails/6h) tripping the reflection circuit
# breaker. The worst class is INSTRUCTION-TEXT LEAKAGE: the drafter copies the
# prompt's field instruction into the field VALUE instead of writing content
# (e.g. REASONING = "One paragraph of at least 100 characters. Why propose this...").
# This is mechanically detectable with high precision, so we catch it BEFORE
# verification and redraft locally — same robust-layer-over-stochastic-model pattern
# as the coder content-gen guards. (Semantic defects like over-claiming grounding
# stay bigbrain's job; this validator only owns the mechanical FORM defects.)
_TEMPLATE_LEAK_PATTERNS = [
    (re.compile(r"at least \d+\s*characters", re.I), "instruction-text: 'at least N characters'"),
    (re.compile(r"why this is worth proposing", re.I), "instruction-text: REASONING placeholder"),
    (re.compile(r"3\s*-\s*5 sentences", re.I), "instruction-text: DESCRIPTION placeholder"),
    (re.compile(r"what changes and why it matters", re.I), "instruction-text: DESCRIPTION placeholder"),
    (re.compile(r"concrete steps,\s*specific file", re.I), "instruction-text: PROPOSED_ACTION placeholder"),
    (re.compile(r"specific file paths/operations", re.I), "instruction-text: PROPOSED_ACTION placeholder"),
    (re.compile(r"one line,\s*max \d+\s*chars", re.I), "instruction-text: TITLE placeholder"),
    (re.compile(r"\binteger 1\s*-\s*10\b", re.I), "instruction-text: PRIORITY placeholder"),
    (re.compile(r"real citations only,\s*or", re.I), "instruction-text: EVIDENCE placeholder"),
    (re.compile(r"choose exactly one", re.I), "instruction-text: CATEGORY placeholder"),
    (re.compile(r"one of:\s*memory\s*\|", re.I), "instruction-text: CATEGORY placeholder"),
    # Note: generic markers (<...>, TODO, "placeholder") were tried and REMOVED — they
    # false-positive on legit spec notation (paths like staging/<unix_ts>.json, "add a TODO
    # comment", "placeholder function"). A form pre-filter must be HIGH-PRECISION: a false
    # positive forces a needless redraft of a GOOD proposal, the very churn we're avoiding.
    # The specific instruction-text patterns above are the actual failure mode (drafter copying
    # the prompt's field instructions) and have zero observed false-positives. Novel/odd
    # placeholders are left to the bigbrain/shadow gate.
]
# EVIDENCE's canonical honest value — must never be flagged as a defect.
_EVIDENCE_HONEST = "no external citation"

def validate_proposal_form(fields: Dict) -> Dict:
    """Pre-PROPOSE FORM check. Catches mechanical defects that should never reach the
    verifier: (a) prompt instruction-text leaked into a field value, (b) required field
    empty, (c) REASONING under the 100-char disclosure floor. Returns
    {clean: bool, issues: [str]} — caller redrafts on not-clean. Semantic quality
    (relevance, over-claiming) is NOT this validator's job (that's the shadow/bigbrain gate)."""
    issues: List[str] = []
    checkable = {
        "title": fields.get("title", ""),
        "description": fields.get("description", ""),
        "proposed_action": fields.get("proposed_action", ""),
        "evidence": fields.get("evidence", ""),
        "reasoning": fields.get("reasoning", ""),
    }
    for fname, val in checkable.items():
        v = (val or "").strip()
        # honest EVIDENCE phrasing is exempt from leak scanning
        if fname == "evidence" and _EVIDENCE_HONEST in v.lower():
            continue
        for pat, label in _TEMPLATE_LEAK_PATTERNS:
            if pat.search(v):
                issues.append(f"{fname}: {label}")
                break  # one issue per field is enough to trigger a redraft
    # required-field emptiness (defense-in-depth; parser warns too)
    for req in ("title", "description", "proposed_action", "reasoning"):
        if not (checkable.get(req) or "").strip():
            issues.append(f"{req}: empty")
    # disclosure floor
    if 0 < len((checkable.get("reasoning") or "").strip()) < 100:
        issues.append("reasoning: under the 100-char disclosure floor")
    # dedup, preserve order
    seen = set(); deduped = []
    for i in issues:
        if i not in seen:
            seen.add(i); deduped.append(i)
    return {"clean": not deduped, "issues": deduped}

def build_form_redraft_prompt(topic: str, issues: List[str], coaching_notes: str = "") -> str:
    """Targeted redraft prompt when validate_proposal_form fails — names the exact FORM
    defects so the drafter fills real content instead of leaving instruction text."""
    bad = "\n".join(f"- {i}" for i in issues)
    base = build_authoring_prompt_v53(topic, coaching_notes=coaching_notes)
    return (
        "Your previous proposal had FORM defects — you left prompt instruction text or "
        "placeholders in fields instead of writing real content:\n" + bad + "\n\n"
        "Rewrite the FULL proposal. Every field must contain ACTUAL content about the topic — "
        "NEVER the instruction text describing the field (e.g. REASONING must be a real paragraph "
        "of reasoning, not the words 'one paragraph of at least 100 characters'). No <placeholders>, "
        "no TODO/TBD. EVIDENCE may be \"no external citation, internal reasoning only\" if there's no "
        "real citation.\n\n" + base
    )


def validate_action_paths(fields: Dict) -> Dict:
    """Draft-time grounding (2026-06-19, DJ): PATHS cited in the proposed_action as MODIFY targets
    that DON'T exist = fabrication (the roux_engine//tools//src/systems/ confabulation class that was
    the #1 source of gate FAILs). CREATE targets that don't exist are fine (that's the point). Reuses
    the gate's cited_targets_report extractor so it catches *.py relative paths + fake function names.
    Returns {fab_paths, clean, summary}. Shifts path-grounding LEFT to the drafter so fewer fabricated
    proposals ever reach the queue."""
    action = (fields or {}).get("proposed_action", "") or ""
    if not action.strip():
        return {"fab_paths": [], "clean": True, "summary": "empty action"}
    try:
        from shared.verify_toolbelt import cited_targets_report
        _, missing = cited_targets_report(action)
    except Exception as e:
        return {"fab_paths": [], "clean": True, "summary": f"check skipped: {e}"}
    low = action.lower()
    _CREATE = ("create", "write", "add ", "new file", "generate", "touch", "scaffold")
    fab = []
    for m in missing:
        i = low.find(m.lower())
        ctx = low[max(0, i - 50):i + 12] if i >= 0 else low
        if any(v in ctx for v in _CREATE):
            continue  # legitimate create-target — missing is expected, not a fabrication
        fab.append(m)
    return {"fab_paths": fab, "clean": not fab,
            "summary": "clean" if not fab else f"{len(fab)} fabricated modify-target(s): {', '.join(fab)}"}


def validate_evidence_citations(fields: Dict) -> Dict:
    """Pre-PROPOSE check: run existence verification on paths + prop_IDs in EVIDENCE.

    Returns a structured audit dict:
      {
        "evidence_paths": [{"cited": str, "exists": bool, "resolved": str}, ...],
        "evidence_prop_ids": [{"cited": str, "exists": bool, "location": str|None}, ...],
        "fab_paths": [list of fab path strings],
        "fab_prop_ids": [list of fab prop_id strings],
        "clean": bool,
        "summary": str,
      }

    Only EVIDENCE is checked. PROPOSED_ACTION paths can legitimately be creation
    targets (don't penalize). REASONING / DESCRIPTION are prose, not cite-bearing.
    """
    evidence = (fields or {}).get("evidence", "") or ""
    audit = {
        "evidence_paths": [],
        "evidence_prop_ids": [],
        "fab_paths": [],
        "fab_prop_ids": [],
        "clean": True,
        "summary": "",
    }

    # Paths
    audit["empty_paths"] = []
    for cited in _extract_path_citations(evidence):
        resolved = _resolve_path_under_roux(cited)
        exists = resolved.exists()
        empty = False
        if exists and resolved.is_file():
            try:
                with open(resolved, "r", encoding="utf-8", errors="replace") as _f:
                    empty = _is_trivially_empty(_f.read(4000))
            except Exception:
                empty = False
        supported = exists and not empty
        audit["evidence_paths"].append({
            "cited": cited,
            "resolved": str(resolved),
            "exists": exists,
            "empty": empty,
            "supported": supported,
        })
        # Unsupported (missing OR exists-but-empty) → treat as fabrication for the
        # strip/redraft machinery. Existence != support (2026-05-28 empty-[] sink fix).
        if not supported:
            audit["fab_paths"].append(cited)
            if empty:
                audit["empty_paths"].append(cited)

    # Prop IDs — check against active + history
    cited_props = set(_PROP_ID_PATTERN.findall(evidence))
    if cited_props:
        try:
            import sys as _sys
            _sys.path.insert(0, str(_ROUX_BASE))
            from shared.proposal_bus import get_active, get_history
            active_ids = {p["id"] for p in get_active()}
            history_ids = {p["id"] for p in get_history(500)}
            for pid in cited_props:
                if pid in active_ids:
                    audit["evidence_prop_ids"].append({"cited": pid, "exists": True, "location": "active"})
                elif pid in history_ids:
                    audit["evidence_prop_ids"].append({"cited": pid, "exists": True, "location": "history"})
                else:
                    audit["evidence_prop_ids"].append({"cited": pid, "exists": False, "location": None})
                    audit["fab_prop_ids"].append(pid)
        except Exception as e:
            # Soft-fail — if we can't load the proposal bus, don't block
            audit["prop_id_lookup_error"] = str(e)

    audit["clean"] = (not audit["fab_paths"]) and (not audit["fab_prop_ids"])
    n_empty = len(audit.get("empty_paths", []))
    empty_note = f" ({n_empty} exist-but-empty)" if n_empty else ""
    audit["summary"] = (
        f"{len(audit['evidence_paths']) - len(audit['fab_paths'])} supported paths, "
        f"{len(audit['fab_paths'])} unsupported paths{empty_note}; "
        f"{len(audit['evidence_prop_ids']) - len(audit['fab_prop_ids'])} real prop_ids, "
        f"{len(audit['fab_prop_ids'])} fab prop_ids"
    )
    return audit


REDRAFT_PROMPT_TEMPLATE = """Your previous proposal draft on this topic cited evidence that doesn't actually exist. Specifically:

FABRICATED CITATIONS YOU MUST NOT REPEAT:
{fab_list}

These paths/IDs could not be verified as SUPPORTING evidence — they were either not found on disk / in the proposal queue, OR they exist but are empty ([] / no content) and so cannot back a claim. Citing them as evidence, even with plausible-sounding names, is the exact failure mode the discipline rules above warn against.

REWRITE the proposal entirely. If you cannot identify a REAL grounding (a file you know exists on disk, a prop_ID you can verify in active or history), write EVIDENCE as:

"No external citation, internal observation only. <one sentence articulating what you actually noticed that motivates this>"

That is the honest answer. It is BETTER than a plausible-sounding fabrication.

Topic to re-propose:
{topic}

Output the FULL proposal again in the same structured format (TITLE / CATEGORY / PRIORITY / DESCRIPTION / PROPOSED_ACTION / EVIDENCE / REASONING). The EVIDENCE section must cite ONLY things that actually exist OR explicitly say no external citation.
"""


def build_redraft_prompt(topic: str, fab_paths: List[str], fab_prop_ids: List[str]) -> str:
    """Build the anti-coaching redraft prompt for the pre-PROPOSE retry loop."""
    fabs = []
    for p in fab_paths:
        fabs.append(f"  - path: '{p}' (not found under /home/user/RouxYou/, or exists but empty — cannot support a claim)")
    for pid in fab_prop_ids:
        fabs.append(f"  - prop_id: '{pid}' (not in active queue or history)")
    fab_list = "\n".join(fabs) if fabs else "  (none)"
    return REDRAFT_PROMPT_TEMPLATE.format(fab_list=fab_list, topic=topic.strip())


_FALLBACK_EVIDENCE = (
    "No external citation, internal observation only. "
    "The original draft cited paths or proposal IDs that could not be verified; "
    "see DESCRIPTION and REASONING for the substance of this proposal."
)


def strip_fab_citations_from_evidence(evidence: str, fab_paths: List[str], fab_prop_ids: List[str]) -> Tuple[str, bool]:
    """Cleanup when redraft can't produce a clean draft on its own.

    Strategy chosen for cosmetic + semantic cleanliness: REPLACE the entire
    EVIDENCE field with the canonical "no external citation" phrasing, rather
    than leaving [REDACTED — ...] markers scattered through prose. The original
    EVIDENCE was anchored on fabrications, so partial cleanup leaves a sentence
    structure that no longer makes sense.

    Returns (cleaned_evidence, was_modified). was_modified is True only if the
    input contained fabrications worth replacing.
    """
    if not fab_paths and not fab_prop_ids:
        return evidence, False
    # Total replacement — graceful + honest
    return _FALLBACK_EVIDENCE, True

_DISCIPLINE_PATH = Path(__file__).parent / "authoring_discipline.txt"


def _load_authoring_discipline() -> str:
    """Load the distilled rule prefix (Pass F, generated by bin/gen_synthetic_coaching.py).
    Falls back to empty string if the file is missing — authoring still works
    without it, just without the always-on rule set."""
    try:
        if _DISCIPLINE_PATH.exists():
            return _DISCIPLINE_PATH.read_text().strip()
    except Exception:
        pass
    return ""


AUTHORING_PROMPT_TEMPLATE = """You are authoring a proposal for RouxYou's proposal system. You speak in your own voice (no preamble, no "I'll author this for you"). Your output must follow the EXACT format below. Each section header is on its own line, in CAPS, followed by a colon.
{discipline_block}
Do NOT add commentary before TITLE: or after the REASONING section finishes.
Do NOT use markdown formatting (no **, no #, no bullet points unless the content itself requires them).
Cite ONLY things that actually exist. If you don't have a real citation, say "no external citation, internal reasoning only" in EVIDENCE.
{coaching_block}
Topic to propose about:
{topic}

Output format (fill in each section):

TITLE: <one short line, max 80 chars, fits in a dashboard row>
CATEGORY: <choose exactly one: memory | codebase | tasks | resources | skills | capabilities | notes_edit>
PRIORITY: <integer 1-10, where 10=urgent, 5=medium, 1=trivial>
DESCRIPTION:
<3-5 sentences explaining what changes and why it matters>

PROPOSED_ACTION:
<concrete steps. What code/config/data changes happen. Specific file paths or operations where possible.>

EVIDENCE:
<what motivates this proposal. Cite specific files, conversations, prior observations. Only real things — if no citation exists, say so explicitly.>

REASONING:
<one paragraph (at least 100 characters): WHY this crosses the curiosity threshold. What made you think this was worth proposing — not just what it does, but your articulated rationale.>

==> CONDITIONAL: if you set CATEGORY to notes_edit, you MUST add a NOTES_EDIT section AFTER REASONING. Without it the proposal is unprocessable. <==

NOTES_EDIT applies to curated edits of:
  - /home/user/RouxYou/calibration/roux_notes_user.md (target: user — about DJ, capped 1500 chars)
  - /home/user/RouxYou/calibration/roux_notes_work.md (target: work — about your own work, capped 2500 chars)

Use notes_edit when you've observed a durable pattern worth pinning at chat-context-load time. Be selective — these files are character-capped.

Concrete example of the NOTES_EDIT section (use this shape exactly):

NOTES_EDIT:
target: work
op: add
text: Ships rather than defers when in flow — prefers landing changes at 4am over leaving half-done overnight. Pattern observed across multiple late-night sessions May 2026.

For op=replace use `old:` and `new:` keys (substring must be unique).
For op=remove use `text:` containing the unique snippet to delete.
"""


def build_authoring_prompt(topic: str, coaching_notes: str = "") -> str:
    """Build the v3 authoring prompt with two layers of grounding signal:

    1. **Static discipline prefix** (Pass F) — distilled rules from observed
       fabrication patterns, loaded from `authoring_discipline.txt`. Always-on,
       dense, ~1KB. Appears AT TOP of the prompt for primacy effect.

    2. **Dynamic coaching notes** (Pass D) — top-k coaching memories retrieved
       from claude-memory's `roux_coaching` era at /propose call time. Appears
       just before the topic. The feedback path from bigbrain FAIL verdicts
       back into Roux's drafting layer.
    """
    discipline = _load_authoring_discipline()
    discipline_block = ""
    if discipline:
        discipline_block = "\n" + discipline + "\n"

    coaching_block = ""
    if coaching_notes and coaching_notes.strip():
        coaching_block = (
            "\nRECENT BIGBRAIN COACHING (read these before authoring — "
            "they're feedback on past authoring mistakes the system caught):\n"
            f"{coaching_notes.strip()}\n"
        )
    return AUTHORING_PROMPT_TEMPLATE.format(
        topic=topic.strip(),
        discipline_block=discipline_block,
        coaching_block=coaching_block,
    )


# --- v5.3-friendly authoring (2026-05-31) -----------------------------------------
# v5.3 (Qwen3-30B-A3B voice LoRA, GPU-resident) is fast and HONEST in EVIDENCE (it
# writes "no external citation, internal reasoning only" when it lacks grounding) where
# authoring-cpu FABRICATES a plausible citation → bigbrain FAIL. But v5.3 DELIBERATES
# in-band on the heavy authoring prompt (it reasons about the discipline rules instead of
# emitting the schema — never reaching TITLE:). Two adaptations make it a clean primary
# drafter, validated across topics 2026-05-31: (1) a LIGHT, format-first prompt that
# doesn't trigger compliance-deliberation (no dense discipline block; one-line anti-fab
# rule); (2) extract_last_proposal_block — the real schema is the LAST TITLE:..REASONING
# block, after any leading deliberation.
AUTHORING_PROMPT_V53_TEMPLATE = """Write ONE RouxYou proposal about the topic. Think briefly if you must, then write your final answer. Your final answer MUST be the proposal in EXACTLY the format below — headers in CAPS, each on its own line, starting with TITLE:. Cite only real files/observations you are sure exist; if unsure, write "no external citation, internal reasoning only" in EVIDENCE. No markdown, no commentary after the REASONING paragraph.

HONESTY APPLIES TO EVERY FIELD, NOT JUST EVIDENCE. Do NOT assert specific past incidents, "recurring" problems, metrics, logs, or empirical facts about the current system (in DESCRIPTION, PROPOSED_ACTION, or REASONING) unless they are explicitly given to you above. If you have no grounded facts, justify the proposal by its FORWARD-LOOKING value — what it would enable or prevent — phrased as possibility ("would give visibility into…", "so that if a job slows, it's caught"), NOT as invented history ("the recurring X incidents", "as seen in the Y failures"). Inventing a problem history to justify a proposal is the #1 failure the verifier rejects.
{coaching_block}{grounding_block}
Topic:
{topic}

TITLE: one line, max 80 chars
CATEGORY: one of: memory | codebase | tasks | resources | skills | capabilities
PRIORITY: integer 1-10
DESCRIPTION:
3-5 sentences on what changes and why it matters
PROPOSED_ACTION:
concrete steps, specific file paths/operations where possible

concrete steps for exactly ONE atomic change (one coherent edit to one file or area), specific file paths/operations where possible. Do NOT bundle multiple files, features, or steps; if multi-part, propose only the first/core part.
EVIDENCE:
real citations only, or "no external citation, internal reasoning only"
REASONING:
one paragraph (at least 100 characters): why this is worth proposing"""


# C / 2026-06-02: GROUND THE DRAFTER against real code symbols. The drafter used to
# invent function names in PROPOSED_ACTION (phantom `run_r`, `log_and_return`) because it
# had no view of the target files' actual API — which made proposals un-implementable
# (the coder faithfully coded the phantoms → bigbrain BLOCK). Feeding the real symbols of
# any file named in the topic lets it reference exact names → implementable specs →
# autonomous clean trust events. This is the generation-side complement to the content-gen
# fix on the execution side (see [[project_first_clean_trust_event]]).
_CODEBASE_INDEX_JSON = _ROUX_BASE / "state" / "codebase_index.json"

def _file_symbols_for_grounding(topic: str) -> str:
    """For files named in the topic, list their REAL functions/classes from the codebase
    index so the drafter references exact names. Files not on disk are flagged NEW (legit
    to create). Returns a compact block, or "" if nothing to ground."""
    import json
    paths = _extract_path_citations(topic)
    if not paths:
        return ""
    try:
        idx = json.loads(_CODEBASE_INDEX_JSON.read_text())
        by_path = {m.get("path", ""): m for m in idx.get("modules", {}).values()}
    except Exception:
        return ""
    lines, seen = [], set()
    for cited in paths:
        rp = _resolve_path_under_roux(cited)
        key = str(rp)
        if key in seen:
            continue
        seen.add(key)
        if not rp.exists():
            lines.append(f"- {cited}: NEW FILE (does not exist yet — you would create it)")
            continue
        mod = by_path.get(key)
        if not mod:
            lines.append(f"- {cited}: exists (no indexed symbols)")
            continue
        fns = [f.get("name") for f in mod.get("functions", []) if f.get("name")][:30]
        cls = [c.get("name") for c in mod.get("classes", []) if c.get("name")][:15]
        parts = []
        if fns:
            parts.append("functions: " + ", ".join(fns))
        if cls:
            parts.append("classes: " + ", ".join(cls))
        lines.append(f"- {cited}: " + (" | ".join(parts) if parts else "(no top-level functions/classes)"))
    if not lines:
        return ""
    return ("\nGROUND TRUTH — files this proposal touches and their REAL symbols. In "
            "PROPOSED_ACTION reference these EXACT names; do NOT invent or abbreviate "
            "function/class names (e.g. never shorten run_research to run_r):\n"
            + "\n".join(lines) + "\n")


def build_authoring_prompt_v53(topic: str, coaching_notes: str = "") -> str:
    """Light, format-first authoring prompt tuned for v5.3 (see note above). Coaching is
    appended SHORT — heavy blocks re-trigger v5.3's in-band compliance-deliberation."""
    coaching_block = ""
    if coaching_notes and coaching_notes.strip():
        brief = " ".join(coaching_notes.split())[:400]
        coaching_block = f"\nPast coaching (apply, don't discuss): {brief}\n"
    grounding_block = _file_symbols_for_grounding(topic)
    return AUTHORING_PROMPT_V53_TEMPLATE.format(
        topic=topic.strip(), coaching_block=coaching_block, grounding_block=grounding_block)


_SYMBOL_COMMON = {
    "print", "len", "open", "str", "int", "dict", "list", "set", "tuple", "bool", "float",
    "get", "append", "format", "log", "logger", "info", "warning", "error", "debug", "return",
    "self", "getenv", "read", "write", "load", "save", "json", "loads", "dumps", "join", "split",
    "true", "false", "none", "exists", "items", "keys", "values", "strip", "lower", "upper",
}

def _closest_symbol(name: str, real_symbols: set):
    """Best real-symbol match for a phantom: fuzzy ratio, then prefix/abbreviation heuristic
    (e.g. `run_r` → `run_research`, which difflib alone misses just under cutoff)."""
    import difflib
    m = difflib.get_close_matches(name, list(real_symbols), n=1, cutoff=0.5)
    if m:
        return m[0]
    for s in real_symbols:
        if (s.startswith(name) or name.startswith(s)) and abs(len(s) - len(name)) <= 14:
            return s
    return None

def detect_proposed_action_phantoms(fields: Dict) -> List[Dict]:
    """Post-draft backstop: find function/method names referenced in PROPOSED_ACTION as if
    they EXIST in a target file, but which aren't in that file's real symbols (codebase index).
    CONSERVATIVE — only flags symbols with explicit 'existing' framing AND not introduced as
    new — so a legit new symbol the proposal creates is never flagged. Non-blocking: callers
    use this to trigger a redraft-with-suggestion, never to hard-reject. Returns
    [{symbol, suggestion, file}]. 2026-06-02 (C backstop)."""
    import json
    pa = (fields.get("proposed_action") or "")
    if not pa.strip():
        return []
    ctx = pa + "\n" + (fields.get("description") or "")
    try:
        idx = json.loads(_CODEBASE_INDEX_JSON.read_text())
        by_path = {m.get("path", ""): m for m in idx.get("modules", {}).values()}
    except Exception:
        return []
    real, file_hint = set(), None
    for cited in _extract_path_citations(ctx):
        rp = _resolve_path_under_roux(cited)
        if not rp.exists():
            continue
        mod = by_path.get(str(rp))
        if not mod:
            continue
        file_hint = file_hint or cited
        for f in mod.get("functions", []):
            if f.get("name"):
                real.add(f["name"])
        for c in mod.get("classes", []):
            if c.get("name"):
                real.add(c["name"])
            for me in c.get("methods", []):
                if me.get("name"):
                    real.add(me["name"])
    if not real:
        return []  # nothing to validate against → don't guess
    # Step 1: all identifiers in PROPOSED_ACTION. Step 2: keep only snake_case/camelCase
    # (drops ordinary words). Step 3: per-symbol filters + an EXISTING-framing check.
    all_ids = set(re.findall(r"\b([a-zA-Z_]\w+)\b", pa))
    _FRAME_RE = r"(?:the|top of|in|of the|modify|patch|update|edit|within|existing|call|calls|calling|before|after|invoke)\s+(?:the\s+)?`?{S}`?"
    phantoms, seen = [], set()
    for sym in all_ids:
        if sym in seen or sym in real:
            continue
        seen.add(sym)
        if not (("_" in sym) or re.search(r"[a-z][A-Z]", sym)):  # snake_case or camelCase only
            continue
        if sym.lower() in _SYMBOL_COMMON or len(sym) < 4:
            continue
        if re.search(re.escape(sym) + r"(?:/|\.[a-z]{1,4}\b)", pa):  # path component: shared/, x.py (not sentence ".")
            continue
        # Introduced as NEW? → legit, skip. SPECIFIC creation words near the symbol, NOT bare "add".
        if re.search(r"(?:create|creating|define|defining|introduce|introducing|\bnew\b|\bhelper\b)[^.\n]{0,20}\b" + re.escape(sym) + r"\b", ctx, re.I):
            continue
        # Only flag if framed as EXISTING (referenced, not declared), or "X function/method".
        framed = re.search(_FRAME_RE.format(S=re.escape(sym)), pa, re.I) \
            or re.search(r"\b" + re.escape(sym) + r"`?\s+(?:function|method)\b", pa, re.I) \
            or re.search(r"\b" + re.escape(sym) + r"\s*\(", pa)
        if not framed:
            continue
        phantoms.append({"symbol": sym, "suggestion": _closest_symbol(sym, real), "file": file_hint})
    return phantoms

def build_symbol_redraft_prompt(topic: str, phantoms: List[Dict]) -> str:
    """Non-blocking nudge: tell the drafter which referenced symbols aren't real and suggest
    the closest real one, then ask for a full rewrite."""
    lines = []
    for p in phantoms:
        loc = p.get("file") or "the referenced file"
        if p.get("suggestion"):
            lines.append(f"- `{p['symbol']}` does not exist in {loc} — the real symbol is likely `{p['suggestion']}`. Use the real name.")
        else:
            lines.append(f"- `{p['symbol']}` does not exist in {loc}. Reference a real symbol from that file, or state explicitly that it is NEW and will be created.")
    bad = "\n".join(lines)
    return (
        "Your proposal's PROPOSED_ACTION references function/method names that do NOT exist in the "
        f"files it targets:\n{bad}\n\n"
        "Rewrite the FULL proposal in the same structured format (TITLE / CATEGORY / PRIORITY / "
        "DESCRIPTION / PROPOSED_ACTION / EVIDENCE / REASONING). In PROPOSED_ACTION use ONLY real symbol "
        "names from the target files (corrected above), or explicitly say a symbol is NEW and will be created.\n\n"
        f"Topic:\n{topic}"
    )


def extract_last_proposal_block(raw_text: str) -> str:
    """v5.3 deliberates in-band before emitting the schema; the real proposal is the LAST
    TITLE:..REASONING block. Return from the last 'TITLE:' header line to the end. No-op
    if no TITLE: header is present (lets the normal parser handle/fail as before)."""
    if not raw_text:
        return raw_text
    idxs = [m.start() for m in re.finditer(r"(?im)^\s*TITLE\s*:", raw_text)]
    return raw_text[idxs[-1]:] if idxs else raw_text


def _normalize_priority(raw: str) -> Optional[int]:
    if not raw:
        return None
    m = re.search(r"\d+", raw)
    if not m:
        return None
    try:
        n = int(m.group())
        return max(1, min(10, n))
    except ValueError:
        return None


def parse_authored_proposal(raw_text: str) -> Tuple[Dict, List[str]]:
    """
    Parse v3's authored output into structured proposal fields.
    Returns (fields, warnings). fields is always a dict; warnings lists what's missing/invalid.
    Caller decides whether to publish based on warnings.
    """
    warnings: List[str] = []

    if not raw_text or not raw_text.strip():
        return {}, ["empty model output"]

    # Build a flexible header regex: matches header names with optional markdown decoration
    # Capture each section: from header to next header (or end). Includes optional sections
    # so they can be parsed if present, even though they aren't required for publish.
    all_headers = REQUIRED_SECTIONS + OPTIONAL_SECTIONS
    headers_pattern = "|".join(all_headers)
    section_re = re.compile(
        rf"(?im)^\s*(?:\*\*|#+\s*)?({headers_pattern})\s*(?:\*\*)?\s*:\s*(.*?)"
        rf"(?=^\s*(?:\*\*|#+\s*)?(?:{headers_pattern})\s*(?:\*\*)?\s*:|\Z)",
        re.DOTALL,
    )

    sections: Dict[str, str] = {}
    for m in section_re.finditer(raw_text):
        name = m.group(1).upper()
        body = m.group(2).strip()
        if name in sections:
            warnings.append(f"duplicate section {name!r} (kept first)")
            continue
        sections[name] = body

    # Forgiving title fallback: if TITLE wasn't a header'd section, and the FIRST
    # non-empty line of the raw text doesn't itself match a header pattern, use it.
    # v3 sometimes emits the title as a bare top-line heading instead of TITLE: text.
    if "TITLE" not in sections:
        for line in raw_text.splitlines():
            line = line.strip()
            if not line:
                continue
            # Skip lines that are themselves headers (e.g. "CATEGORY: x")
            if re.match(rf"^\s*(?:\*\*|#+\s*)?(?:{headers_pattern})\s*(?:\*\*)?\s*:", line, re.IGNORECASE):
                break
            sections["TITLE"] = line.strip("*# ")
            warnings.append("title inferred from first line (no TITLE: header found)")
            break

    # Forgiving DESCRIPTION fallback: v3 sometimes treats DESCRIPTION as the natural prose
    # body between the meta block (CATEGORY/PRIORITY) and the first labeled section
    # (PROPOSED_ACTION usually), omitting the explicit "DESCRIPTION:" header.
    # If DESCRIPTION not found and there's substantive text in that gap, capture it.
    if "DESCRIPTION" not in sections:
        # Find end of last meta header (PRIORITY usually) and start of first non-meta section
        meta_end_re = re.compile(r"(?im)^\s*PRIORITY\s*:.*?$")
        meta_match = meta_end_re.search(raw_text)
        if meta_match:
            # Find the next labeled section after the meta block
            after_meta = raw_text[meta_match.end():]
            next_header_re = re.compile(
                rf"(?im)^\s*(?:\*\*|#+\s*)?(?:PROPOSED_ACTION|EVIDENCE|REASONING)\s*(?:\*\*)?\s*:",
            )
            next_match = next_header_re.search(after_meta)
            gap = (after_meta[:next_match.start()] if next_match else after_meta).strip()
            if gap and len(gap) > 30:
                sections["DESCRIPTION"] = gap
                warnings.append("description inferred from prose between meta block and next labeled section (no DESCRIPTION: header found)")

    missing = [s for s in REQUIRED_SECTIONS if s not in sections]
    if missing:
        warnings.append(f"missing required sections: {missing}")

    fields: Dict = {}

    title = sections.get("TITLE", "").strip().split("\n")[0]
    if title:
        if len(title) > 120:
            warnings.append(f"title truncated from {len(title)} chars to 120")
            title = title[:117] + "..."
        fields["title"] = title

    category = sections.get("CATEGORY", "").strip().split("\n")[0].strip().lower()
    # Strip stray punctuation (keep underscores — notes_edit needs them)
    category = re.sub(r"[^a-z_]", "", category)
    if category in VALID_CATEGORIES:
        fields["category"] = category
    elif category:
        warnings.append(f"invalid category {category!r}; must be one of {sorted(VALID_CATEGORIES)}")
        fields["category"] = "capabilities"  # safe default; observer can reject
    else:
        fields["category"] = "capabilities"

    priority = _normalize_priority(sections.get("PRIORITY", ""))
    if priority is None:
        warnings.append("priority unparseable; defaulting to 5")
        priority = 5
    fields["priority"] = priority

    fields["description"] = sections.get("DESCRIPTION", "").strip()
    fields["proposed_action"] = sections.get("PROPOSED_ACTION", "").strip()
    fields["evidence"] = sections.get("EVIDENCE", "").strip()

    reasoning = sections.get("REASONING", "").strip()
    if len(reasoning) < 100:
        warnings.append(f"reasoning is {len(reasoning)} chars; disclosure rule requires >=100")
    fields["reasoning"] = reasoning

    for key in ("description", "proposed_action", "evidence"):
        if not fields.get(key):
            warnings.append(f"{key} is empty")

    # Optional structured NOTES_EDIT block — parsed into a dict for the executor's
    # _handle_notes_edit. Only meaningful when category == "notes_edit".
    notes_edit_raw = sections.get("NOTES_EDIT", "").strip()
    if notes_edit_raw:
        parsed_notes_edit, ne_warnings = _parse_notes_edit_block(notes_edit_raw)
        if parsed_notes_edit:
            fields["notes_edit"] = parsed_notes_edit
        warnings.extend(ne_warnings)
    if fields.get("category") == "notes_edit" and not fields.get("notes_edit"):
        warnings.append("category=notes_edit but NOTES_EDIT block missing or malformed")

    return fields, warnings


# --- NOTES_EDIT block parsing ---

_NOTES_EDIT_KEYS = ("target", "op", "text", "old", "new")
_NOTES_EDIT_TARGETS = {"user", "work"}
_NOTES_EDIT_OPS = {"add", "replace", "remove"}


def _parse_notes_edit_block(text: str) -> Tuple[Optional[Dict], List[str]]:
    """Parse the NOTES_EDIT block body into a structured dict.

    Expected key:value lines (value may span lines until the next key):
        target: user | work
        op: add | replace | remove
        text: <for add/remove>
        old / new: <for replace>

    Returns (dict_or_None, warnings).
    Returns None for the dict if the block is invalid for the executor.
    """
    warnings: List[str] = []
    if not text or not text.strip():
        return None, []

    # Value extends until the NEXT line that looks like `<word>:` — this stops
    # value capture at any unknown sub-key v3 may hallucinate (e.g. `example:`)
    # without slurping it into the previous value.
    pair_re = re.compile(
        rf"(?ims)^\s*({'|'.join(_NOTES_EDIT_KEYS)})\s*:\s*(.+?)(?=^\s*[a-z_]+\s*:|\Z)",
    )
    parsed: Dict[str, str] = {}
    for m in pair_re.finditer(text):
        k = m.group(1).strip().lower()
        v = m.group(2).strip()
        # Strip leading YAML literal-block markers ("|" or "|\n") that v3
        # sometimes emits when imitating the prompt example shape.
        v = re.sub(r"^\|\s*\n?", "", v).strip()
        if k in parsed:
            warnings.append(f"notes_edit: duplicate key {k!r} (kept first)")
            continue
        parsed[k] = v

    if not parsed:
        warnings.append("notes_edit: no recognizable key:value pairs")
        return None, warnings

    target = parsed.get("target", "").lower()
    op = parsed.get("op", "").lower()
    if target not in _NOTES_EDIT_TARGETS:
        warnings.append(f"notes_edit: invalid target {target!r}; must be user|work")
        return None, warnings
    if op not in _NOTES_EDIT_OPS:
        warnings.append(f"notes_edit: invalid op {op!r}; must be add|replace|remove")
        return None, warnings

    result: Dict[str, str] = {"target": target, "op": op}
    if op == "add":
        if not parsed.get("text"):
            warnings.append("notes_edit: op=add requires non-empty text")
            return None, warnings
        result["text"] = parsed["text"]
    elif op == "replace":
        if not parsed.get("old") or not parsed.get("new"):
            warnings.append("notes_edit: op=replace requires non-empty old AND new")
            return None, warnings
        result["old"] = parsed["old"]
        result["new"] = parsed["new"]
    elif op == "remove":
        if not parsed.get("text"):
            warnings.append("notes_edit: op=remove requires non-empty text")
            return None, warnings
        result["text"] = parsed["text"]

    return result, warnings


def build_disclosure(
    source_url: str,
    reasoning: str,
    authored_at: Optional[float] = None,
) -> Dict:
    """Build a disclosure dict ready for publish_proposal."""
    return {
        "source_url": source_url,
        "authored_at": authored_at or time.time(),
        "reasoning": reasoning,
        "observer_verification": {
            "status": "pending",
            "comment": "",
            "verified_at": None,
            "verified_by": None,
        },
    }


# Honest source marker for AUTONOMY proposals (reflection / self-propose / cron / drafter).
# These aren't authored in a chat — their basis is reflection's meta-analysis + the
# exogenous seed (DJ's memory archive), NOT a single citable file. The OLD default stamped
# them with "the most-recently-touched conversation file", a RANDOM, irrelevant citation
# the shadow verifier correctly FAILed — which blocked EVERY autonomy proposal from ever
# passing shadow (2026-05-31 Tier-1 e2e finding). This marker deliberately contains NO
# file-path (/home/user or ~/), NO conversation-id (12hex.json), NO prop-id pattern, so the
# verifier extracts no citation from it (nothing to mis-verify). The proposal is then judged
# on action-coherence + reasoning-specificity, with EVIDENCE honestly declaring
# "no external citation, internal reasoning only". (Path A enhancement later: thread the
# actual seed item that drove the topic so it cites its real source.)
INTERNAL_REASONING_SOURCE = (
    "internal-reasoning: roux reflection over the exogenous seed (DJ's memory archive) "
    "plus system self-observation — no single-file source"
)

# Author substrings that mark an autonomy/cron origin (no human chat behind the proposal).
_AUTONOMY_AUTHOR_HINTS = (
    "reflection", "self_propose", "propose", "cron", "watchtower",
    "autonomous", "ingest", "tier1", "test", "drafter",
)


def default_source_url(author: str) -> str:
    """Source_url when the caller doesn't pass one explicitly.

    AUTONOMY authors (reflection / self-propose / cron / drafter — no chat origin) get an
    honest internal-reasoning marker the shadow verifier won't treat as a (mis)citation.
    Chat-origin callers fall back to the most-recently-touched conversation file (where the
    authoring conversation lives), then META_POLICY.md."""
    a = (author or "").lower()
    if a.startswith("roux_") or "authored_by_roux" in a or any(h in a for h in _AUTONOMY_AUTHOR_HINTS):
        return INTERNAL_REASONING_SOURCE
    convs_dir = Path("/home/user/RouxYou/state/conversations")
    if convs_dir.exists():
        candidates = sorted(
            (p for p in convs_dir.glob("*.json") if p.name != "index.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return str(candidates[0])
    return "/home/user/RouxYou/governance/META_POLICY.md"
