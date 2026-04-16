#!/usr/bin/env bash
# Demo environment — overrides env.sh for the self-contained demo.
# Usage: source demo/env_demo.sh && python3 scripts/poll_tick.py

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export SERVICE_ROOT="${SCRIPT_DIR}"
export RESULT_ROOT="${SCRIPT_DIR}/demo/results"
export KERMT_ROOT="${SCRIPT_DIR}/demo"
export LOG_GLOB="nohup_train.log"
export PRIMARY_METRIC="mae"
export PRIMARY_METRIC_OP="lt"
export GLOBAL_STOP_THRESHOLD="0.05"
export TICK_INTERVAL_MIN=1
export TRAIN_TIME_BUDGET_SEC=0
export AGENT_BIN="agent"
export AGENT_MAX_LOG_LINES=50
export AGENT_TIMEOUT_SEC=120
export GIT_EXPERIMENT_MGMT="false"
export MAX_CONSECUTIVE_FAILURES=3
export LOCKFILE="${SERVICE_ROOT}/.state/tick.lock"
export PATH="$HOME/.local/bin:$PATH"
