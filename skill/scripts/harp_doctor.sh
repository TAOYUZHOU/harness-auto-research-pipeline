#!/usr/bin/env bash
# harp_doctor.sh — health check for a HARP installation.
#
# Verifies (with PASS/FAIL/WARN tags):
#   - cursor-agent in PATH and responds to --version
#   - meta_info/project.yaml exists and parses
#   - workspace B exists, is a git repo, has harness.yaml
#   - cron line installed for this engine
#   - tick.log recently written (within 2× cron interval)
#   - target repo D exists and is a git repo
#   - .state/program_constitution.sha256 matches current program.md
#   - workspace_remote (if mode=auto) is reachable
#   - free disk on workspace mount > 1 GB
#   - python deps available (yaml, requests already in poll_tick.py)
#
# Exit code = number of FAILs.  WARNs don't fail.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
ENGINE_DIR="$(dirname "$SKILL_DIR")"

# Match the engine's PATH munging — agent CLI is often in ~/.local/bin
# which cron / fresh shells don't get by default.
export PATH="$HOME/.local/bin:$PATH"

fails=0
warns=0
passes=0
pass() { printf '  \033[1;32m✓\033[0m  %s\n' "$1"; passes=$((passes+1)); }
fail() { printf '  \033[1;31m✗\033[0m  %s\n' "$1"; fails=$((fails+1)); }
warn() { printf '  \033[1;33m!\033[0m  %s\n' "$1"; warns=$((warns+1)); }

echo "==[ HARP doctor ]=================================="
echo "engine: $ENGINE_DIR"
echo

# 1. cursor-agent
if command -v cursor-agent >/dev/null 2>&1; then
  v=$(cursor-agent --version 2>/dev/null | head -1)
  pass "cursor-agent: $v"
else
  fail "cursor-agent not in PATH"
fi

# 2. meta_info/project.yaml
META="$ENGINE_DIR/meta_info/project.yaml"
if [ ! -f "$META" ]; then
  fail "meta_info/project.yaml missing — run harp_init.sh"
  echo
  echo "doctor: $fails fail(s), $warns warn(s)"
  exit "$fails"
fi
if python3 -c "import yaml,sys;yaml.safe_load(open(sys.argv[1]))" "$META" 2>/dev/null; then
  pass "meta_info/project.yaml parses"
else
  fail "meta_info/project.yaml malformed YAML"
fi

# 3. workspace B
WORK_DIR=$(python3 - "$META" <<'PY'
import sys, yaml, pathlib
try:
    print(yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())["harness"]["workspace"]["dir"])
except Exception:
    print("")
PY
)
if [ -z "$WORK_DIR" ]; then
  fail "harness.workspace.dir missing in meta_info"
elif [ ! -d "$WORK_DIR" ]; then
  fail "workspace dir does not exist: $WORK_DIR (run init_workspace.sh)"
else
  pass "workspace exists: $WORK_DIR"
  [ -f "$WORK_DIR/harness.yaml" ] && pass "B/harness.yaml present" || fail "B/harness.yaml missing"
  if git -C "$WORK_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    pass "workspace is a git repo"
  else
    fail "workspace is NOT a git repo"
  fi
fi

# 4. cron line
if crontab -l 2>/dev/null | grep -qF "harness-auto-research"; then
  pass "cron line installed"
  CRON_MIN=$(crontab -l 2>/dev/null | grep "harness-auto-research" | awk '{print $1}')
  echo "       schedule: $CRON_MIN"
else
  warn "no cron line for HARP — install with: bash $ENGINE_DIR/scripts/install_cron.sh install"
fi

# 5. recent tick.log
TLOG="$WORK_DIR/.state/tick.log"
if [ -f "$TLOG" ]; then
  age=$(( $(date +%s) - $(stat -c %Y "$TLOG") ))
  if [ "$age" -lt 1800 ]; then
    pass "tick.log fresh (last write ${age}s ago)"
  else
    warn "tick.log stale (${age}s = $((age/60))m old) — cron may not be firing"
  fi
else
  warn "tick.log not yet written — wait for first cron tick"
fi

# 6. target repo D
TARGET=$(python3 - "$META" <<'PY'
import sys, yaml, pathlib
try:
    print(yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())["harness"]["targets"][0]["repo_path"])
except Exception:
    print("")
PY
)
if [ -z "$TARGET" ]; then
  fail "harness.targets[0].repo_path missing"
elif [ ! -d "$TARGET" ]; then
  fail "target repo not found: $TARGET"
else
  pass "target repo exists: $TARGET"
  if git -C "$TARGET" rev-parse --git-dir >/dev/null 2>&1; then
    pass "target is a git repo"
  else
    fail "target is NOT a git repo (HARP needs git for keep/discard)"
  fi
fi

# 7. constitution hash
HASH_FILE="$WORK_DIR/.state/program_constitution.sha256"
PROG="$WORK_DIR/program.md"
if [ -f "$HASH_FILE" ] && [ -f "$PROG" ]; then
  expected=$(cat "$HASH_FILE")
  # Must match scripts/poll_tick.py::_constitution_text exactly — the
  # USER-INJECTED block (markers included) is REMOVED, not replaced.
  current=$(python3 - "$PROG" <<'PY'
import sys, re, hashlib, pathlib
text = pathlib.Path(sys.argv[1]).read_text()
text = re.sub(
    r"<!-- USER-INJECTED-BEGIN -->.*?<!-- USER-INJECTED-END -->",
    "", text, flags=re.DOTALL)
print(hashlib.sha256(text.encode("utf-8")).hexdigest())
PY
)
  if [ "$expected" = "$current" ]; then
    pass "program.md constitution hash matches (${expected:0:16}…)"
  else
    fail "program.md constitution DRIFTED — engine will trigger atomic rollback next tick"
  fi
else
  warn "no constitution hash recorded yet — run quickstart.sh preflight"
fi

# 8. workspace_remote reachability
RMODE=$(python3 - "$META" <<'PY'
import sys, yaml, pathlib
try:
    print(yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())["harness"]["workspace_remote"]["mode"])
except Exception:
    print("none")
PY
)
if [ "$RMODE" = "auto" ] || [ "$RMODE" = "manual" ]; then
  if git -C "$WORK_DIR" remote get-url origin >/dev/null 2>&1; then
    if git -C "$WORK_DIR" ls-remote --quiet origin HEAD >/dev/null 2>&1; then
      pass "workspace remote reachable: $(git -C "$WORK_DIR" remote get-url origin)"
    else
      warn "workspace remote unreachable (network or auth)"
    fi
  else
    warn "workspace_remote.mode=$RMODE but no 'origin' configured"
  fi
fi

# 9. disk
if [ -d "$WORK_DIR" ]; then
  free_kb=$(df -k "$WORK_DIR" | tail -1 | awk '{print $4}')
  free_mb=$((free_kb/1024))
  if [ "$free_mb" -gt 1024 ]; then
    pass "disk free: ${free_mb} MB on $(df "$WORK_DIR" | tail -1 | awk '{print $1}')"
  else
    warn "disk almost full: only ${free_mb} MB free on workspace mount"
  fi
fi

# 10. python deps
if python3 -c "import yaml" 2>/dev/null; then
  pass "python yaml module"
else
  fail "python yaml module missing — pip install pyyaml"
fi

echo
if [ "$fails" -eq 0 ]; then
  printf '\033[1;32mhealthy\033[0m: %d pass, %d warn\n' "$passes" "$warns"
else
  printf '\033[1;31m%d FAIL(s)\033[0m, %d pass, %d warn — fix above before running HARP\n' "$fails" "$passes" "$warns"
fi
exit "$fails"
