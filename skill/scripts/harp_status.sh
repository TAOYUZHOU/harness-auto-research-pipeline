#!/usr/bin/env bash
# harp_status.sh — one-screen dashboard of the running HARP loop.
#
# Reads everything from the workspace B (resolved via meta_info), so it
# works whether you're babysitting or just curious.  No writes.
#
# Usage:
#   bash harp_status.sh           # full dashboard
#   bash harp_status.sh --json    # machine-readable JSON to stdout
#   bash harp_status.sh --watch   # refresh every 30s (Ctrl-C to exit)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
ENGINE_DIR="$(dirname "$SKILL_DIR")"

export PATH="$HOME/.local/bin:$PATH"

mode="text"
case "${1:-}" in
  --json)  mode="json" ;;
  --watch) mode="watch" ;;
  -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
  "") ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;;
esac

# ── Resolve B from meta_info via env.sh (single source of truth) ───
WORK_DIR=$(python3 - "$ENGINE_DIR/meta_info/project.yaml" <<'PY'
import sys, pathlib
try:
    import yaml
    p = pathlib.Path(sys.argv[1])
    cfg = yaml.safe_load(p.read_text())
    print(cfg["harness"]["workspace"]["dir"])
except Exception as e:
    print(f"__ERROR__:{e}", file=sys.stderr)
    sys.exit(1)
PY
)
[ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ] || { echo "ERROR: workspace not found: $WORK_DIR" >&2; exit 1; }

STATE="$WORK_DIR/.state"

# ── Helpers ─────────────────────────────────────────────────────────
_read() { [ -f "$1" ] && cat "$1" || echo "$2"; }
_yaml() {
  python3 - "$1" "$2" <<'PY'
import sys, pathlib, yaml
try:
    cfg = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())
    keys = sys.argv[2].split(".")
    cur = cfg
    for k in keys:
        cur = cur[k] if isinstance(cur, dict) and k in cur else cur[int(k)]
    print(cur)
except Exception:
    print("?")
PY
}
_human_dur() {
  local secs=$1
  if [ "$secs" -lt 60 ]; then echo "${secs}s"
  elif [ "$secs" -lt 3600 ]; then printf "%dm%ds\n" $((secs/60)) $((secs%60))
  elif [ "$secs" -lt 86400 ]; then printf "%dh%dm\n" $((secs/3600)) $(((secs%3600)/60))
  else printf "%dd%dh\n" $((secs/86400)) $(((secs%86400)/3600))
  fi
}

# ── Collect ────────────────────────────────────────────────────────
collect() {
  CYCLE=$(_read "$STATE/cycle_count.txt" "0")
  ACTIVE=$(_read "$STATE/iteration_active" "false")
  BEST=$(_read "$STATE/best_metric.txt" "n/a")
  # B/harness.yaml is flat (no top-level 'harness:' wrapper) — that
  # wrapper only lives in meta_info/project.yaml.  init_workspace.sh
  # strips it on render.
  THRESHOLD=$(_yaml "$WORK_DIR/harness.yaml" "targets.0.stop_threshold" 2>/dev/null || echo "?")
  MAX_CYCLE=$(_yaml "$WORK_DIR/harness.yaml" "schedule.max_cycle" 2>/dev/null || echo "?")
  TARGET_NAME=$(_yaml "$WORK_DIR/harness.yaml" "targets.0.name" 2>/dev/null || echo "?")
  TARGET_REPO=$(_yaml "$WORK_DIR/harness.yaml" "targets.0.repo_path" 2>/dev/null || echo "?")
  METRIC=$(_yaml "$WORK_DIR/harness.yaml" "targets.0.primary_metric" 2>/dev/null || echo "?")
  TIME_BUDGET=$(_yaml "$WORK_DIR/harness.yaml" "schedule.train_time_budget_sec" 2>/dev/null || echo "0")

  # Cron
  CRON_LINE=$(crontab -l 2>/dev/null | grep -F "harness-auto-research" | head -1 || true)
  CRON_STATUS="not installed"
  [ -n "$CRON_LINE" ] && CRON_STATUS="installed"

  # In-flight training
  TRAIN_PIDS=$(pgrep -f "python.*train.*\.py" 2>/dev/null || true)
  TRAIN_INFO=""
  if [ -n "$TRAIN_PIDS" ]; then
    while read -r pid; do
      [ -z "$pid" ] && continue
      etime=$(ps -p "$pid" -o etimes= 2>/dev/null | tr -d ' ' || echo 0)
      cmd=$(ps -p "$pid" -o args= 2>/dev/null | head -c 80 || echo "?")
      TRAIN_INFO+="    PID $pid · running $(_human_dur "$etime")"
      if [ "$TIME_BUDGET" -gt 0 ] 2>/dev/null; then
        remaining=$((TIME_BUDGET - etime))
        if [ "$remaining" -gt 0 ]; then
          TRAIN_INFO+=" / $(_human_dur "$TIME_BUDGET") budget (${remaining}s left)"
        else
          TRAIN_INFO+=" / OVER BUDGET — engine will SIGKILL next tick"
        fi
      fi
      TRAIN_INFO+="\n"
    done <<< "$TRAIN_PIDS"
  else
    TRAIN_INFO="    (no training process detected)\n"
  fi

  # In-flight log snapshot
  INFLIGHT_JSON="$STATE/inflight_emit.json"
  INFLIGHT_INFO=""
  if [ -f "$INFLIGHT_JSON" ]; then
    INFLIGHT_INFO=$(python3 - "$INFLIGHT_JSON" <<'PY'
import sys, json
try:
    d = json.load(open(sys.argv[1]))
    if not d:
        print("    (none)"); sys.exit()
    for anchor, info in d.items():
        print(f"    {anchor}: ep {info.get('last_epoch','?')}, best={info.get('best','?')}")
except Exception as e:
    print(f"    (parse error: {e})")
PY
)
  else
    INFLIGHT_INFO="    (no in-flight snapshot yet — engine writes after first tick that sees a live log)"
  fi

  USAGE_FILE="$STATE/usage_summary.txt"
  USAGE_INFO=""
  if [ -f "$USAGE_FILE" ]; then
    USAGE_INFO=$(grep -E "(input_tokens|output_tokens|cache_read|ticks_with)" "$USAGE_FILE" | sed 's/^/    /')
  else
    USAGE_INFO="    (no token-usage data yet — appears after first agent invocation)"
  fi

  # Last tick + log
  LAST_TICK_TS="?"
  if [ -f "$STATE/tick.log" ]; then
    LAST_TICK_TS=$(stat -c %y "$STATE/tick.log" | cut -d. -f1)
  fi

  RECENT_LOG=""
  if [ -f "$WORK_DIR/log.md" ]; then
    RECENT_LOG=$(tail -3 "$WORK_DIR/log.md" | sed 's/^/    /')
  fi

  # Remote
  REMOTE_INFO="(none)"
  if git -C "$WORK_DIR" remote -v 2>/dev/null | grep -q origin; then
    last_push=$(git -C "$WORK_DIR" log origin/$(git -C "$WORK_DIR" branch --show-current 2>/dev/null)..HEAD --oneline 2>/dev/null | wc -l)
    if [ "$last_push" = "0" ]; then
      REMOTE_INFO="$(git -C "$WORK_DIR" remote get-url origin) [in sync]"
    else
      REMOTE_INFO="$(git -C "$WORK_DIR" remote get-url origin) [+${last_push} unpushed]"
    fi
  fi
}

emit_text() {
  printf '\033[1;36m======================== HARP status ========================\033[0m\n'
  printf '  workspace : %s\n' "$WORK_DIR"
  printf '  target    : %s @ %s\n' "$TARGET_NAME" "$TARGET_REPO"
  printf '  active    : %s\n' "$ACTIVE"
  printf '  cycle     : %s / %s\n' "$CYCLE" "$MAX_CYCLE"
  printf '  best %s : %s   (target ≤ %s)\n' "$METRIC" "$BEST" "$THRESHOLD"
  printf '  cron      : %s\n' "$CRON_STATUS"
  printf '  remote    : %s\n' "$REMOTE_INFO"
  printf '  last tick : %s\n' "$LAST_TICK_TS"
  printf '\n\033[1m  in-flight training:\033[0m\n%b' "$TRAIN_INFO"
  printf '\n\033[1m  in-flight metric (live log scan):\033[0m\n%s\n' "$INFLIGHT_INFO"
  printf '\n\033[1m  token usage:\033[0m\n%s\n' "$USAGE_INFO"
  printf '\n\033[1m  recent log.md (3 lines):\033[0m\n%s\n' "$RECENT_LOG"
  printf '\033[1;36m============================================================\033[0m\n'
}

emit_json() {
  python3 - <<PY
import json
print(json.dumps({
  "workspace": "$WORK_DIR",
  "target": {"name": "$TARGET_NAME", "repo": "$TARGET_REPO", "metric": "$METRIC"},
  "iteration": {"active": "$ACTIVE", "cycle": "$CYCLE", "max_cycle": "$MAX_CYCLE",
                "best": "$BEST", "stop_threshold": "$THRESHOLD"},
  "cron": "$CRON_STATUS",
  "remote": "$REMOTE_INFO",
  "last_tick_ts": "$LAST_TICK_TS",
}, indent=2))
PY
}

case "$mode" in
  text)
    collect; emit_text ;;
  json)
    collect; emit_json ;;
  watch)
    while true; do
      clear; collect; emit_text
      printf '\n  refreshing every 30s — Ctrl-C to exit\n'
      sleep 30
    done ;;
esac
