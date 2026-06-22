#!/usr/bin/env python3
"""Local merge: LoRA adapter (v5.2) + base (Qwen3-30B-A3B-bnb-4bit) → bf16 HF shards.

Split-workflow step: this runs on DJ's box after the pod-trained LoRA
adapter has been scp'd home. The pod was terminated; everything from here
is CPU/GPU local.

Layout assumptions:
- HF_HOME points at /mnt/sata/.hf-cache (base model already downloaded there)
- LoRA adapter at /mnt/sata/roux-v52/lora_qwen3_roux_v52/ (130MB, scp'd from pod)
- Output written to /mnt/sata/roux-v52/merged_qwen3_roux_v52/ (~57GB bf16 shards)

unsloth's save_pretrained_merged with save_method="merged_16bit" does the
merge layer-by-layer, so peak RAM usage stays well under model size.
"""
import os

# CRITICAL: set HF_HOME before importing transformers/unsloth — otherwise they
# read the default cache (~/.cache/huggingface on root, which is tight).
os.environ["HF_HOME"] = "/mnt/sata/.hf-cache"
os.environ["UNSLOTH_COMPILE_DISABLE"] = "1"

import sys
import time
from unsloth import FastLanguageModel

ADAPTER = "/mnt/sata/roux-v52/lora_qwen3_roux_v52"
MERGED_OUT = "/mnt/sata/roux-v52/merged_qwen3_roux_v52"
MAX_SEQ = 4096

print(f"=== v5.2 LOCAL MERGE ===", flush=True)
print(f"adapter: {ADAPTER}", flush=True)
print(f"merged out: {MERGED_OUT}", flush=True)
print(f"HF_HOME: {os.environ['HF_HOME']}", flush=True)

t0 = time.time()
print(f"\n>> [{time.strftime('%H:%M:%S')}] loading adapter (auto-pulls base from cache) ...", flush=True)

# FastLanguageModel.from_pretrained on an ADAPTER path reads adapter_config.json
# → identifies base model (unsloth/Qwen3-30B-A3B-bnb-4bit) → loads base + applies adapter.
# device_map="auto" lets bitsandbytes spill the 4-bit base across GPU + CPU
# (we have 16GB VRAM, base is 18GB at 4-bit, so ~3GB will spill to CPU).
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name=ADAPTER,
    max_seq_length=MAX_SEQ,
    load_in_4bit=True,
    device_map="auto",
)

t1 = time.time()
print(f"\n>> [{time.strftime('%H:%M:%S')}] loaded in {t1-t0:.1f}s. merging + saving bf16 shards ...", flush=True)
print(f"   (target: {MERGED_OUT}/, ~57GB across ~13 shards)", flush=True)

# merged_16bit: unsloth iterates over layers, dequantizes 4-bit weight, applies
# LoRA delta, casts to bf16, writes to shard. Per-layer memory pressure only.
model.save_pretrained_merged(MERGED_OUT, tokenizer, save_method="merged_16bit")

t2 = time.time()
print(f"\n>> [{time.strftime('%H:%M:%S')}] DONE_MERGE_v52 — total {t2-t0:.1f}s ({(t2-t0)/60:.1f} min)", flush=True)
print(f"merged at: {MERGED_OUT}", flush=True)
