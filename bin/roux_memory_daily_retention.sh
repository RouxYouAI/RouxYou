#!/usr/bin/env bash
# Daily 30-day retention TAIL for the irreplaceable memory stores.
# ---------------------------------------------------------------------------
# The 30-min `roux_backup.sh --memory-only` cron keeps only RETAIN=10 snapshots
# (~5 hours). That's fine for catching a fast mistake, but the memory archive is
# IRREPLACEABLE (the shared co-parent LanceDB + the file-memory pickup docs) — a
# bad prune / corruption you don't notice within 5h would roll off the tail.
# This promotes ONE snapshot per day to a 30-day tail, so a month of daily
# restore points always exists. 2026-06-18 (DJ's call: "keep a daily for 30 days").
#
# HARDLINK-COPY (cp -al): near-zero disk (shares inodes with the 30-min snapshots),
# AND the data stays alive even after the 30-min source rotates out (inode refcount).
#
# Install (needs root — the backup dirs are root-owned):
#   sudo crontab -e   →   0 4 * * * /home/user/RouxYou/bin/roux_memory_daily_retention.sh >> /var/log/roux_backup.log 2>&1
set -euo pipefail

RETAIN_DAYS=30
ts=$(date +%Y%m%dT%H%M%S)

# (source 30-min snapshot dir, daily-tail dir, glob prefix)
promote() {
  local src_dir="$1" daily_dir="$2" prefix="$3"
  local newest
  newest=$(ls -1dt "$src_dir"/${prefix}_* 2>/dev/null | head -1) || true
  if [ -z "${newest:-}" ]; then
    echo "daily-retention: no source ${prefix}_ snapshot in $src_dir — skipping"
    return 0
  fi
  mkdir -p "$daily_dir"
  local dest="$daily_dir/daily_${ts}"
  cp -al "$newest" "$dest"
  echo "daily-retention: $newest -> $dest"
  # prune to RETAIN_DAYS most-recent daily dirs
  ls -1dt "$daily_dir"/daily_* 2>/dev/null | tail -n +$((RETAIN_DAYS + 1)) | xargs -r rm -rf
  echo "daily-retention: $(ls -1d "$daily_dir"/daily_* 2>/dev/null | wc -l) daily snapshots retained in $daily_dir"
}

# Shared archive (LanceDB co-parent memory)
promote "/mnt/sata/roux-memory-backups"  "/mnt/sata/roux-memory-daily"   "mem"
# File-memory (the pickup / build / calibration docs)
promote "/mnt/sata/roux-filemem-backups" "/mnt/sata/roux-filemem-daily"  "fmem"

echo "daily-retention: done ($ts)"
