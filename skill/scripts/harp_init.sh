#!/usr/bin/env bash
# harp_init.sh — interactive bootstrap for a new HARP project.
#
# Asks 4 questions, renders <ENGINE>/meta_info/project.yaml from
# skill/templates/project.yaml, then runs init_workspace.sh +
# quickstart.sh.  Idempotent-ish: refuses to overwrite an existing
# project.yaml unless --force is passed.
#
# Usage:
#   bash harp_init.sh                     # interactive
#   bash harp_init.sh --force             # overwrite existing project.yaml
#   bash harp_init.sh --no-cron           # skip install_cron.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
ENGINE_DIR="$(dirname "$SKILL_DIR")"
TEMPLATE="$SKILL_DIR/templates/project.yaml"
TARGET="$ENGINE_DIR/meta_info/project.yaml"

force=0
do_cron=1
for arg in "$@"; do
  case "$arg" in
    --force)   force=1 ;;
    --no-cron) do_cron=0 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

echo "==[ HARP init ]=================================================="
echo "engine:   $ENGINE_DIR"
echo "template: $TEMPLATE"
echo "target:   $TARGET"
echo

if [ -f "$TARGET" ] && [ "$force" -ne 1 ]; then
  echo "ERROR: $TARGET already exists. Pass --force to overwrite." >&2
  exit 1
fi

# ── Question 1: target repo path ────────────────────────────────────
read -r -p "1) Target repo absolute path (Repo D, the codebase to optimise): " TARGET_REPO
TARGET_REPO=$(realpath -m "$TARGET_REPO")
if [ ! -d "$TARGET_REPO" ]; then
  echo "WARN: $TARGET_REPO does not exist yet — assuming you'll clone it later." >&2
fi
PROJECT_NAME=$(basename "$TARGET_REPO")
read -r -p "   project short id [$PROJECT_NAME]: " PNAME_IN
PROJECT_NAME="${PNAME_IN:-$PROJECT_NAME}"

# ── Question 2: workspace path ──────────────────────────────────────
DEFAULT_WS="$(dirname "$TARGET_REPO")/harp-${PROJECT_NAME}"
read -r -p "2) Workspace path (Repo B) [$DEFAULT_WS]: " WS_IN
WORKSPACE_DIR="$(realpath -m "${WS_IN:-$DEFAULT_WS}")"

# ── Question 3: training entry-point file ───────────────────────────
read -r -p "3) Training script (relative to target repo, with AGENT-EDITABLE blocks): " TRAIN_SCRIPT
if [ -n "$TRAIN_SCRIPT" ] && [ ! -f "$TARGET_REPO/$TRAIN_SCRIPT" ]; then
  echo "WARN: $TARGET_REPO/$TRAIN_SCRIPT not found — continuing anyway." >&2
fi

# ── Question 4: metric + baseline + threshold ───────────────────────
read -r -p "4a) Primary metric name [best_val_mae]: " METRIC
METRIC="${METRIC:-best_val_mae}"
read -r -p "4b) Direction lt/gt (lt = lower-is-better) [lt]: " METRIC_OP
METRIC_OP="${METRIC_OP:-lt}"
read -r -p "4c) Starting baseline value (numeric, current best): " BASELINE
read -r -p "4d) Stop threshold (iteration stops when best beats this): " THRESHOLD
read -r -p "4e) Baseline run name [baseline]: " BASELINE_NAME
BASELINE_NAME="${BASELINE_NAME:-baseline}"

echo
echo "Summary:"
printf "  project_name : %s\n" "$PROJECT_NAME"
printf "  target_repo  : %s\n" "$TARGET_REPO"
printf "  workspace    : %s\n" "$WORKSPACE_DIR"
printf "  train_script : %s\n" "$TRAIN_SCRIPT"
printf "  metric       : %s (%s)\n" "$METRIC" "$METRIC_OP"
printf "  baseline     : %s = %s\n" "$BASELINE_NAME" "$BASELINE"
printf "  threshold    : %s\n" "$THRESHOLD"
echo
read -r -p "Render meta_info/project.yaml from these values? [y/N] " CONFIRM
[[ "$CONFIRM" =~ ^[yY] ]] || { echo "aborted."; exit 0; }

# ── Render template via sed ────────────────────────────────────────
mkdir -p "$(dirname "$TARGET")"
cp "$TEMPLATE" "$TARGET"

# Use a delimiter unlikely to appear in paths.
sed_inplace() {
  local key="$1" val="$2"
  python3 - "$TARGET" "$key" "$val" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
key, val = sys.argv[2], sys.argv[3]
p.write_text(p.read_text().replace(key, val))
PY
}

sed_inplace "__TODO_ABSOLUTE_PATH_TO_NEW_WORKSPACE__" "$WORKSPACE_DIR"
sed_inplace "__TODO_PROJECT_NAME__"                    "$PROJECT_NAME"
sed_inplace "__TODO_ABSOLUTE_PATH_TO_REPO_D__"         "$TARGET_REPO"
sed_inplace "__TODO_RELATIVE_PATH_TO_TRAIN_SCRIPT__"   "$TRAIN_SCRIPT"
sed_inplace "__TODO_METRIC_NAME__"                     "$METRIC"
sed_inplace "__TODO_lt_or_gt__"                        "$METRIC_OP"
sed_inplace "__TODO_STOP_VALUE__"                      "$THRESHOLD"
sed_inplace "__TODO_BASELINE_NAME__"                   "$BASELINE_NAME"
sed_inplace "__TODO_BASELINE_VALUE__"                  "$BASELINE"

# Sanity check: any remaining __TODO_ tokens?
remaining=$(grep -c "__TODO_" "$TARGET" || true)
if [ "$remaining" -gt 0 ]; then
  echo "WARN: $remaining __TODO_ placeholder(s) still in $TARGET — fix before init_workspace." >&2
  grep -n "__TODO_" "$TARGET" >&2 || true
fi

echo
echo "✓ wrote $TARGET"
echo
echo "==[ rendering workspace ]========================================="
bash "$ENGINE_DIR/scripts/init_workspace.sh"

echo
echo "==[ preflight ]==================================================="
if bash "$ENGINE_DIR/scripts/quickstart.sh"; then
  echo "✓ preflight ok"
else
  echo "WARN: preflight had issues (continuing). Check the output above." >&2
fi

if [ "$do_cron" -eq 1 ]; then
  echo
  echo "==[ installing cron ]============================================="
  bash "$ENGINE_DIR/scripts/install_cron.sh" install
fi

echo
echo "==[ DONE ]========================================================"
echo "Watch live progress:    bash $SKILL_DIR/scripts/harp_status.sh"
echo "Tail tick log:          tail -f $WORKSPACE_DIR/.state/tick.log"
echo "Polish to Chinese:      bash $SKILL_DIR/scripts/harp_polish.sh --once"
