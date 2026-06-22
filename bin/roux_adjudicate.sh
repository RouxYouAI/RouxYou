#!/usr/bin/env bash
# roux_adjudicate.sh — tier-0 trust-ledger adjudication pass (lever #1, 2026-06-05).
#
# Promotes APPROVE'd changes that stayed ground_truth=unknown (patches — runtime-verify only
# confirms net-new files) to confirmed_good once they have SURVIVED ROUX_ADJUDICATION_DAYS with
# no rollback and no later confirmed_bad ("no news is good news"). Re-verifies touched .py still
# parse where the event recorded paths. Promote-ONLY, idempotent — safe to run daily.
#
# This is TIER-0 (Claude/DJ/cron) on purpose: the autonomous proposal loop must NOT be able to
# promote its own changes toward graduation. A cron is infra, not the loop, so it's allowed.
#
# Cron: once daily. Log: logs/adjudicate.log.
set -euo pipefail
cd /home/user/RouxYou
ts="$(date '+%Y-%m-%d %H:%M:%S')"

# Honor the master autonomy switch (2026-06-16): graduation bookkeeping must NOT advance the
# ledger toward the 25-clean gate while autonomy is PAUSED — closes the gap roux_launch.sh's
# pause didn't cover (this cron is infra, fires independent of the stack). Same switch.
if venv/bin/python -c "import sys; from shared.autonomy_switch import get_state; sys.exit(0 if get_state().get('paused') else 1)" 2>/dev/null; then
  echo "[$ts] adjudication SKIPPED — autonomy PAUSED (ledger frozen; no promotion toward graduation)"
  exit 0
fi

out="$(venv/bin/python -m shared.trust_ledger adjudicate 2>&1)"
promoted="$(printf '%s' "$out" | grep -c '"change_id"' || true)"
echo "[$ts] adjudication pass — promotions: ${promoted}"
printf '%s\n' "$out"
