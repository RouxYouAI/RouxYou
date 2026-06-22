#!/bin/bash
# GPU arbiter for the llama-server v53 backend.
#
# llama-server (Roux v53, Vulkan, --n-cpu-moe 10) owns ~15GB of the 16GB card.
# The coder model (qwen2.5-coder:14b, ~9GB GPU via ollama) can't co-reside.
# Companion is the always-on priority; coder (/plan) is manual + rare (and
# autonomy is currently paused). To run a coder task that needs the GPU:
#     bin/llama_v53.sh stop      # frees the GPU
#     ... run the coder /plan ...
#     bin/llama_v53.sh start     # restore the companion backend
#
# (A future automatic arbiter can wrap this around _handle_code; deferred while
#  autonomy is paused.)
case "$1" in
  stop)
    PID=$(ss -lptnH 'sport = :8090' 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
    if [ -n "$PID" ]; then kill "$PID" && echo "llama-server stopped (pid $PID) — GPU freed for coder"
    else echo "llama-server not running on :8090"; fi
    ;;
  start)
    if ss -lptnH 'sport = :8090' 2>/dev/null | grep -q 8090; then
      echo "llama-server already up on :8090"
    else
      nohup /home/user/RouxYou/bin/serve_v53.sh > /tmp/llama-server-v53.log 2>&1 &
      echo "llama-server starting on :8090 (~8s cold-load)..."
    fi
    ;;
  status)
    code=$(curl -s -o /dev/null -w "%{http_code}" -m 3 http://127.0.0.1:8090/health 2>/dev/null)
    echo "llama-server :8090 → HTTP ${code:-down}"
    nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader 2>/dev/null
    ;;
  *)
    echo "usage: $0 {stop|start|status}"
    ;;
esac
