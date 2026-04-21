#!/usr/bin/env bash
# Wrapper: invoke cursor agent CLI or dry-run.
# Called by poll_tick.py — but can also be used standalone.
# Usage: invoke_agent.sh <prompt_file>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../env.sh"

: "${WORK_DIR:?WORK_DIR not set}"

PROMPT_FILE="${1:?Usage: invoke_agent.sh <prompt_file>}"
PROMPT="$(cat "$PROMPT_FILE")"

if command -v "${AGENT_BIN:-agent}" >/dev/null 2>&1; then
    echo "[agent] calling ${AGENT_BIN} with workspace ${WORK_DIR} ..."
    CMD=("${AGENT_BIN}" -p --force --trust --workspace "${WORK_DIR}")
    [[ -n "${AGENT_MODEL:-}" ]] && CMD+=(--model "$AGENT_MODEL")
    CMD+=("$PROMPT")
    timeout "${AGENT_TIMEOUT_SEC:-300}" "${CMD[@]}" 2>&1
else
    echo "[dry-run] agent binary '${AGENT_BIN:-agent}' not found"
    echo "[dry-run] prompt saved at: ${PROMPT_FILE}"
    echo "[dry-run] --- prompt preview (first 20 lines) ---"
    head -20 "$PROMPT_FILE"
    echo "[dry-run] --- end preview ---"
fi
