#!/bin/bash
# llama-server: Roux v53 backend — Vulkan, MoE expert offload (--n-cpu-moe).
# 2026-05-30: "the precise way" — beats ollama's native-engine layer-split.
#   gen ~58 t/s (vs ollama 38 = ~1.5x), grounded prompt-eval ~900 t/s (prefix-cached), ncmoe=10 fits 16GB.
# -c 8192: grounded prompts run ~5600 tokens (4096 was too small → rejected → empty companion replies).
# --parallel 1: single-user companion; 1 slot @ 8192 uses less KV than the default 4 slots.
# OPTION B (2026-06-20): -ncmoe 16 (was 10) frees VRAM by offloading more MoE experts to CPU, creating
#   headroom so --reasoning-budget 1024 (full reasoning, no preamble truncation) fits even on memory-heavy
#   prompts WITHOUT the vk OutOfDeviceMemory crash that 1024 caused at ncmoe 10 (~136 MiB free). Tradeoff:
#   slower generation (more CPU experts). Bridge until the DGX Spark (more VRAM). If still OOM → raise ncmoe more.
set -e
DIR=/home/user/llama-vulkan/llama-b9413
# v53 GGUF (NVMe ollama blob — fast cold-load ~8s)
V53=/usr/share/ollama/models/blobs/sha256-e6c1948a7066c66e81c713357e57ca1c6484c801356e7ed82b2cc947e06d754c
cd "$DIR"
# Free the GPU: ensure ollama isn't holding roux-v53 (harmless if absent/down).
ollama stop roux-v53 2>/dev/null || true
exec env LD_LIBRARY_PATH="$DIR" ./llama-server \
  -m "$V53" \
  -ngl 99 -ncmoe 16 -fa on -c 8192 --parallel 1 \
  --host 127.0.0.1 --port 8090 --jinja \
  --reasoning-budget 1024 \
  --temp 0.6 --top-k 20 --top-p 0.95 --repeat-penalty 1.1
