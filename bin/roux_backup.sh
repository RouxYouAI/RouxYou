#!/usr/bin/env bash
# roux_backup.sh — unified reversibility backup for the Tier-3 era.
#
#   (1) CODE   → private GitHub  (RouxYouAI/RouxYou-private)  — off-machine, versioned.
#   (2) MEMORY → SATA snapshots  (/mnt/sata/roux-memory-backups) — the 37k LanceDB
#                archive, which lives on the NVMe and otherwise has NO backup.
#   (3) FILE-MEM → SATA snapshots (/mnt/sata/roux-filemem-backups) — the FILE-based memory:
#                ~/.claude/.../memory/*.md (MEMORY.md + topic files) + ~/Desktop/For Claude/
#                (voice.md, signals.md, calibration). Text + irreplaceable; lived on the NVMe
#                with NO backup until the gap was caught 2026-06-01.
#
# Why two targets: code is text → git gives granular `reset --hard` rollback off-machine.
# Memory is large churning binary + the most personal data → stays LOCAL, snapshotted to a
# SEPARATE physical drive (a copy on the same NVMe is not a backup). Versioned snapshots
# beat RAID here: RAID mirrors a bad write instantly; snapshots let you revert past it.
#
# TIER-0: this script is in the immutable set (shared/tier0.py) — the autonomous
# loop may never edit it. Run it from system cron (independent of the autonomy stack) and,
# once hardened, from a user/owner the loop can't impersonate.
#
# Usage: bin/roux_backup.sh [--code-only|--memory-only]   (default: both)
set -uo pipefail

ROUXYOU="/home/user/RouxYou"
MEM_SRC="/home/user/claude-memory-mcp/memories"
MEM_DEST="/mnt/sata/roux-memory-backups"
RETAIN=10                       # keep this many memory snapshots
BRANCH="master"
# (3) FILE-MEM: the text/file-based memory neither channel above covered (caught 2026-06-01).
FILEMEM_SRCS=( "/home/user/.claude/projects/-home-dj/memory" "/home/user/Desktop/For Claude" )
FILEMEM_NAMES=( "claude-auto-memory" "for-claude" )
FILEMEM_DEST="/mnt/sata/roux-filemem-backups"
ts() { date +%Y%m%dT%H%M%S; }

DO_CODE=1; DO_MEM=1
case "${1:-}" in
  --code-only) DO_MEM=0 ;;
  --memory-only) DO_CODE=0 ;;
esac

# ---------------------------------------------------------------- (1) CODE → GitHub
backup_code() {
  cd "$ROUXYOU" || { echo "code: cannot cd $ROUXYOU"; return 1; }
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "code: not a git repo"; return 1; }
  if [ -n "$(git status --porcelain)" ]; then
    git add -A
    # HARD secret gate — never push a real key literal, even on an auto-commit.
    local hits
    hits=$(git diff --cached --name-only -z 2>/dev/null \
      | xargs -0 grep -lIE "sk-ant-[A-Za-z0-9_-]{20}|sk-[A-Za-z0-9]{40}|gho_[A-Za-z0-9]{30}|AKIA[A-Z0-9]{16}|xoxb-[A-Za-z0-9-]{20}" 2>/dev/null)
    local envct
    envct=$(git diff --cached --name-only | grep -cE '(^|/)\.env$')
    if [ -n "$hits" ] || [ "$envct" != "0" ]; then
      echo "code: ⛔ ABORT — secret literal/.env staged ($hits .env=$envct). Unstaging, not committing."
      git reset -q
      return 1
    fi
    git commit -q -m "auto-backup $(ts)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
    echo "code: committed $(git log -1 --format='%h')"
  else
    echo "code: no changes to commit"
  fi
  if git push -q origin "$BRANCH" 2>/dev/null; then
    echo "code: pushed → RouxYouAI/RouxYou-private"
  else
    echo "code: push skipped (offline / auth?) — commit is still local"
  fi
}

# ---------------------------------------------------------------- (2) MEMORY → SATA
backup_memory() {
  [ -d "$MEM_SRC" ] || { echo "memory: source $MEM_SRC missing"; return 1; }
  mkdir -p "$MEM_DEST" 2>/dev/null || { echo "memory: cannot write $MEM_DEST (SATA mounted?)"; return 1; }
  local prev snap
  prev=$(ls -1dt "$MEM_DEST"/mem_* 2>/dev/null | head -1)
  snap="$MEM_DEST/mem_$(ts)"
  # --link-dest hardlinks files unchanged since the last snapshot → space-efficient history.
  # --no-owner --no-group: do NOT preserve the source's (dj) ownership — the snapshot is
  # owned by whoever RUNS this (root, via root cron), so Roux-as-dj can't even corrupt the
  # CONTENTS of a snapshot, not just add/delete whole ones. (2026-05-31 hardening fix.)
  local ropts=(-a --no-owner --no-group --delete)
  if [ -n "$prev" ]; then
    rsync "${ropts[@]}" --link-dest="$prev" "$MEM_SRC/" "$snap/"
  else
    rsync "${ropts[@]}" "$MEM_SRC/" "$snap/"
  fi
  # belt-and-suspenders: if running as root, force root ownership on the whole snapshot.
  [ "$(id -u)" = "0" ] && chown -R root:root "$snap" 2>/dev/null
  echo "memory: snapshot → $snap ($(du -sh "$snap" 2>/dev/null | cut -f1))"
  # retain the last RETAIN snapshots
  ls -1dt "$MEM_DEST"/mem_* 2>/dev/null | tail -n +$((RETAIN+1)) | xargs -r rm -rf
  echo "memory: $(ls -1d "$MEM_DEST"/mem_* 2>/dev/null | wc -l) snapshots retained"
}

# ---------------------------------------------------------------- (3) FILE-MEM → SATA
backup_file_memory() {
  mkdir -p "$FILEMEM_DEST" 2>/dev/null || { echo "filemem: cannot write $FILEMEM_DEST (SATA mounted?)"; return 1; }
  local prev snap i src name
  prev=$(ls -1dt "$FILEMEM_DEST"/fmem_* 2>/dev/null | head -1)
  snap="$FILEMEM_DEST/fmem_$(ts)"; mkdir -p "$snap"
  local ropts=(-a --no-owner --no-group --delete)   # same root-ownable, hardlink-dedup pattern as memory
  for i in "${!FILEMEM_SRCS[@]}"; do
    src="${FILEMEM_SRCS[$i]}"; name="${FILEMEM_NAMES[$i]}"
    [ -d "$src" ] || { echo "filemem: source missing: $src"; continue; }
    if [ -n "$prev" ] && [ -d "$prev/$name" ]; then
      rsync "${ropts[@]}" --link-dest="$prev/$name" "$src/" "$snap/$name/"
    else
      rsync "${ropts[@]}" "$src/" "$snap/$name/"
    fi
  done
  [ "$(id -u)" = "0" ] && chown -R root:root "$snap" 2>/dev/null
  echo "filemem: snapshot → $snap ($(du -sh "$snap" 2>/dev/null | cut -f1))"
  ls -1dt "$FILEMEM_DEST"/fmem_* 2>/dev/null | tail -n +$((RETAIN+1)) | xargs -r rm -rf
  echo "filemem: $(ls -1d "$FILEMEM_DEST"/fmem_* 2>/dev/null | wc -l) snapshots retained"
}

echo "=== roux_backup $(date '+%F %T') ==="
[ "$DO_CODE" = 1 ] && backup_code
[ "$DO_MEM" = 1 ] && backup_memory
[ "$DO_MEM" = 1 ] && backup_file_memory   # file-memory rides the --memory side of the toggle
echo "=== done ==="
