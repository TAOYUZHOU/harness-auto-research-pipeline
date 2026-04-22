#!/usr/bin/env bash
# harp_polish_daemon.sh — background watcher that runs harp_polish.sh
# whenever log.md / memory.md / plan.md changes.
#
# - Polls every $POLL_SEC seconds (default 60).
# - Skips polish if mtime cache says nothing changed (delegated to
#   harp_polish.sh which checks sha256, so spurious mtime bumps don't
#   waste tokens).
# - Writes its own log to <WORK_DIR>/.state/polish_daemon.log.
# - Single-instance via lockfile.
#
# Usage:
#   nohup bash harp_polish_daemon.sh > /tmp/harp_polish.log 2>&1 &
#   POLL_SEC=30 bash harp_polish_daemon.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
ENGINE_DIR="$(dirname "$SKILL_DIR")"

export PATH="$HOME/.local/bin:$PATH"

POLL_SEC="${POLL_SEC:-60}"

WORK_DIR=$(python3 - "$ENGINE_DIR/meta_info/project.yaml" <<'PY'
import sys, yaml, pathlib
print(yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())["harness"]["workspace"]["dir"])
PY
)
[ -d "$WORK_DIR" ] || { echo "ERROR: workspace not found: $WORK_DIR" >&2; exit 1; }

STATE="$WORK_DIR/.state"
mkdir -p "$STATE"
LOCK="$STATE/polish_daemon.lock"
LOGF="$STATE/polish_daemon.log"

# Single-instance lock (flock-based)
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "another harp_polish_daemon is already running (lock $LOCK held)" >&2
  exit 1
fi
echo $$ > "$LOCK"

trap 'rm -f "$LOCK"; echo "[$(date -uIseconds)] daemon stopped" >> "$LOGF"; exit 0' INT TERM EXIT

echo "[$(date -uIseconds)] daemon started (pid $$ poll=${POLL_SEC}s)" >> "$LOGF"

while true; do
  changed=0
  for f in log memory plan; do
    src="$WORK_DIR/${f}.md"
    cache="$STATE/zh/.${f}.src_sha256"
    [ -f "$src" ] || continue
    sha=$(sha256sum "$src" | awk '{print $1}')
    prev="$( [ -f "$cache" ] && cat "$cache" || echo "" )"
    if [ "$sha" != "$prev" ]; then
      changed=1
      break
    fi
  done

  if [ "$changed" -eq 1 ]; then
    echo "[$(date -uIseconds)] change detected, polishing..." >> "$LOGF"
    if bash "$SCRIPT_DIR/harp_polish.sh" --once >> "$LOGF" 2>&1; then
      echo "[$(date -uIseconds)] polish ok" >> "$LOGF"
    else
      echo "[$(date -uIseconds)] polish FAILED (see above)" >> "$LOGF"
    fi
  fi

  sleep "$POLL_SEC"
done
