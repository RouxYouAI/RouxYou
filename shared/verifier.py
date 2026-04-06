"""
SILENT SEMANTIC DRIFT VERIFIER — Phase 39 (flag-only)
======================================================
Catches the failure mode where self-healing retry loops satisfy surface
constraints ("no errors") while silently dropping semantic constraints
("the thing the user actually asked for"). The retry loop closes
successfully on the wrong outcome.

Pipeline:
  1. extract_intent_spec(query, intent)  — runs at task submission,
     uses local gpt-oss:20b (informed_chat alias) to pull a structured
     list of checkable constraints from the user's request. JSON-mode.
  2. verify_task(query, spec, result, plan_steps) — runs at task
     completion, sends the spec + actual code artifacts + outputs to
     bigbrain (Claude Sonnet) for grading. Returns verdict + reasoning
     + missing constraints.

Design rules baked in:
  - Verifier is ADVISORY. Never blocks a task. Never raises.
  - On any failure (timeout, parse error, budget hit, kill switch),
    returns verdict="uncertain" or "skipped" with a reason.
  - Verifier sees actual code (file contents written/patched), not
    just the natural-language summary the worker produced. Without
    the code, the verifier rubber-stamps the worker's prose.
  - All payload sent to bigbrain is run through redact_credentials
    first to keep .env contents and tokens out of the cloud call.
  - Extraction uses informed_chat (cheap, local). Verification uses
    bigbrain (accurate, paid). The cost asymmetry is the whole point.
"""

import json
import time
import logging
from typing import Dict, List, Optional

import sys
from pathlib import Path
_BASE = Path(__file__).parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from shared.llm import llm_generate
from shared.permissions import redact_credentials
from shared.kill_switch import is_engaged as _kill_switch_engaged
from shared.verification_budget import (
    check_budget as _check_verify_budget,
    record_verification as _record_verify,
)

logger = logging.getLogger("verifier")


# ============================================================
# Defaults (overridable via config.yaml verifier section)
# ============================================================

DEFAULT_EXTRACTION_MODEL = "informed_chat"   # gpt-oss:20b alias
DEFAULT_VERIFICATION_MODEL = "bigbrain"      # claude-sonnet-4-6 alias
DEFAULT_EXTRACTION_TIMEOUT = 20
DEFAULT_VERIFICATION_TIMEOUT = 30
DEFAULT_VERIFICATION_MAX_TOKENS = 600

# Cap on per-artifact content sent to bigbrain (chars). Avoids blowing
# the input context on a single huge file write.
ARTIFACT_CHAR_CAP = 4000
COMMAND_OUTPUT_CAP = 500


def _get_config() -> Dict:
    """Read verifier section from config.yaml. Returns empty dict if
    config or pyyaml is unavailable. Caller falls back to defaults."""
    try:
        import yaml
        cfg_path = _BASE / "config.yaml"
        if not cfg_path.exists():
            return {}
        with open(cfg_path, "r") as f:
            data = yaml.safe_load(f) or {}
        return data.get("verifier", {}) or {}
    except Exception:
        return {}


# ============================================================
# JSON salvage helper
# ============================================================

def _salvage_json(text: str) -> Optional[Dict]:
    """Try hard to parse a JSON object out of an LLM response.

    Strategies in order:
      1. Direct json.loads on stripped text
      2. Find first '{' and last '}' and parse the slice
      3. If the slice fails, walk back from the end trimming chars until valid

    Returns the parsed dict, or None if all strategies fail.
    Always returns a dict (not lists or scalars) — caller wants a verdict object.
    """
    if not text:
        return None
    text = text.strip()

    # Strategy 1: direct parse
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        pass

    # Strategy 2: extract the first plausible JSON object slice
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    slice_text = text[start : end + 1]
    try:
        result = json.loads(slice_text)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        pass

    # Strategy 3: walk the end back to find a valid prefix
    # Useful when output is truncated mid-array but the front of the object is valid.
    # Try closing the open structures progressively.
    for trim_chars in range(1, min(800, len(slice_text) - start)):
        candidate = slice_text[: -trim_chars].rstrip().rstrip(",")
        # Try closing whatever's open
        for closer in ("]}", "}", '"]}', '"}'):
            try:
                result = json.loads(candidate + closer)
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                continue

    return None


# ============================================================
# Intent extraction
# ============================================================

EXTRACTION_SYSTEM_PROMPT = """You are an intent extractor for a code-execution agent system.

Your job: read the user's request and produce a structured list of checkable constraints — concrete, verifiable predicates that the system's output must satisfy for the task to be considered correctly completed.

Each constraint MUST:
- name a concrete VERB (add, remove, rename, ensure, include, exclude, write, save, create, delete, modify, patch, return, print, support, use, avoid, etc.)
- name a concrete TARGET (a file path, function name, variable, flag, value, behavior, dependency, etc.)
- describe an OBSERVABLE outcome (something a reviewer could check by reading the resulting code or output)

DO NOT include vague constraints like "be correct", "handle errors", "work properly", "look good". Those are not checkable.
DO NOT include implementation suggestions the user did not ask for.
DO NOT invent constraints the user did not actually request.

If the request is too vague to extract any concrete constraints, return an empty constraints list. That is a valid answer — do not invent.

Output JSON ONLY. No markdown, no commentary. Schema:
{
  "primary_goal": "<one sentence summarizing the user's overall goal>",
  "constraints": [
    {"verb": "<verb>", "target": "<concrete target>", "outcome": "<what should be observable>"}
  ]
}
"""


async def extract_intent_spec(query: str, intent: str) -> Dict:
    """
    Extract a structured list of checkable constraints from a user request.

    Runs as a fire-and-forget background task from the orchestrator at
    task submission time, then awaited just before verification. Uses
    gpt-oss:20b via the informed_chat alias.

    Returns a dict with:
        primary_goal: str
        constraints: list of {verb, target, outcome}
        extraction_model: str
        extracted_at: float
        extraction_error: str  (only on failure)

    NEVER raises. On any error, returns empty constraints + error string.
    """
    cfg = _get_config()
    model = cfg.get("extraction_model", DEFAULT_EXTRACTION_MODEL)
    timeout = float(cfg.get("timeouts", {}).get("extraction", DEFAULT_EXTRACTION_TIMEOUT))

    user_prompt = (
        f"User request (intent={intent}):\n\n{query}\n\n"
        f"Extract checkable constraints. Return JSON only."
    )

    try:
        # Note: NOT passing format="json" — reasoning models like gpt-oss:20b
        # route reasoning into a separate channel when format is forced, leaving
        # response empty. The system prompt is enough — the model produces JSON.
        response = await llm_generate(
            model,
            prompt=user_prompt,
            system=EXTRACTION_SYSTEM_PROMPT,
            temperature=0.1,
            max_tokens=1500,  # Generous — verbose models truncate at 600 mid-string
            timeout=timeout,
        )

        if not response.success:
            return {
                "primary_goal": query[:200],
                "constraints": [],
                "extraction_model": model,
                "extracted_at": time.time(),
                "extraction_error": f"LLM call failed: {response.error}",
            }

        text = (response.text or "").strip()

        # Strip thinking tags if present (some models still emit them)
        if "<think>" in text:
            text = text.split("</think>")[-1].strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()

        spec = _salvage_json(text)
        if spec is None:
            return {
                "primary_goal": query[:200],
                "constraints": [],
                "extraction_model": model,
                "extracted_at": time.time(),
                "extraction_error": f"JSON parse failed even after salvage; preview: {text[:200]}",
            }

        # Schema validation — be forgiving but reject obvious garbage
        if not isinstance(spec, dict):
            return {
                "primary_goal": query[:200],
                "constraints": [],
                "extraction_model": model,
                "extracted_at": time.time(),
                "extraction_error": "Extractor returned non-dict",
            }

        constraints = spec.get("constraints", [])
        if not isinstance(constraints, list):
            constraints = []

        # Coerce constraint shape — drop anything that doesn't look right
        clean_constraints = []
        for c in constraints:
            if isinstance(c, dict) and c.get("verb") and c.get("target"):
                clean_constraints.append({
                    "verb": str(c.get("verb", ""))[:50],
                    "target": str(c.get("target", ""))[:200],
                    "outcome": str(c.get("outcome", ""))[:300],
                })
            elif isinstance(c, str) and c.strip():
                # Some models return flat strings; preserve them in a wrapper shape
                clean_constraints.append({
                    "verb": "ensure",
                    "target": c.strip()[:200],
                    "outcome": c.strip()[:300],
                })

        return {
            "primary_goal": str(spec.get("primary_goal", query[:200]))[:300],
            "constraints": clean_constraints,
            "extraction_model": model,
            "extracted_at": time.time(),
        }

    except Exception as e:
        logger.warning(f"extract_intent_spec failed: {e}")
        return {
            "primary_goal": query[:200],
            "constraints": [],
            "extraction_model": model,
            "extracted_at": time.time(),
            "extraction_error": f"Exception: {str(e)[:100]}",
        }


# ============================================================
# Payload construction (the part that makes verification real)
# ============================================================

def _build_verification_payload(
    query: str,
    intent_spec: Dict,
    execution_result: Dict,
    plan_steps: Optional[List] = None,
) -> Dict:
    """
    Build the payload bigbrain sees for verification.

    Crucially: this includes the actual CODE that was written/patched,
    not just the worker's natural-language summary. Without the code,
    the verifier reads the same prose the worker generated and would
    just rubber-stamp it.

    All fields are run through redact_credentials before assembly so
    .env contents and tokens never reach the Anthropic API.

    Returns a compact dict the verification prompt embeds as JSON.
    """
    # Files written / patched / deployed — extract the actual content
    code_artifacts = []
    command_outputs = []
    files_read = []

    if plan_steps:
        # plan_steps may be list of dicts (from coder) or list of Step
        # objects from the worker; normalize via .get / getattr
        results_list = execution_result.get("results", []) if isinstance(execution_result, dict) else []
        # Build a step.id -> result map so we can correlate
        results_by_id = {}
        for r in results_list:
            if isinstance(r, dict):
                step_id = r.get("step")
                if step_id is not None:
                    results_by_id[step_id] = r

        for step in plan_steps:
            step_data = step if isinstance(step, dict) else {}
            action = step_data.get("action", "")
            details = step_data.get("details", "")
            content = step_data.get("content", "") or ""
            step_id = step_data.get("id")
            step_result = results_by_id.get(step_id, {}) if step_id is not None else {}
            inner = step_result.get("result", {}) if isinstance(step_result, dict) else {}

            if action in ("write_file", "patch_file", "deploy_patch"):
                # The code itself is the load-bearing thing
                if content:
                    artifact_text = content[:ARTIFACT_CHAR_CAP]
                    code_artifacts.append({
                        "action": action,
                        "path": redact_credentials(str(details)[:300]),
                        "content": redact_credentials(artifact_text),
                        "truncated": len(content) > ARTIFACT_CHAR_CAP,
                    })

            elif action == "run_command":
                # Capture short command + first chunk of output
                output = ""
                if isinstance(inner, dict):
                    output = inner.get("output") or inner.get("stdout") or ""
                if isinstance(output, str) and output.strip():
                    command_outputs.append({
                        "command": redact_credentials(str(details)[:300]),
                        "output": redact_credentials(output[:COMMAND_OUTPUT_CAP]),
                    })
                else:
                    command_outputs.append({
                        "command": redact_credentials(str(details)[:300]),
                        "output": "(no output)",
                    })

            elif action == "read_file":
                files_read.append(redact_credentials(str(details)[:300]))

    synthesized = execution_result.get("synthesized_response") or execution_result.get("response") or ""
    if synthesized:
        synthesized = redact_credentials(str(synthesized)[:2000])

    summary = execution_result.get("summary") or ""
    if summary:
        summary = redact_credentials(str(summary)[:1500])

    return {
        "original_query": redact_credentials(query[:1500]),
        "primary_goal": intent_spec.get("primary_goal", ""),
        "constraints": intent_spec.get("constraints", []),
        "synthesized_response": synthesized,
        "worker_summary": summary,
        "code_artifacts": code_artifacts,
        "command_outputs": command_outputs,
        "files_read": files_read,
    }


# ============================================================
# Verification (bigbrain call)
# ============================================================

VERIFICATION_SYSTEM_PROMPT = """You are a verification agent for a code-execution AI system.

You will be given:
- The user's ORIGINAL request
- A list of CONSTRAINTS that were extracted from that request at submission time (each is a {verb, target, outcome} predicate)
- A WORKER SUMMARY (natural language description of what the agent did)
- CODE ARTIFACTS (the actual file content that was written or patched)
- COMMAND OUTPUTS (truncated outputs from any shell commands the agent ran)

Your job: grade whether the EXECUTION actually satisfies the original CONSTRAINTS. You are looking for SILENT SEMANTIC DRIFT — cases where the worker reports success but actually dropped one or more of the user's requirements while fixing some intermediate error.

Rules:
- Read the CODE ARTIFACTS, not just the worker summary. The summary is unreliable; the code is ground truth.
- For each constraint, check whether the code/output actually satisfies the predicate.
- If ALL constraints are satisfied: verdict is "pass".
- If ANY constraint is dropped or unsatisfied: verdict is "fail" and list the missing constraints.
- If the evidence is insufficient (no code artifacts, no useful outputs, etc.): verdict is "uncertain".
- DO NOT invent constraints that were not in the input list. Grade ONLY against what was extracted.
- DO NOT penalize the agent for things the user did not ask for.
- Be concise. Reasoning under 200 words.

Output JSON ONLY. No markdown. Schema:
{
  "verdict": "pass" | "fail" | "uncertain",
  "reason": "<one paragraph explaining the verdict>",
  "missing_constraints": ["<verb> <target>", ...]   // empty list if pass
}
"""


async def verify_task(
    original_query: str,
    intent_spec: Dict,
    execution_result: Dict,
    plan_steps: Optional[List] = None,
) -> Dict:
    """
    Verify a completed task against its captured intent spec.

    Returns a dict with:
        verdict: "pass" | "fail" | "uncertain" | "skipped"
        reason: str
        missing_constraints: list[str]
        verifier_model: str
        verified_at: float

    NEVER raises. On any error, returns verdict="uncertain" with the
    error in reason. The verifier is advisory — a broken verifier must
    not break the task pipeline.
    """
    cfg = _get_config()
    model = cfg.get("verification_model", DEFAULT_VERIFICATION_MODEL)
    timeout = float(cfg.get("timeouts", {}).get("verification", DEFAULT_VERIFICATION_TIMEOUT))

    now = time.time()

    # ---- Skip checks (cheap, local, before any LLM call) ----

    if not cfg.get("enabled", True):
        return {
            "verdict": "skipped",
            "reason": "verifier disabled in config.yaml",
            "missing_constraints": [],
            "verifier_model": model,
            "verified_at": now,
        }

    if _kill_switch_engaged():
        return {
            "verdict": "skipped",
            "reason": "kill switch engaged — verifier short-circuited",
            "missing_constraints": [],
            "verifier_model": model,
            "verified_at": now,
        }

    constraints = intent_spec.get("constraints", []) if isinstance(intent_spec, dict) else []
    if not constraints:
        return {
            "verdict": "skipped",
            "reason": (
                intent_spec.get("extraction_error", "no constraints extracted")
                if isinstance(intent_spec, dict)
                else "no constraints extracted"
            ),
            "missing_constraints": [],
            "verifier_model": model,
            "verified_at": now,
        }

    allowed, budget_info = _check_verify_budget()
    if not allowed:
        return {
            "verdict": "skipped",
            "reason": (
                f"verification_budget_exhausted "
                f"({budget_info.get('used')}/{budget_info.get('max')} this hour)"
            ),
            "missing_constraints": [],
            "verifier_model": model,
            "verified_at": now,
        }

    # ---- Build payload (with redaction) ----

    try:
        payload = _build_verification_payload(
            original_query, intent_spec, execution_result, plan_steps
        )
    except Exception as e:
        logger.warning(f"verifier payload build failed: {e}")
        return {
            "verdict": "uncertain",
            "reason": f"payload build failed: {str(e)[:150]}",
            "missing_constraints": [],
            "verifier_model": model,
            "verified_at": now,
        }

    # ---- Bigbrain call ----

    user_prompt = (
        "Verify the following task execution against the extracted constraints.\n\n"
        f"```json\n{json.dumps(payload, indent=2)[:8000]}\n```\n\n"
        "Return ONLY the JSON verdict object."
    )

    try:
        response = await llm_generate(
            model,
            prompt=user_prompt,
            system=VERIFICATION_SYSTEM_PROMPT,
            temperature=0.1,
            max_tokens=DEFAULT_VERIFICATION_MAX_TOKENS,
            timeout=timeout,
        )
    except Exception as e:
        logger.warning(f"verifier llm call exception: {e}")
        return {
            "verdict": "uncertain",
            "reason": f"verifier call exception: {str(e)[:150]}",
            "missing_constraints": [],
            "verifier_model": model,
            "verified_at": now,
        }

    if not response.success:
        return {
            "verdict": "uncertain",
            "reason": f"verifier llm failure: {response.error[:150] if response.error else 'unknown'}",
            "missing_constraints": [],
            "verifier_model": model,
            "verified_at": now,
        }

    # ---- Parse verdict ----

    text = (response.text or "").strip()
    if "<think>" in text:
        text = text.split("</think>")[-1].strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    verdict_obj = _salvage_json(text)
    if verdict_obj is None:
        return {
            "verdict": "uncertain",
            "reason": f"verifier output parse failed; preview: {text[:200]}",
            "missing_constraints": [],
            "verifier_model": model,
            "verified_at": now,
        }

    raw_verdict = str(verdict_obj.get("verdict", "uncertain")).lower().strip()
    if raw_verdict not in ("pass", "fail", "uncertain"):
        raw_verdict = "uncertain"

    reason = str(verdict_obj.get("reason", ""))[:1500]

    missing = verdict_obj.get("missing_constraints", []) or []
    if not isinstance(missing, list):
        missing = []
    missing = [str(m)[:200] for m in missing if m]

    # Record budget consumption ONLY for successful verifier calls
    try:
        _record_verify()
    except Exception as e:
        logger.warning(f"verify budget record failed: {e}")

    return {
        "verdict": raw_verdict,
        "reason": reason,
        "missing_constraints": missing,
        "verifier_model": model,
        "verified_at": now,
    }
