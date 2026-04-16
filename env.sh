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

# ---------- agent ----------
export AGENT_BIN="agent"                   # cursor CLI binary name
export AGENT_MAX_LOG_LINES=50              # how many recent log.md lines to feed agent
export AGENT_TIMEOUT_SEC=300               # kill agent if it runs longer than this

# ---------- safety ----------
export MAX_CONSECUTIVE_FAILURES=5          # stop after N consecutive crash/parse_error ticks
export LOCKFILE="${SERVICE_ROOT}/.state/tick.lock"
