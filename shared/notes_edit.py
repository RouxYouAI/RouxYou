"""Substring add/replace/remove API for Roux's notes files.

Mirrors the Hermes Agent edit API (add / replace / remove) but governed:
operations route through /propose category=notes_edit so the shadow +
resolver chain reviews every mutation. Direct calls bypass governance —
only the executor handler should invoke these.

Frozen-at-start semantics: writes persist to disk immediately, but the
in-prompt copy doesn't refresh until the companion module reloads (cached
at import). Matches Hermes design.
"""
from pathlib import Path
from typing import Literal

NotesTarget = Literal["user", "work"]

_PATHS: dict[str, Path] = {
    "user": Path("/home/user/RouxYou/calibration/roux_notes_user.md"),
    "work": Path("/home/user/RouxYou/calibration/roux_notes_work.md"),
}

_CAPS: dict[str, int] = {
    "user": 1500,
    "work": 2500,
}


def _resolve(target: str) -> tuple[Path, int]:
    if target not in _PATHS:
        raise ValueError(f"unknown notes target {target!r} (expected: user|work)")
    return _PATHS[target], _CAPS[target]


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_or_refuse(path: Path, new_text: str, cap: int) -> dict:
    if len(new_text) > cap:
        return {
            "ok": False,
            "error": f"would exceed cap ({len(new_text)} > {cap})",
            "path": str(path),
            "after_len": len(new_text),
        }
    before = _read(path)
    path.write_text(new_text, encoding="utf-8")
    return {
        "ok": True,
        "path": str(path),
        "before_len": len(before),
        "after_len": len(new_text),
    }


def notes_add(target: NotesTarget, text: str) -> dict:
    """Append a paragraph to the notes file.

    Returns {ok, path, before_len, after_len} or {ok: False, error}.
    Adds a leading newline if the file doesn't already end with one.
    """
    path, cap = _resolve(target)
    current = _read(path)
    sep = "" if current.endswith("\n\n") else ("\n" if current.endswith("\n") else "\n\n")
    candidate = current + sep + text.rstrip() + "\n"
    return {"action": "add", **_write_or_refuse(path, candidate, cap)}


def notes_replace(target: NotesTarget, old: str, new: str) -> dict:
    """Substring replace. Errors if `old` appears 0 or >1 times (ambiguity protection)."""
    path, cap = _resolve(target)
    current = _read(path)
    count = current.count(old)
    if count == 0:
        return {"action": "replace", "ok": False, "error": "old substring not found", "path": str(path)}
    if count > 1:
        return {
            "action": "replace",
            "ok": False,
            "error": f"old substring matches {count} places — must be unique",
            "path": str(path),
        }
    candidate = current.replace(old, new, 1)
    return {"action": "replace", **_write_or_refuse(path, candidate, cap)}


def notes_remove(target: NotesTarget, text: str) -> dict:
    """Substring remove. Errors if `text` appears 0 or >1 times."""
    path, cap = _resolve(target)
    current = _read(path)
    count = current.count(text)
    if count == 0:
        return {"action": "remove", "ok": False, "error": "text not found", "path": str(path)}
    if count > 1:
        return {
            "action": "remove",
            "ok": False,
            "error": f"text matches {count} places — must be unique",
            "path": str(path),
        }
    candidate = current.replace(text, "", 1)
    return {"action": "remove", **_write_or_refuse(path, candidate, cap)}


def apply_edit(op: dict) -> dict:
    """Single-entry dispatch for the proposal executor.

    Expected op shape:
        {"target": "user"|"work", "op": "add"|"replace"|"remove", ...}
    where the remaining fields depend on op:
        add:     {"text": "..."}
        replace: {"old": "...", "new": "..."}
        remove:  {"text": "..."}
    """
    target = op.get("target")
    kind = op.get("op")
    try:
        if kind == "add":
            return notes_add(target, op["text"])
        if kind == "replace":
            return notes_replace(target, op["old"], op["new"])
        if kind == "remove":
            return notes_remove(target, op["text"])
        return {"ok": False, "error": f"unknown op {kind!r}"}
    except KeyError as e:
        return {"ok": False, "error": f"missing field: {e.args[0]}"}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
