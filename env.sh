#!/usr/bin/env bash
# env.sh — environment for HARP shell scripts (cron, install_cron, helpers).
#
# Two-stage config resolution:
#
#   Stage 1 (bootstrap):   read A/meta_info/project.yaml ONLY for
#                          harness.workspace.dir → exports WORK_DIR.
#                          This is the sole "where does B live" pointer
#                          inside repo A; everything else is in B.
#
#   Stage 2 (runtime):     read WORK_DIR/harness.yaml for every other
#                          field (target paths, metric, schedule, etc.).
#                          B is the single source of truth at runtime.
#
# Why two stages: A/meta_info is the only file in A that can plausibly
# contain a project-specific value (the workspace path). Once we have
# that, we hop to B and never look back at A's harness.yaml — which no
# longer exists post-meta_info migration.
#
# Honour pre-existing $WORK_DIR (cron may set it explicitly): the env
# var wins over what meta_info says, useful for multi-workspace setups
# pointing one A at several Bs.
set -euo pipefail

_HARP_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SERVICE_ROOT="${_HARP_ENV_DIR}"
unset _HARP_ENV_DIR

# ── Stage 1: bootstrap WORK_DIR from meta_info ─────────────────────────────
META_FILE="${SERVICE_ROOT}/meta_info/project.yaml"
if [[ ! -f "$META_FILE" ]]; then
    echo "[FATAL] meta_info not found at $META_FILE" >&2
    echo "        Create it from meta_info/README.md instructions." >&2
    return 1 2>/dev/null || exit 1
fi

# Resolve WORK_DIR: env var > meta_info; never overwrite an explicit setting.
if [[ -z "${WORK_DIR:-}" ]]; then
    WORK_DIR="$(python3 -c "
import sys, yaml
m = yaml.safe_load(open('${META_FILE}')) or {}
ws = (m.get('harness') or {}).get('workspace') or {}
print(ws.get('dir', '') or '')
")"
fi
if [[ -z "$WORK_DIR" || "$WORK_DIR" == *"<"*">"* ]]; then
    echo "[FATAL] WORK_DIR unresolved." >&2
    echo "        Either export WORK_DIR=<path>, or fill in" >&2
    echo "        ${META_FILE}::harness.workspace.dir." >&2
    return 1 2>/dev/null || exit 1
fi
export WORK_DIR

# ── Stage 2: read runtime config from B/harness.yaml ───────────────────────
HARNESS_FILE="${WORK_DIR}/harness.yaml"
if [[ ! -f "$HARNESS_FILE" ]]; then
    echo "[FATAL] B/harness.yaml not found at $HARNESS_FILE" >&2
    echo "        Has the workspace been initialised? Run:" >&2
    echo "          bash ${SERVICE_ROOT}/scripts/init_workspace.sh" >&2
    return 1 2>/dev/null || exit 1
fi

# Minimal YAML reader — extracts a single key path from B/harness.yaml.
_yaml_val() {
    local key="$1"
    python3 -c "
import yaml
d = yaml.safe_load(open('${HARNESS_FILE}'))
keys = '${key}'.split('.')
try:
    cur = d
    for k in keys:
        if isinstance(cur, list):
            cur = cur[int(k)]
        else:
            cur = cur[k]
    if isinstance(cur, list):
        print(' '.join(str(x) for x in cur))
    elif isinstance(cur, bool):
        print('true' if cur else 'false')
    else:
        print(cur if cur is not None else '')
except (KeyError, TypeError, IndexError, ValueError):
    print('')
" 2>/dev/null
}

# ---------- paths (from targets[0]) ----------
export RESULT_ROOT="$(_yaml_val 'targets.0.result_path')"
export KERMT_ROOT="$(_yaml_val 'targets.0.repo_path')"

# ---------- scan ----------
export LOG_GLOB="$(_yaml_val 'targets.0.log_glob')"
export CONFIG_GLOB="$(_yaml_val 'targets.0.config_glob')"

# ---------- metrics ----------
export PRIMARY_METRIC="$(_yaml_val 'targets.0.primary_metric')"
export PRIMARY_METRIC_OP="$(_yaml_val 'targets.0.metric_op')"
export GLOBAL_STOP_THRESHOLD="$(_yaml_val 'targets.0.stop_threshold')"

# ---------- schedule ----------
export TICK_INTERVAL_MIN="$(_yaml_val 'schedule.tick_interval_min')"
export TRAIN_TIME_BUDGET_SEC="$(_yaml_val 'schedule.train_time_budget_sec')"
export MAX_CONSECUTIVE_FAILURES="$(_yaml_val 'schedule.max_consecutive_failures')"
export MAX_CYCLE="$(_yaml_val 'schedule.max_cycle')"
export STOP_PROTOCOL="$(_yaml_val 'schedule.stop_protocol')"

# ---------- agent ----------
export AGENT_BIN="$(_yaml_val 'agent.bin')"
export AGENT_MODEL="$(_yaml_val 'agent.model')"
export AGENT_MAX_LOG_LINES="$(_yaml_val 'agent.max_log_lines')"
export AGENT_TIMEOUT_SEC="$(_yaml_val 'agent.timeout_sec')"

# ---------- git experiment management ----------
export GIT_EXPERIMENT_MGMT="$(_yaml_val 'workspace.git_experiment_mgmt')"
export BEST_METRIC_FILE="${WORK_DIR}/.state/best_metric.txt"

# ---------- safety ----------
export LOCKFILE="${WORK_DIR}/.state/tick.lock"

# ---------- PATH ----------
export PATH="$HOME/.local/bin:$PATH"
