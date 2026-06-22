#!/usr/bin/env bash
# roux_status.sh — one-glance system state (read-only). The "check anytime" window.
# Services, hot models, snapshots, trust-ledger readiness, proposal queue.
# Usage: bin/roux_status.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
PY="venv/bin/python"
G=$'\033[92m'; R=$'\033[91m'; Y=$'\033[93m'; B=$'\033[1m'; D=$'\033[2m'; X=$'\033[0m'

dot() { [ "$1" = "200" ] && printf "%s●%s" "$G" "$X" || printf "%s●%s" "$R" "$X"; }

echo "${B}── Roux status ── $(date '+%Y-%m-%d %H:%M:%S') ──${X}"

# --- Services ---
echo "${B}services${X}"
declare -A SVC=( [8000]=gateway [8001]=orchestrator [8002]=coder [8003]=worker [8004]=memory [8010]=supervisor [8011]=rag [8012]=cron )
up=0; tot=0
for p in 8000 8001 8002 8003 8004 8010 8011 8012; do
  code=$(curl -s -m2 "http://127.0.0.1:$p/health" -o /dev/null -w '%{http_code}' 2>/dev/null)
  printf "  %s %-13s %s%s%s\n" "$(dot "$code")" "${SVC[$p]}" "$D" "$p" "$X"
  tot=$((tot+1)); [ "$code" = "200" ] && up=$((up+1))
done
ocode=$(curl -s -m2 http://127.0.0.1:11434/api/tags -o /dev/null -w '%{http_code}' 2>/dev/null)
printf "  %s %-13s %s%s%s\n" "$(dot "$ocode")" "ollama" "$D" "11434" "$X"
echo "  ${D}${up}/${tot} core services up${X}"

# --- Hot models ---
echo "${B}models hot${X}"
curl -s -m3 http://127.0.0.1:11434/api/ps 2>/dev/null | $PY -c "
import json,sys
try: m=json.load(sys.stdin).get('models',[])
except: m=[]
if not m: print('  (none loaded)')
for x in m: print(f\"  {x['name']:<34} {x.get('size_vram',0)/1e9:.1f}GB\")
" 2>/dev/null || echo "  (ollama unreachable)"

# --- Snapshots ---
echo "${B}rollback${X}"
SNAPDIR=/mnt/sata/roux-backups
n=$(ls -1 "$SNAPDIR"/snap_*.tar.gz 2>/dev/null | wc -l)
latest=$(ls -1t "$SNAPDIR"/snap_*.tar.gz 2>/dev/null | head -1 | xargs -r basename)
free=$(df -BG --output=avail "$SNAPDIR" 2>/dev/null | tail -1 | tr -dc '0-9')
printf "  %s snapshots | latest: %s%s%s | %sG free\n" "$n" "$D" "${latest:-none}" "$X" "${free:-?}"

# --- Trust ledger ---
echo "${B}trust ledger${X}"
$PY -c "
from shared.trust_ledger import readiness_report
r=readiness_report()
if not r: print('  (no events yet)')
for t,d in r.items():
    rdy='${G}READY${X}' if d['graduation_ready'] else '${Y}not ready${X}'
    print(f\"  {t}: {d['correct']} clean / {d['false_pass']} false-pass / {d['false_block']} false-block  [{rdy}]\")
    for b in d.get('blockers',[]): print(f'      - {b}')
" 2>/dev/null || echo "  (ledger unavailable)"

# --- Proposal queue ---
echo "${B}proposal queue${X}"
$PY -c "
import json
from collections import Counter
try:
    d=json.load(open('state/proposals_active.json')); p=d if isinstance(d,list) else d.get('proposals',[])
except: p=[]
states=Counter((x.get('state') or 'pending') for x in p)
def elig(x):
    if x.get('state')!='approved': return False
    em=x.get('executor_meta') or {}
    if em.get('dispatched') or em.get('dispatch_state')=='dispatch_failed': return False
    return True
e=sum(1 for x in p if elig(x))
print(f\"  {len(p)} active | eligible-to-dispatch: {e}\")
print('  ' + ' '.join(f'{k}:{v}' for k,v in sorted(states.items())))
" 2>/dev/null || echo "  (queue unavailable)"
echo "${B}──────────────────────────────────────────${X}"
