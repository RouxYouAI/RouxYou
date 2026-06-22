#!/usr/bin/env python3
"""Build v5.3 train corpus = v5.2 corpus minus the 39 authoring examples.

v5.2 verdict (2026-05-28): authoring corpus was recursively contaminated
(46% of titles about self-propose loop pathology, the very thing the trained
LoRA was supposed to NOT do). Loop persisted in v5.2 thinking field with
rhetorical anaphora.

v5.3 strategy (Option C with the Opus 4.8 twist):
  - v5.3 voice LoRA = voice-only (241 examples, same as v4 had). Known-good.
  - Authoring lives as a SEPARATE LoRA on a fresh Opus-4.8-generated corpus,
    loaded independently when authoring role is invoked. Not folded in.

This script produces the v5.3 train corpus by filtering out anything whose
assistant message starts with '{' (the authoring-as-JSON marker from v5.2).
"""
import json
import sys
from pathlib import Path

SRC = Path("/home/user/TheSeed/voice_corpus/voice_corpus_v52_train.jsonl")
DST = Path("/home/user/TheSeed/voice_corpus/voice_corpus_v53_train.jsonl")


def is_authoring(messages: list) -> bool:
    asst = next((m["content"] for m in messages if m["role"] == "assistant"), "")
    return asst.startswith("{")


def main():
    n_total = 0
    n_voice = 0
    n_authoring_dropped = 0
    with SRC.open() as src, DST.open("w") as dst:
        for line in src:
            n_total += 1
            d = json.loads(line)
            if is_authoring(d.get("messages", [])):
                n_authoring_dropped += 1
                continue
            dst.write(line)
            n_voice += 1
    print(f"=== v5.3 voice-only corpus ===")
    print(f"Input:  {SRC}  ({n_total} examples)")
    print(f"Output: {DST}  ({n_voice} examples — voice only)")
    print(f"Dropped: {n_authoring_dropped} authoring examples (contaminated)")


if __name__ == "__main__":
    main()
