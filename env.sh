#!/usr/bin/env bash
# Auto-Research Service — environment configuration
# Source this file before running any script, or let poll_tick.py auto-source it.
# All paths must be absolute; ~ is NOT expanded in cron environments.

# ---------- paths ----------
export SERVICE_ROOT="/root/autodl-tmp/taoyuzhou/auto-research-service"
export RESULT_ROOT="/root/autodl-tmp/taoyuzhou/KERMT/tlc/results"
export KERMT_ROOT="/root/autodl-tmp/taoyuzhou/KERMT"

# ---------- scan ----------
export LOG_GLOB="nohup_train.log"          # filename to match inside each result dir
export CONFIG_GLOB="effective_config.yaml"  # hyperparams source per run

# ---------- metrics ----------
export PRIMARY_METRIC="mae"                # field name extracted from log
export PRIMARY_METRIC_OP="lt"              # lt = lower is better
export GLOBAL_STOP_THRESHOLD="0.04"        # stop iteration when metric < this

# ---------- schedule ----------
export TICK_INTERVAL_MIN=10                # cron interval in minutes

# ---------- training time budget ----------
# If >0, kill training processes that exceed this wall-clock limit (seconds).
# Set to 0 to let training run to natural completion (default for long runs).
export TRAIN_TIME_BUDGET_SEC=0

# ---------- agent ----------
export AGENT_BIN="agent"                   # cursor CLI binary name
export AGENT_MAX_LOG_LINES=50              # how many recent log.md lines to feed agent
export AGENT_TIMEOUT_SEC=300               # kill agent if it runs longer than this

# ---------- git experiment management ----------
export GIT_EXPERIMENT_MGMT="true"          # true = commit before train, reset on failure
export BEST_METRIC_FILE="${SERVICE_ROOT}/.state/best_metric.txt"

# ---------- safety ----------
export MAX_CONSECUTIVE_FAILURES=5          # stop after N consecutive crash/parse_error ticks
export LOCKFILE="${SERVICE_ROOT}/.state/tick.lock"

# ---------- PATH (ensure agent CLI is findable) ----------
export PATH="$HOME/.local/bin:$PATH"
