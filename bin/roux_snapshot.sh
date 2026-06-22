#!/usr/bin/env bash
# roux_snapshot.sh — Phase A rollback: rolling full code+config+state snapshots
# of RouxYou onto the SATA drive. Reversibility is what lets the human leave the
# loop (2026-05-25). NOT git — plain timestamped tarballs you can ls/cp/extract.
#
# Usage:
#   roux_snapshot.sh snapshot [label]     Create a snapshot (optional label slug)
#   roux_snapshot.sh list                 List snapshots, newest first
#   roux_snapshot.sh restore <name>       Stage a snapshot for restore (safe; no live write)
#   roux_snapshot.sh restore <name> --confirm   Apply restore over the live tree
#                                         (auto-takes a safety snapshot first)
#
# Excludes venv / models / caches (they don't change per-edit and are huge).
# A code+config+state snapshot is ~21M, so RETAIN_MAX=300 is ~6G of 650G free.
set -euo pipefail

SRC="/home/user/RouxYou"
DEST="/mnt/sata/roux-backups"
PREFIX="snap"
# Retention: keep AS MANY rollback points as possible (DJ 2026-05-25). Snapshots
# are ~3.3M each on a 650G-free drive, so we don't cap by count — we only prune
# the OLDEST once free space drops below MIN_FREE_GB. A high count backstop guards
# against a pathological runaway only.
MIN_FREE_GB=25
RETAIN_BACKSTOP=20000

# Tier-0 / apex files: the components with no automatic recovery (a bad edit here
# has no supervisor-of-the-supervisor). `pre-apex` snapshots before these change.
TIER0_FILES=(
  "orchestrator/watchtower.py"   # the supervisor — cannot restart itself
)

EXCLUDES=(
  --exclude="venv" --exclude="*.venv" --exclude=".venv"
  --exclude="__pycache__" --exclude="*.pyc"
  --exclude="models" --exclude="*.gguf" --exclude="*.bin" --exclude="*.safetensors"
  --exclude=".git"
  --exclude="*.log"            # logs are noise for a code-rollback
)

ts() { date +%Y%m%dT%H%M%S; }
slug() { echo "${1:-snapshot}" | tr -c 'A-Za-z0-9._-' '-' | sed 's/-\+/-/g; s/^-//; s/-$//'; }

cmd_snapshot() {
  mkdir -p "$DEST"
  local label; label="$(slug "${1:-snapshot}")"
  local name="${PREFIX}_$(ts)_${label}.tar.gz"
  local path="$DEST/$name"
  echo "Snapshotting $SRC -> $path"
  tar -czf "$path" "${EXCLUDES[@]}" -C "$(dirname "$SRC")" "$(basename "$SRC")"
  local size; size="$(du -h "$path" | cut -f1)"
  echo "  done ($size)"
  prune
  echo "  $(ls -1 "$DEST"/${PREFIX}_*.tar.gz 2>/dev/null | wc -l) snapshots retained ($(free_gb)G free, floor ${MIN_FREE_GB}G)"
  echo "$path"
}

free_gb() { df -BG --output=avail "$DEST" 2>/dev/null | tail -1 | tr -dc '0-9'; }

prune() {
  # Capacity-based: delete the OLDEST snapshots only while free space is under the
  # floor (or the backstop count is exceeded). Newest are always kept.
  while :; do
    local files; mapfile -t files < <(ls -1t "$DEST"/${PREFIX}_*.tar.gz 2>/dev/null)
    (( ${#files[@]} <= 1 )) && break
    local free; free="$(free_gb)"
    if (( free < MIN_FREE_GB )) || (( ${#files[@]} > RETAIN_BACKSTOP )); then
      local oldest="${files[-1]}"
      echo "  pruning oldest (free ${free}G < ${MIN_FREE_GB}G): $(basename "$oldest")"
      rm -f "$oldest"
    else
      break
    fi
  done
}

cmd_list() {
  echo "Snapshots in $DEST (newest first):"
  ls -1t "$DEST"/${PREFIX}_*.tar.gz 2>/dev/null | while read -r f; do
    printf "  %-8s  %s\n" "$(du -h "$f" | cut -f1)" "$(basename "$f")"
  done
  local n; n="$(ls -1 "$DEST"/${PREFIX}_*.tar.gz 2>/dev/null | wc -l)"
  echo "  ($n total)"
}

cmd_restore() {
  local name="${1:?restore: need a snapshot name (see 'list')}"
  local confirm="${2:-}"
  local path="$DEST/$name"
  [[ -f "$path" ]] || { echo "No such snapshot: $path" >&2; exit 1; }

  local stage="/mnt/sata/roux-restore-staging"
  rm -rf "$stage"; mkdir -p "$stage"
  echo "Staging $name -> $stage (no live write yet)"
  tar -xzf "$path" -C "$stage"

  if [[ "$confirm" != "--confirm" ]]; then
    echo
    echo "STAGED ONLY. Review $stage/$(basename "$SRC") then re-run with --confirm to apply:"
    echo "  $0 restore $name --confirm"
    echo "Apply will: (1) take a SAFETY snapshot of the current tree, then"
    echo "            (2) rsync --delete the staged tree over $SRC (venv/models preserved)."
    exit 0
  fi

  echo "Taking safety snapshot of CURRENT state before overwriting..."
  cmd_snapshot "pre-restore-of-${name%.tar.gz}" >/dev/null
  echo "Applying restore (rsync --delete; venv/models/caches preserved)..."
  rsync -a --delete \
    --exclude="venv" --exclude=".venv" --exclude="__pycache__" \
    --exclude="models" --exclude="*.gguf" --exclude="*.bin" --exclude="*.safetensors" \
    "$stage/$(basename "$SRC")/" "$SRC/"
  echo "Restore applied. Restart services (launch.py or per-service /restart) to load reverted code."
}

cmd_pre_apex() {
  # Take a rollback point BEFORE editing a tier-0/apex file. Call this first when
  # touching anything in TIER0_FILES (e.g. the supervisor). The autonomous loop
  # must never reach these at all — this hook is for the Claude-in-loop + DJ path.
  local target="${1:?pre-apex: need the file about to be edited}"
  local is_tier0="no"
  for t in "${TIER0_FILES[@]}"; do [[ "$target" == "$t" || "$target" == *"$t" ]] && is_tier0="yes"; done
  if [[ "$is_tier0" == "no" ]]; then
    echo "NOTE: '$target' is not in the tier-0 manifest — taking a snapshot anyway."
  else
    echo "TIER-0 edit guard: '$target' — snapshotting before any change."
  fi
  cmd_snapshot "pre-apex-$(basename "$target")"
}

case "${1:-}" in
  snapshot)  shift; cmd_snapshot "${1:-}";;
  list)      cmd_list;;
  restore)   shift; cmd_restore "${1:-}" "${2:-}";;
  pre-apex)  shift; cmd_pre_apex "${1:-}";;
  tier0)     printf '%s\n' "${TIER0_FILES[@]}";;
  *) echo "usage: $0 {snapshot [label] | list | restore <name> [--confirm] | pre-apex <file> | tier0}" >&2; exit 2;;
esac
