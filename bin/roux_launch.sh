#!/usr/bin/env bash
# roux_launch.sh — one clean command to bring the Roux stack up after a reboot.
#
# Why: launch.py must run from the RouxYou root with the venv python, and every reboot
# otherwise re-enables ALL autonomy with no durable off-switch. This wrapper fixes the
# cwd, uses the venv, logs to a known file, leaves the launcher running (killing it
# cascades SIGTERM to every child), and routes the autonomy decision through the master
# switch (shared/autonomy_switch.py) so it's durable and dashboard-compatible.
#
# Autonomy is ON by default (DJ does not want it disabled at launch). Pass --paused to
# boot with the autonomy pipe gracefully paused (infra — health_check, memory_decay —
# still runs). The pause state persists in state/autonomy.json and can be flipped live
# via POST :8012/watchtower/autonomy without a restart.
#
# Usage:
#   bin/roux_launch.sh            # bring stack up, autonomy ON
#   bin/roux_launch.sh --paused   # bring stack up, autonomy pipe PAUSED (durable)
#   bin/roux_launch.sh --status   # just print what's up / autonomy state, don't launch
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/venv/bin/python"
LOG_DIR="$ROOT/logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/launch.log"
PAUSED=0; STATUS_ONLY=0
for a in "$@"; do
  case "$a" in
    --paused) PAUSED=1 ;;
    --status) STATUS_ONLY=1 ;;
    *) echo "unknown arg: $a (use --paused or --status)"; exit 2 ;;
  esac
done

cd "$ROOT"

# --- per-job autonomy defaults (durable across reboots; flip here or via runtime toggle).
# web_researcher is OFF by default (2026-05-31): its direct-propose output is low-signal
# clutter. Plan is to REPURPOSE it as a seed source feeding reflection's exogenous signals
# (shared/exogenous_seed.py) rather than a direct proposer. Set =1 to re-enable direct mode.
export ROUX_WEB_RESEARCHER="${ROUX_WEB_RESEARCHER:-0}"
# Tier 2 (2026-05-31): roux_self_propose is the offline autonomous proposer. ENABLED now
# that it has the reflection playbook — decoupled (enqueue→drafter, no freeze), the
# exogenous seed (proposes outward), and the topic-diversity gate (no dups). It rides the
# same v5.3 drafter as reflection. Set =0 to pause Tier 2.
export ROUX_AUTONOMOUS_PROPOSE="${ROUX_AUTONOMOUS_PROPOSE:-1}"
# SKIP-enforcement force-flip is OFF (2026-05-31): it force-flipped SKIP→PROPOSE on
# self-referential topics and MANUFACTURED the attractor. The exogenous seed + topic-
# diversity gate supersede it cleanly. Set =1 to restore the old force-flip behavior.
export ROUX_SKIP_ENFORCEMENT="${ROUX_SKIP_ENFORCEMENT:-0}"

ports_up() { ss -tlnp 2>/dev/null | grep -cE ':(8001|8005|8012|8090)\b'; }

print_status() {
  echo "== Roux stack status =="
  local up; up="$(ports_up)"
  echo "  stack ports listening: ${up}/4 (8001 orch, 8005 sched, 8012 watchtower, 8090 llama)"
  echo -n "  autonomy switch: "
  "$PYTHON" -c "from shared.autonomy_switch import get_state; s=get_state(); print(('PAUSED' if s['paused'] else 'ON')+f\"  (by={s.get('by')}, src={s.get('source')})\")" 2>/dev/null \
    || echo "(could not read — stack may be down)"
}

if [ "$STATUS_ONLY" = "1" ]; then
  print_status; exit 0
fi

# Set the durable autonomy state BEFORE launch so the cron loop reads the right value
# from its first tick. Use the switch module (one source of truth), not an env hack.
if [ "$PAUSED" = "1" ]; then
  "$PYTHON" -c "from shared.autonomy_switch import set_paused; set_paused(True, by='roux_launch.sh', reason='--paused flag at boot')"
  echo "autonomy pipe: PAUSED at boot (durable; resume via POST :8012/watchtower/autonomy {paused:false})"
else
  "$PYTHON" -c "from shared.autonomy_switch import set_paused; set_paused(False, by='roux_launch.sh', reason='default autonomy-on launch')"
  echo "autonomy pipe: ON (default)"
fi

if [ "$(ports_up)" -ge 4 ]; then
  echo "stack already appears up ($(ports_up)/4 ports). Not relaunching. Use --status to inspect."
  exit 0
fi

echo "launching stack (logs -> $LOG) ..."
nohup "$PYTHON" launch.py >"$LOG" 2>&1 &
echo "launch.py started (pid $!). Leave it running — killing it SIGTERMs all children."
echo "Tail with: tail -f $LOG"
