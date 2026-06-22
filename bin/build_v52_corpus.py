#!/usr/bin/env python3
"""Build v5.2 train corpus from v5 train corpus + authoring_pairs.

v5/v5.1 looped on long-gen because the 40 authoring examples used a rigid
TITLE:/CATEGORY:/PRIORITY:/... header skeleton — the model learned "header
begets header, bullet begets bullet" and couldn't escape that rhythm.

v5.2 fix: re-render those 40 authoring assistant messages as JSON objects
AND rewrite the matching user-prompt instruction from "Each section header
is on its own line, in CAPS, followed by a colon" to "Output a single JSON
object with these fields...".

Strategy: take the existing v5 train file (280 examples); for each example
whose assistant message starts with rigid TITLE:/CATEGORY: format, rewrite
both sides of the pair. The 240 voice examples pass through unchanged.

Output: voice_corpus_v52_train.jsonl in the same dir (280 lines).
"""
import json
import re
import sys
from pathlib import Path

SRC = Path("/home/user/TheSeed/voice_corpus/voice_corpus_v5_train.jsonl")
DST = Path("/home/user/TheSeed/voice_corpus/voice_corpus_v52_train.jsonl")

# Rigid-format markers in the v5 user prompt (what we replace)
RIGID_USER_INSTRUCTION = (
    "Your output must follow the EXACT format below. Each section header is on its own line, "
    "in CAPS, followed by a colon."
)
RIGID_USER_DONT_PREAMBLE = (
    "Do NOT add commentary before TITLE: or after the REASONING section finishes.\n"
    "Do NOT use markdown formatting (no **, no #, no bullet points"
)

# v5.2 replacements (JSON-shaped)
JSON_USER_INSTRUCTION = (
    "Your output must be a single JSON object containing these keys: "
    "title, category, priority, description, proposed_action, evidence, reasoning."
)
JSON_USER_DONT_PREAMBLE = (
    "Output ONLY the JSON object. No preamble, no commentary before or after.\n"
    "No markdown code fences (no ```), no extra prose"
)

# Required keys we expect to find in every parsed authoring target
REQUIRED_KEYS = ("title", "category", "priority", "description",
                 "proposed_action", "evidence", "reasoning")


def parse_rigid_authoring_response(text: str) -> dict | None:
    """Parse the v5 rigid TITLE:/CATEGORY:/... format into a dict.

    Returns None if it doesn't look like a rigid authoring response.
    """
    if not text or "TITLE:" not in text or "CATEGORY:" not in text:
        return None
    # Match SECTION_NAME: <content until next ALL-CAPS section or end>
    pattern = re.compile(
        r"^(TITLE|CATEGORY|PRIORITY|DESCRIPTION|PROPOSED_ACTION|EVIDENCE|REASONING):\s*",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if len(matches) < 4:  # not enough sections to look real
        return None
    out = {}
    for i, m in enumerate(matches):
        key = m.group(1).lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[start:end].strip()
        # Coerce priority to int if possible
        if key == "priority":
            try:
                value = int(value)
            except ValueError:
                pass
        out[key] = value
    return out


def rewrite_user_prompt(usr: str) -> str:
    """Swap the rigid-format instruction blocks for JSON instructions."""
    new = usr.replace(RIGID_USER_INSTRUCTION, JSON_USER_INSTRUCTION)
    new = new.replace(RIGID_USER_DONT_PREAMBLE, JSON_USER_DONT_PREAMBLE)
    return new


def main():
    n_total = 0
    n_authoring = 0
    n_passthrough = 0
    n_failed_parse = 0
    n_missing_keys = 0
    with SRC.open() as src, DST.open("w") as dst:
        for line in src:
            n_total += 1
            d = json.loads(line)
            msgs = d.get("messages", [])
            if not msgs:
                dst.write(line)
                n_passthrough += 1
                continue
            asst = next((m["content"] for m in msgs if m["role"] == "assistant"), "")
            parsed = parse_rigid_authoring_response(asst)
            if parsed is None:
                # voice example or non-rigid authoring — pass through
                dst.write(line)
                n_passthrough += 1
                continue
            # Authoring example — rewrite both sides
            missing = [k for k in REQUIRED_KEYS if k not in parsed]
            if missing:
                n_missing_keys += 1
                print(f"WARN: line {n_total} missing keys {missing} — passing through unchanged",
                      file=sys.stderr)
                dst.write(line)
                continue
            # Rewrite user prompt
            new_msgs = []
            for m in msgs:
                if m["role"] == "user":
                    new_msgs.append({"role": "user", "content": rewrite_user_prompt(m["content"])})
                elif m["role"] == "assistant":
                    new_msgs.append({"role": "assistant",
                                     "content": json.dumps(parsed, indent=2, ensure_ascii=False)})
                else:
                    new_msgs.append(m)
            dst.write(json.dumps({"messages": new_msgs}, ensure_ascii=False) + "\n")
            n_authoring += 1
    print(f"\n=== RESULT ===")
    print(f"Total examples processed: {n_total}")
    print(f"  Authoring (rewritten to JSON): {n_authoring}")
    print(f"  Voice / passthrough: {n_passthrough}")
    print(f"  Authoring with missing keys (passed through): {n_missing_keys}")
    print(f"Output: {DST}")


if __name__ == "__main__":
    main()
