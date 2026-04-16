#!/usr/bin/env bash
# Install or remove the auto-research cron job.
# Usage:
#   install_cron.sh install   — add cron entry + set iteration_active=true
#   install_cron.sh remove    — remove cron entry + set iteration_active=false
#   install_cron.sh status    — show current crontab
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../env.sh"

TAG="# auto-research-service"
TICK_CMD="cd ${SERVICE_ROOT} && source env.sh && /usr/bin/python3 scripts/poll_tick.py >> .state/tick.log 2>&1 ${TAG}"

ACTION="${1:-status}"

ensure_cron_running() {
    if ! pgrep -x cron >/dev/null 2>&1; then
        echo "[cron] cron daemon not running, starting..."
        /usr/sbin/cron 2>/dev/null || service cron start 2>/dev/null || true
        sleep 1
        if pgrep -x cron >/dev/null 2>&1; then
            echo "[cron] started successfully"
        else
            echo "[cron] WARNING: could not start cron daemon"
        fi
    fi
}

case "$ACTION" in
    install)
        ensure_cron_running
        mkdir -p "${SERVICE_ROOT}/.state"

        EXISTING="$(crontab -l 2>/dev/null || true)"
        CLEAN="$(echo "$EXISTING" | grep -v "$TAG" || true)"
        ENTRY="*/${TICK_INTERVAL_MIN:-10} * * * * ${TICK_CMD}"

        echo "$CLEAN" | { cat; echo "$ENTRY"; } | crontab -

        echo "true" > "${SERVICE_ROOT}/.state/iteration_active"
        echo "[cron] installed: every ${TICK_INTERVAL_MIN:-10} min"
        echo "[cron] iteration_active = true"
        crontab -l
        ;;

    remove)
        EXISTING="$(crontab -l 2>/dev/null || true)"
        CLEAN="$(echo "$EXISTING" | grep -v "$TAG" || true)"
        echo "$CLEAN" | crontab -

        echo "false" > "${SERVICE_ROOT}/.state/iteration_active"
        echo "[cron] removed auto-research entry"
        echo "[cron] iteration_active = false"
        ;;

    status)
        echo "=== crontab ==="
        crontab -l 2>/dev/null || echo "(empty)"
        echo ""
        echo "=== iteration_active ==="
        cat "${SERVICE_ROOT}/.state/iteration_active" 2>/dev/null || echo "(not set)"
        echo ""
        echo "=== cron daemon ==="
        pgrep -x cron >/dev/null 2>&1 && echo "running" || echo "NOT running"
        ;;

    *)
        echo "Usage: $0 {install|remove|status}"
        exit 1
        ;;
esac
