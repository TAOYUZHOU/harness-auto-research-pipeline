#!/usr/bin/env bash
# Atomic toggle: swap the on-disk content of every editable file with its
# backup copy. Running this script TWICE returns the system to its original
# state, so it is a true A/B switch.
#
#   BEFORE: file = AGENT_VERSION,    backup = ORIGINAL
#   run #1: file = ORIGINAL,         backup = AGENT_VERSION
#   run #2: file = AGENT_VERSION,    backup = ORIGINAL    (same as start)
#
# The mtime/perm of each file is preserved.  The sha256 sidecar is updated
# so it always reflects the file currently sitting in the .backup tree.
#
# Usage:
#   source env.sh && bash scripts/swap_editable.sh           # interactive confirm
#   source env.sh && bash scripts/swap_editable.sh --yes     # no confirm
#   source env.sh && bash scripts/swap_editable.sh --status  # just print state
#
# Requires: backup_originals.sh has been run at least once.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -z "${WORK_DIR:-}" || -z "${SERVICE_ROOT:-}" ]]; then
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/../env.sh"
fi

: "${WORK_DIR:?WORK_DIR not set}"
: "${SERVICE_ROOT:?SERVICE_ROOT not set}"

BACKUP_ROOT="$WORK_DIR/.backup"

MODE=swap
[[ "${1:-}" == "--yes" ]] && MODE=swap_noconfirm
[[ "${1:-}" == "--status" ]] && MODE=status

# Read editable_files list from B/harness.yaml (the canonical runtime config).
mapfile -t ENTRIES < <(python3 - <<PY
import yaml
d = yaml.safe_load(open("${WORK_DIR}/harness.yaml"))
for t in d.get("targets", []):
    name = t["name"]; repo = t["repo_path"]
    for f in t.get("editable_files", []) or []:
        print(f"{name}|{repo}|{f}")
PY
)

if [[ ${#ENTRIES[@]} -eq 0 ]]; then
    echo "[swap] no editable_files declared in harness.yaml — nothing to do"
    exit 0
fi

# Phase 1 — verify all backups exist and report current diff state.
plans=()
echo "=== editable file status ==="
for entry in "${ENTRIES[@]}"; do
    IFS='|' read -r name repo rel <<< "$entry"
    cur="$repo/$rel"
    bak="$BACKUP_ROOT/$name/$rel"
    if [[ ! -f "$cur" ]]; then
        echo "  MISSING current: $cur"; exit 1
    fi
    if [[ ! -f "$bak" ]]; then
        echo "  MISSING backup:  $bak"
        echo "  Run: bash scripts/backup_originals.sh"
        exit 1
    fi
    cur_sha=$(sha256sum "$cur" | awk '{print $1}')
    bak_sha=$(sha256sum "$bak" | awk '{print $1}')
    if [[ "$cur_sha" == "$bak_sha" ]]; then
        state="IDENTICAL"
    else
        state="DIFFERS"
    fi
    printf "  %-9s  %s/%s\n" "$state" "$name" "$rel"
    plans+=("$cur|$bak|$state")
done

if [[ "$MODE" == "status" ]]; then
    exit 0
fi

# Phase 2 — confirm.
if [[ "$MODE" == "swap" ]]; then
    read -r -p "Proceed with swap? [y/N] " ans
    [[ "${ans,,}" == "y" || "${ans,,}" == "yes" ]] || { echo "[swap] aborted"; exit 0; }
fi

# Phase 3 — swap.  Use a per-file tmp inside the same dir for atomicity.
ts=$(date -u +%Y%m%dT%H%M%SZ)
echo ""
echo "[swap] performing swap ($ts) ..."
for plan in "${plans[@]}"; do
    IFS='|' read -r cur bak state <<< "$plan"
    tmp="${cur}.swap.$$"
    mv "$cur" "$tmp"
    mv "$bak" "$cur"
    mv "$tmp" "$bak"
    new_bak_sha=$(sha256sum "$bak" | awk '{print $1}')
    echo "$new_bak_sha" > "$bak.sha256"
    echo "  swapped: ${cur#${WORK_DIR}/}    (backup now sha=${new_bak_sha:0:8})"
done

# Append swap event to log.md so the agent sees it next tick.
LOG_FILE="$WORK_DIR/log.md"
if [[ -f "$LOG_FILE" ]]; then
    echo "TS=$ts;EVENT=editable_swap;COUNT=${#plans[@]}" >> "$LOG_FILE"
fi

echo "[swap] done — run again to swap back"
