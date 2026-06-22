"""
Verify-Toolbelt — read-only grounding tools for v5.3-as-bigbrain verdicts
==========================================================================
2026-06-18 (Phase 2 of the v5.3-as-bigbrain build). Anthropic credits ran out →
the gate is local v5.3 now. v5.3's one failure mode as a verifier is CONFABULATION:
it will reason about a file/precedent it can't see and invent specifics (the S12
"June-16 patch that didn't exist"). The cure designed in S12: give the verdict path
an actual READ-ONLY verification toolbelt so it CHECKS instead of guessing — then it
can only escalate what no tool can reach.

This module provides:
  - A small set of SANDBOXED, read-only tools (path_exists/ls/read_file/grep/ledger_query),
    confined to the RouxYou root, refusing secrets, output-capped.
  - OpenAI-style tool schemas for the llama-server (:8090, served --jinja, native tool-calls).
  - execute_tool() dispatcher.
  - grounded_verify(): the tool-calling loop — v5.3 requests a tool, we run it, feed the
    real result back, repeat (capped), then it produces a grounded verdict.

SAFETY: every tool is read-only and sandboxed. No writes, no shell, no path escapes,
no .env/key/secret reads. Worst case a tool returns "denied" or "not found" — which is
exactly the grounding we want (a confab dies when the lookup comes back empty).
"""

import json
import os
import re
import sys
import time
import requests
from pathlib import Path
from typing import Optional, Tuple, List, Dict

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.logger import get_logger

logger = get_logger("verify_toolbelt")

ROOT = os.path.realpath(str(_PROJECT_ROOT))

V53_CHAT_URL = "http://localhost:8090/v1/chat/completions"
V53_MODEL    = "roux-v53"

# Caps — keep tool output small so it grounds without flooding the context.
MAX_READ_LINES   = 120
MAX_GREP_MATCHES = 40
MAX_LS_ENTRIES   = 200
MAX_FILE_BYTES   = 256 * 1024
_SKIP_DIRS       = {".git", "venv", ".venv", "node_modules", "__pycache__", ".mypy_cache"}
_SENSITIVE_RE    = re.compile(r"(^\.env$|\.env\.|\.env$|\.key$|\.pem$|^id_|_rsa$|secret|password|token)", re.IGNORECASE)


def _safe_path(path: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve `path` under ROOT. Returns (realpath, None) or (None, deny_reason).
    Rejects path escapes and sensitive files (defense-in-depth, read-only anyway)."""
    if not path or not str(path).strip():
        return None, "empty path"
    p = str(path).strip()
    cand = p if os.path.isabs(p) else os.path.join(ROOT, p)
    real = os.path.realpath(cand)
    try:
        if os.path.commonpath([real, ROOT]) != ROOT:
            return None, f"path '{p}' escapes the RouxYou root — denied"
    except ValueError:
        return None, f"path '{p}' invalid — denied"
    if _SENSITIVE_RE.search(os.path.basename(real)):
        return None, f"'{os.path.basename(real)}' looks like a secret — denied (read-only toolbelt won't expose credentials)"
    return real, None


# ─────────────────────────── tools ───────────────────────────

def _tool_path_exists(path: str = "") -> str:
    real, deny = _safe_path(path)
    if deny:
        return deny
    if os.path.exists(real):
        kind = "directory" if os.path.isdir(real) else "file"
        return f"YES — '{path}' exists ({kind})."
    return f"NO — '{path}' does not exist."


def _tool_ls(path: str = ".") -> str:
    real, deny = _safe_path(path or ".")
    if deny:
        return deny
    if not os.path.exists(real):
        return f"NO — '{path}' does not exist."
    if not os.path.isdir(real):
        return f"'{path}' is a file, not a directory."
    try:
        entries = sorted(os.listdir(real))
    except Exception as e:
        return f"error listing '{path}': {e}"
    out = []
    for e in entries[:MAX_LS_ENTRIES]:
        full = os.path.join(real, e)
        out.append(f"{e}/" if os.path.isdir(full) else e)
    extra = f"\n... (+{len(entries) - MAX_LS_ENTRIES} more)" if len(entries) > MAX_LS_ENTRIES else ""
    return f"{len(entries)} entries in '{path}':\n" + "\n".join(out) + extra


def _tool_read_file(path: str = "", max_lines: int = MAX_READ_LINES) -> str:
    real, deny = _safe_path(path)
    if deny:
        return deny
    if not os.path.exists(real):
        return f"NO — '{path}' does not exist."
    if os.path.isdir(real):
        return f"'{path}' is a directory; use ls."
    try:
        if os.path.getsize(real) > MAX_FILE_BYTES:
            return f"'{path}' is large (> {MAX_FILE_BYTES//1024}KB); use grep to find specific lines."
        with open(real, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"error reading '{path}': {e}"
    try:
        cap = min(int(max_lines), MAX_READ_LINES)
    except (TypeError, ValueError):
        cap = MAX_READ_LINES
    body = "".join(lines[:cap])
    extra = f"\n... (+{len(lines) - cap} more lines; {path} has {len(lines)} total)" if len(lines) > cap else ""
    return f"--- {path} (showing {min(cap, len(lines))}/{len(lines)} lines) ---\n{body}{extra}"


def _tool_grep(pattern: str = "", path: str = ".") -> str:
    if not pattern:
        return "empty pattern"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"invalid regex: {e}"
    base, deny = _safe_path(path or ".")
    if deny:
        return deny
    if not os.path.exists(base):
        return f"NO — '{path}' does not exist."
    targets: List[str] = []
    if os.path.isfile(base):
        targets = [base]
    else:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if _SENSITIVE_RE.search(fn):
                    continue
                targets.append(os.path.join(dirpath, fn))
    matches: List[str] = []
    for fp in targets:
        if len(matches) >= MAX_GREP_MATCHES:
            break
        try:
            if os.path.getsize(fp) > MAX_FILE_BYTES:
                continue
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        rel = os.path.relpath(fp, ROOT)
                        matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(matches) >= MAX_GREP_MATCHES:
                            break
        except Exception:
            continue
    if not matches:
        return f"NO matches for /{pattern}/ under '{path}'."
    capped = f"\n... (capped at {MAX_GREP_MATCHES})" if len(matches) >= MAX_GREP_MATCHES else ""
    return f"{len(matches)} match(es) for /{pattern}/:\n" + "\n".join(matches) + capped


def _tool_ledger_query(tier: str = "") -> str:
    try:
        from shared.trust_ledger import readiness_report
        rpt = readiness_report(tier or None)
        return "TRUST LEDGER readiness:\n" + json.dumps(rpt, indent=2, default=str)[:2000]
    except Exception as e:
        return f"ledger query failed: {e}"


_TOOLS = {
    "path_exists":  _tool_path_exists,
    "ls":           _tool_ls,
    "read_file":    _tool_read_file,
    "grep":         _tool_grep,
    "ledger_query": _tool_ledger_query,
}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "path_exists",
        "description": "Check whether a file or directory exists in the RouxYou repo. Use this BEFORE claiming a file exists.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "repo-relative path, e.g. 'shared/coach.py'"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "ls",
        "description": "List the contents of a directory in the RouxYou repo.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "repo-relative dir, e.g. 'state/' (default '.')"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read the first lines of a file in the RouxYou repo. Use to verify a claim about file contents.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "max_lines": {"type": "integer", "description": f"default {MAX_READ_LINES}"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Search the repo (or a subpath) for a regex pattern. Use to verify a function/string/precedent actually exists before citing it.",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string", "description": "repo-relative scope (default whole repo)"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "ledger_query",
        "description": "Read the trust-ledger readiness report (per-tier clean/false-pass/false-block counts). Use to verify a claim about the ledger state.",
        "parameters": {"type": "object", "properties": {"tier": {"type": "string", "description": "optional tier e.g. 'code','memory','config'"}}, "required": []}}},
]


_TOOL_INSTRUCTION = (
    "\n\nYou have READ-ONLY verification tools — path_exists, ls, read_file, grep, "
    "ledger_query — scoped to the RouxYou repo. USE them to check every concrete claim "
    "(file paths, function/string existence, cited precedents, trust-ledger state) BEFORE "
    "you rely on it. If a lookup comes back empty, the claim is unsupported — say so rather "
    "than assuming it's true. Verify, don't defend; only escalate what no tool can reach."
)


def execute_tool(name: str, arguments: dict) -> str:
    """Dispatch a tool call to its sandboxed implementation. Always returns a string."""
    fn = _TOOLS.get(name)
    if not fn:
        return f"unknown tool '{name}'"
    try:
        args = arguments if isinstance(arguments, dict) else {}
        return str(fn(**args))[:4000]
    except TypeError as e:
        return f"bad arguments for '{name}': {e}"
    except Exception as e:
        return f"tool '{name}' error: {e}"


_CODE_REF_RE = re.compile(r"`([^`\n]{2,80})`")
_FUNC_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\(\))?$")


def _func_source(grep_hit: str, max_lines: int = 28) -> str:
    """From a _tool_grep hit for a function/class def, return its ACTUAL source body (LIVE copy,
    skipping archive/ duplicates) so the verifier can check a proposal's BEHAVIORAL claim against
    the real code — not just confirm the symbol exists (GAP-1 fix 2026-06-19: catches 'real file,
    fabricated behavior' false-passes like prop_da5875806360)."""
    for line in grep_hit.splitlines():
        m = re.match(r"^([\w./\-]+\.py):(\d+):", line)
        if not m:
            continue
        path, lineno = m.group(1), int(m.group(2))
        if path.startswith("archive/") or "/archive/" in path:
            continue
        real, deny = _safe_path(path)
        if deny:
            continue
        try:
            with open(real, "r", encoding="utf-8", errors="replace") as f:
                src = f.readlines()
        except Exception:
            continue
        start = lineno - 1
        if start < 0 or start >= len(src):
            continue
        def_indent = len(src[start]) - len(src[start].lstrip())
        body = [src[start].rstrip("\n")]
        for ln in src[start + 1: start + 1 + max_lines]:
            s = ln.strip()
            ind = len(ln) - len(ln.lstrip())
            if s and ind <= def_indent and s.startswith(("def ", "class ", "@")):
                break  # next sibling def/class = end of this function
            body.append(ln.rstrip("\n"))
        return f"{path}:{lineno}\n" + "\n".join(body)
    return ""


def cited_targets_report(text: str) -> Tuple[str, List[str]]:
    """MECHANICAL grounding pre-check (2026-06-18): extract the code targets a proposal NAMES
    (backtick-quoted file paths, dirs, function names) and check on disk whether each EXISTS.
    Returns (report_string, missing_list). The report is injected into the verdict prompt so
    NO reviewer — v5.3 OR a human — can pass a proposal that cites a non-existent function/path
    without seeing it flagged. Born from a real double-false-pass (#2 cited `_is_unaddressed_workitem`
    + `state/drafts/`, neither existed; both the gate AND Claude-in-review missed it). Defense in depth
    can't lean on anyone REMEMBERING to check — so we check mechanically and show the facts."""
    text = text or ""
    refs: List[str] = []

    def _add(r):
        r = (r or "").strip().strip("`").rstrip(".,;:)")
        if r and len(r) >= 3 and r not in refs:
            refs.append(r)

    # backtick-quoted code refs
    for m in _CODE_REF_RE.findall(text):
        _add(m)
    # repo-relative dir/file paths (high-signal prefixes)
    for m in re.findall(r"\b((?:state|shared|services|orchestrator|coder|worker|bin|docs|tests)/[\w./\-]+)", text):
        _add(m)
    # any file path with a known code/data extension
    for m in re.findall(r"\b([\w./\-]+\.(?:py|md|json|sh|ya?ml|txt))\b", text):
        _add(m)
    # explicit function-call syntax: name()
    for m in re.findall(r"\b([A-Za-z_]\w{2,})\(\)", text):
        _add(m)
    # "function/method/helper NAME" and "NAME function/method"
    for m in re.findall(r"(?:function|method|helper)\s+`?([A-Za-z_]\w{2,})`?", text, re.I):
        _add(m)
    for m in re.findall(r"\b([A-Za-z_]\w{2,})`?\s+(?:function|method|helper)\b", text, re.I):
        _add(m)
    lines, missing = [], []
    for r in refs[:25]:
        fn = r[:-2] if r.endswith("()") else r
        if ("/" in r) or r.endswith((".py", ".md", ".json", ".sh", ".yaml", ".yml", ".txt")):
            # path or file
            real, deny = _safe_path(r.rstrip("/"))
            if deny:
                lines.append(f"- `{r}`: {deny}")
            elif os.path.exists(real):
                lines.append(f"- `{r}`: EXISTS ({'dir' if os.path.isdir(real) else 'file'})")
            else:
                lines.append(f"- `{r}`: ❌ DOES NOT EXIST")
                missing.append(r)
        elif _FUNC_RE.match(r) and ("_" in fn or r.endswith("()")):
            # looks like a function/identifier — grep for its definition
            hit = _tool_grep(rf"(def|class)\s+{re.escape(fn)}\b", ".")
            if hit.startswith("NO matches"):
                lines.append(f"- `{r}`: ❌ NO def/class found in codebase")
                missing.append(r)
            else:
                src = _func_source(hit)
                if src:
                    lines.append(f"- `{r}`: defined — ACTUAL SOURCE below; check the proposal's CLAIM about it against this code:\n```python\n{src}\n```")
                else:
                    lines.append(f"- `{r}`: defined ({hit.splitlines()[1][:70] if len(hit.splitlines())>1 else 'found'})")
    if not lines:
        return "", []
    header = ("MECHANICAL GROUNDING CHECK (checked on disk just now — authoritative). The proposal "
              "names these targets:\n" + "\n".join(lines))
    if missing:
        header += ("\n\n⚠️ TARGETS THAT DO NOT EXIST: " + ", ".join(f"`{m}`" for m in missing) +
                   ". If the proposal claims to MODIFY/edit/reuse any of these, it is FABRICATED — "
                   "FAIL it. (Only OK if the proposal explicitly CREATES them.)")
    if "ACTUAL SOURCE below" in header:
        header += ("\n\nBEHAVIORAL CHECK: for any target shown WITH its source above, verify the proposal's "
                   "claim about what it does AGAINST the actual code. If the proposal says the function does X "
                   "but the source shows it does not (the behavior/branch isn't there), that is FABRICATED "
                   "BEHAVIOR — FAIL it, exactly like a fabricated citation. A real symbol does NOT make a false "
                   "claim about it true.")
    return header, missing


def grounded_verify(prompt: str, system: str = "", *, max_tokens: int = 4096,
                    temperature: float = 0.3, timeout: int = 180,
                    max_tool_iters: int = 5) -> Dict:
    # NOTE on max_tokens (2026-06-18, DJ's call): v5.3 is LOCAL — tokens are free, so this
    # is a RUNAWAY GUARD, not a cost budget. It must be GENEROUS: v5.3 reasons for a few
    # hundred tokens before the verdict/tool-call, and a tight cap truncates that reasoning
    # BEFORE the answer (the old "empty verdict / no tools" failures). Let it finish naturally
    # (finish_reason=stop); only this high ceiling stops a pathological loop.
    """Run a v5.3 verdict call WITH the read-only verify-toolbelt.

    v5.3 may request tools (path_exists/ls/read_file/grep/ledger_query); we execute
    them sandboxed and feed real results back, looping until it answers (or the iter
    cap). The final answer is grounded in actual lookups. Returns:
      {"text": <final content>, "success": bool, "tool_trace": [...], "iters": n, "error": str?}
    The tool_trace is an audit record of what it checked — attach to the verdict log.
    """
    sys_full = (system + _TOOL_INSTRUCTION) if system else _TOOL_INSTRUCTION.strip()
    messages = [{"role": "system", "content": sys_full},
                {"role": "user", "content": prompt}]
    trace: List[Dict] = []

    for it in range(max_tool_iters + 1):
        # On the last allowed turn, force a final answer (no more tools).
        force_final = (it == max_tool_iters)
        payload = {
            "model": V53_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if not force_final:
            payload["tools"] = TOOL_SCHEMAS
            payload["tool_choice"] = "auto"
        else:
            payload["tool_choice"] = "none"
        try:
            r = requests.post(V53_CHAT_URL, json=payload, timeout=timeout)
        except requests.Timeout:
            return {"text": "", "success": False, "tool_trace": trace, "iters": it,
                    "error": f"v53 timed out after {timeout}s"}
        except Exception as e:
            return {"text": "", "success": False, "tool_trace": trace, "iters": it, "error": str(e)}
        if r.status_code != 200:
            return {"text": "", "success": False, "tool_trace": trace, "iters": it,
                    "error": f"v53 HTTP {r.status_code}"}
        choice = (r.json().get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        tool_calls = msg.get("tool_calls") or []

        if tool_calls and not force_final:
            # Echo the assistant's tool-call message, then append each tool result.
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = (tc.get("function") or {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                result = execute_tool(name, args)
                trace.append({"tool": name, "args": args, "result_preview": result[:200]})
                logger.info(f"verify-tool: {name}({args}) -> {result[:80]!r}")
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "name": name, "content": result})
            continue  # loop for the next model turn with tool results in context

        # No tool calls (or forced-final): this is the answer.
        return {"text": msg.get("content") or "", "success": True,
                "tool_trace": trace, "iters": it, "error": None}

    return {"text": "", "success": False, "tool_trace": trace, "iters": max_tool_iters,
            "error": "exhausted tool iterations without a final answer"}
