#!/bin/bash
# llama-server: qwen3-coder-30b-a3b-q4_K_M — CODER backend (lever #2, 2026-06-05).
# Vulkan, MoE expert offload. Served on :8091 so it A/B's against v5.3 (:8090) without
# touching companion routing. Both are Qwen3-30B-A3B on a 16GB card → CANNOT co-reside;
# evict v5.3 first (bin/llama_v53.sh stop). 18.56GB GGUF → more CPU offload than v5.3.
# --jinja = use the GGUF's embedded qwen3-coder chat template (the hypothesis: ollama's
# template caused the instruction misses / foreign tool schema we saw in the ollama A/B).
# Qwen3-Coder is NON-thinking (no reasoning-budget). Sampling = Qwen-recommended defaults;
# the coder pipeline overrides temp=0.1 per request.
set -e
DIR=/home/user/llama-vulkan/llama-b9413
GGUF=/usr/share/ollama/models/blobs/sha256-1194192cf2a187eb02722edcc3f77b11d21f537048ce04b67ccf8ba78863006a
cd "$DIR"
exec env LD_LIBRARY_PATH="$DIR" ./llama-server \
  -m "$GGUF" \
  -ngl 99 -ncmoe 20 -fa on -c 16384 --parallel 1 \
  --host 127.0.0.1 --port 8091 --jinja \
  --temp 0.7 --top-k 20 --top-p 0.8 --repeat-penalty 1.05
