#!/usr/bin/env bash
# HARP quickstart — one command from "fresh checkout" to "ready for cron".
#
# Pipeline:
#   1. Sanity-check meta_info/project.yaml (the SOLE per-project input in
#      repo A: must declare workspace.dir, target paths, metric, threshold,
#      and at least one userprompt rule).
#   2. init_workspace.sh — renders meta_info into B (harness.yaml,
#      userprompt.yaml, .cursorrules) and seeds program.md/check.md/...
#      Idempotent unless --force is passed through.
#   3. Post-init: re-validate B/harness.yaml + B/userprompt.yaml.  This
#      catches rendering bugs before we burn an agent invocation.
#   4. backup_originals.sh (init runs it too; cheap to repeat).
#   5. GitNexus re-index sanity (warns if node>=20 missing).
#   6. agent CLI in --mode preflight: read check.md, do bootstrap.
#   7. On preflight success, set iteration_active=true.
#   8. Configure workspace_remote (B's optional GitHub backup).
#
# Usage:
#   bash scripts/quickstart.sh           # normal
#   bash scripts/quickstart.sh --force   # wipe & re-init workspace first
#   bash scripts/quickstart.sh --skip-init   # workspace already exists & is OK
#
# Anything that fails here aborts the script — quickstart is fail-fast
# precisely because the alternative (silently starting cron with a
# half-configured harness) wastes hours of training time.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
META_FILE="${SERVICE_ROOT}/meta_info/project.yaml"

# Don't source env.sh yet — env.sh fails hard if WORK_DIR/harness.yaml is
# missing, which is exactly the situation on a fresh checkout.  We bootstrap
# WORK_DIR ourselves from meta_info, run init, then source env.sh.
if [[ ! -f "$META_FILE" ]]; then
    echo "[quickstart] FATAL: meta_info missing at $META_FILE" >&2
    echo "             See ${SERVICE_ROOT}/meta_info/README.md" >&2
    exit 2
fi

# Resolve WORK_DIR from env or meta_info (no A/harness.yaml read).
if [[ -z "${WORK_DIR:-}" ]]; then
    WORK_DIR="$(python3 -c "
import yaml
m = yaml.safe_load(open('${META_FILE}')) or {}
print(((m.get('harness') or {}).get('workspace') or {}).get('dir', '') or '')
")"
fi
if [[ -z "$WORK_DIR" || "$WORK_DIR" == *"<"*">"* ]]; then
    echo "[quickstart] FATAL: workspace.dir not set in meta_info." >&2
    exit 2
fi
export WORK_DIR SERVICE_ROOT

FORCE_INIT=false
SKIP_INIT=false
for arg in "$@"; do
    case "$arg" in
        --force)     FORCE_INIT=true ;;
        --skip-init) SKIP_INIT=true ;;
        -h|--help)
            sed -n '2,20p' "$0"; exit 0 ;;
        *)
            echo "[quickstart] unknown arg: $arg" >&2
            exit 1 ;;
    esac
done

echo "=========================================="
echo "  HARP quickstart"
echo "=========================================="
echo "  SERVICE_ROOT:  $SERVICE_ROOT"
echo "  WORK_DIR:      $WORK_DIR"
echo ""

# ── 1) sanity-check meta_info (the SOLE per-project input file) ──────
echo "[1/8] checking meta_info/project.yaml..."

python3 - <<PY
import sys, yaml
from pathlib import Path

META = Path("$META_FILE")
errors = []
m = yaml.safe_load(open(META)) or {}

# harness section
h = m.get("harness") or {}
ws = h.get("workspace") or {}
if not ws.get("dir"):
    errors.append("harness.workspace.dir is empty")

targets = h.get("targets") or []
if not targets:
    errors.append("harness.targets is empty (declare at least one)")
for t in targets:
    name = t.get("name", "?")
    for k in ("repo_path", "result_path", "editable_files",
              "primary_metric", "metric_op", "stop_threshold"):
        if k not in t:
            errors.append(f"target '{name}' missing field: {k}")
    rp = Path(t.get("repo_path", ""))
    if rp and "<" in str(rp):
        errors.append(f"target '{name}' repo_path is still a placeholder: {rp}")
    elif rp and not rp.is_dir():
        errors.append(f"target '{name}' repo_path does not exist: {rp}")

# userprompt section
up = m.get("userprompt") or {}
rules = up.get("rules") or []
if not rules:
    errors.append("userprompt.rules is empty — add at least one rule")

# cursorrules section
cr = m.get("cursorrules") or {}
if not cr.get("header"):
    errors.append("cursorrules.header is empty — needed to render .cursorrules")

if errors:
    print("[quickstart] FAIL — fix meta_info first:")
    for e in errors:
        print(f"   - {e}")
    sys.exit(1)
print("[quickstart] meta_info looks sane")
PY

# ── 2) init workspace (unless skipped) ───────────────────────────────
if $SKIP_INIT && [[ -d "$WORK_DIR" ]]; then
    echo "[2/8] --skip-init: reusing existing workspace at $WORK_DIR"
else
    echo "[2/8] initialising workspace..."
    if $FORCE_INIT; then
        bash "${SCRIPT_DIR}/init_workspace.sh" --force
    elif [[ -d "$WORK_DIR" ]]; then
        echo "[quickstart] workspace already exists; reusing (pass --force to wipe)"
    else
        bash "${SCRIPT_DIR}/init_workspace.sh"
    fi
fi

# Now that B exists, source env.sh (it reads B/harness.yaml).
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/../env.sh"

# ── 3) post-init: validate B was rendered correctly ──────────────────
echo "[3/8] verifying B was rendered correctly..."
python3 - <<PY
import sys, yaml
from pathlib import Path

WD = Path("$WORK_DIR")
errors = []

# Required files in B (ALL of these must exist post-init)
required = ["harness.yaml", "userprompt.yaml", ".cursorrules", "program.md",
            "plan.md", "memory.md", "check.md", "log.md"]
for f in required:
    if not (WD / f).exists():
        errors.append(f"missing in B: {f}")

# B/harness.yaml shape
hy = WD / "harness.yaml"
if hy.exists():
    h = yaml.safe_load(open(hy)) or {}
    if not h.get("targets"):
        errors.append("B/harness.yaml has no targets[]")
    elif not h["targets"][0].get("repo_path"):
        errors.append("B/harness.yaml targets[0].repo_path is empty")

# B/userprompt.yaml shape
up = WD / "userprompt.yaml"
if up.exists():
    u = yaml.safe_load(open(up)) or {}
    if not u.get("rules"):
        errors.append("B/userprompt.yaml has no rules[]")

# Self-heal: copy project-agnostic templates if user is on an old workspace.
for f in ("check.md", "memory.md", ".mcp.json"):
    src = Path("$SERVICE_ROOT") / f
    dst = WD / f
    if not dst.exists() and src.exists():
        dst.write_bytes(src.read_bytes())
        print(f"[quickstart] back-filled missing {dst}")

if errors:
    print("[quickstart] FAIL — B is missing critical files:")
    for e in errors:
        print(f"   - {e}")
    print("[quickstart] try: bash $SCRIPT_DIR/quickstart.sh --force")
    sys.exit(1)
print("[quickstart] B looks correctly rendered")
PY

# ── 4) re-assert backups (init runs it too; cheap to repeat) ─────────
echo "[4/8] backing up editable_files originals..."
bash "${SCRIPT_DIR}/backup_originals.sh" || {
    echo "[quickstart] FAIL: backup_originals.sh failed — see above"
    exit 1
}

# ── 5) GitNexus index (poll_tick.py also does this, but doing it
#       upfront gives quicker feedback if node/glibc is wrong) ───────
echo "[5/8] re-indexing target repos via GitNexus..."
NODE_BIN="$(command -v node || true)"
if [[ -z "$NODE_BIN" ]]; then
    NODE_BIN="$(ls /root/.cursor-server/bin/linux-x64/*/node 2>/dev/null | head -1 || true)"
fi
if [[ -z "$NODE_BIN" ]]; then
    echo "[quickstart] WARN: no node>=20 found — preflight will still run, "
    echo "             but GitNexus MCP queries may fail until you install one."
fi

# ── 6) agent preflight ──────────────────────────────────────────────
echo "[6/8] running agent preflight (mode=preflight)..."
echo "      (agent reads ${WORK_DIR}/check.md and does the bootstrap checklist)"
echo ""

python3 "${SCRIPT_DIR}/poll_tick.py" --mode preflight
preflight_rc=$?

if [[ $preflight_rc -ne 0 ]]; then
    echo ""
    echo "[quickstart] preflight FAILED (rc=$preflight_rc)"
    echo "  - inspect:  ${WORK_DIR}/.state/last_preflight_output.txt"
    echo "  - common fixes:"
    echo "      * add AGENT-EDITABLE-BEGIN/END markers to editable_files"
    echo "      * set targets[].baseline_anchor in harness.yaml if no runs exist yet"
    echo "      * authenticate the agent CLI (run: agent login)"
    exit $preflight_rc
fi

# ── 7) flip iteration_active=true ────────────────────────────────────
echo "[7/8] enabling iteration..."
mkdir -p "${WORK_DIR}/.state"
echo "true" > "${WORK_DIR}/.state/iteration_active"

# ── 8) configure workspace remote (optional) ─────────────────────────
# Reads workspace_remote.{mode,org,visibility,push_on} from harness.yaml
# (WORK_DIR is canonical) and, on mode=auto, creates a GitHub repo via
# `gh` and sets it as origin so future ticks can push automatically.
# mode=manual assumes the user already wired up `git remote add origin`
# and we just verify + do an initial push. mode=none is a no-op.
echo "[8/8] configuring workspace remote..."

REMOTE_PLAN="$(python3 - <<PY
import sys, yaml
from pathlib import Path
h = yaml.safe_load(open(Path("$WORK_DIR") / "harness.yaml")) or {}
wr = h.get("workspace_remote") or {}
mode = (wr.get("mode") or "none").strip().lower()
org = (wr.get("org") or "").strip()
vis = (wr.get("visibility") or "private").strip().lower()
push_on = (wr.get("push_on") or "keep").strip().lower()
if mode not in {"auto", "manual", "none"}:
    print(f"ERROR:invalid mode '{mode}' (expected auto|manual|none)")
    sys.exit(0)
if vis not in {"private", "public"}:
    print(f"ERROR:invalid visibility '{vis}' (expected private|public)")
    sys.exit(0)
if push_on not in {"keep", "every_tick", "never"}:
    print(f"ERROR:invalid push_on '{push_on}' (expected keep|every_tick|never)")
    sys.exit(0)
print(f"OK\t{mode}\t{org}\t{vis}\t{push_on}")
PY
)"

if [[ "$REMOTE_PLAN" == ERROR:* ]]; then
    echo "[quickstart] workspace_remote config error: ${REMOTE_PLAN#ERROR:}"
    exit 1
fi

REMOTE_MODE="$(echo "$REMOTE_PLAN" | cut -f2)"
REMOTE_ORG="$(echo "$REMOTE_PLAN" | cut -f3)"
REMOTE_VIS="$(echo "$REMOTE_PLAN" | cut -f4)"
REMOTE_PUSH_ON="$(echo "$REMOTE_PLAN" | cut -f5)"
REMOTE_BRANCH="$(git -C "$WORK_DIR" symbolic-ref --short HEAD 2>/dev/null || echo master)"
REMOTE_REPO_NAME="$(basename "$WORK_DIR")"

case "$REMOTE_MODE" in
    none)
        echo "[quickstart] workspace_remote.mode=none — staying local-only."
        echo "             flip to 'auto' or 'manual' in harness.yaml when ready to back up."
        ;;
    manual)
        if ! git -C "$WORK_DIR" remote get-url origin >/dev/null 2>&1; then
            echo "[quickstart] workspace_remote.mode=manual but no 'origin' remote configured."
            echo "             configure it yourself, e.g.:"
            echo "                 git -C $WORK_DIR remote add origin git@github.com:<you>/${REMOTE_REPO_NAME}.git"
            echo "             then re-run quickstart, or set push_on=never."
            if [[ "$REMOTE_PUSH_ON" != "never" ]]; then
                exit 1
            fi
        else
            origin_url="$(git -C "$WORK_DIR" remote get-url origin)"
            echo "[quickstart] workspace_remote.mode=manual — origin already set: $origin_url"
            if [[ "$REMOTE_PUSH_ON" != "never" ]]; then
                echo "[quickstart] performing initial push of '$REMOTE_BRANCH' (and tags)..."
                git -C "$WORK_DIR" push -u origin "$REMOTE_BRANCH"
                git -C "$WORK_DIR" push --tags origin || true
            fi
        fi
        ;;
    auto)
        if ! command -v gh >/dev/null 2>&1; then
            echo "[quickstart] workspace_remote.mode=auto requires 'gh' CLI; not on PATH."
            echo "             install from https://cli.github.com/ or set mode=manual/none."
            exit 1
        fi
        if ! gh auth status >/dev/null 2>&1; then
            echo "[quickstart] workspace_remote.mode=auto but 'gh' is not authenticated."
            echo "             run:  gh auth login   (then re-run quickstart)"
            exit 1
        fi
        if git -C "$WORK_DIR" remote get-url origin >/dev/null 2>&1; then
            origin_url="$(git -C "$WORK_DIR" remote get-url origin)"
            echo "[quickstart] origin already set: $origin_url — skipping repo creation."
            if [[ "$REMOTE_PUSH_ON" != "never" ]]; then
                git -C "$WORK_DIR" push -u origin "$REMOTE_BRANCH"
                git -C "$WORK_DIR" push --tags origin || true
            fi
        else
            slug="${REMOTE_REPO_NAME}"
            if [[ -n "$REMOTE_ORG" ]]; then
                slug="${REMOTE_ORG}/${REMOTE_REPO_NAME}"
            fi
            echo "[quickstart] creating GitHub repo '$slug' (visibility=$REMOTE_VIS)..."
            gh repo create "$slug" "--$REMOTE_VIS" \
                --source "$WORK_DIR" \
                --description "HARP workspace for $(basename "$WORK_DIR") — auto-created by quickstart.sh" \
                --remote origin \
                --push
            git -C "$WORK_DIR" push --tags origin || true
            echo "[quickstart] workspace_remote ready: origin=$(git -C "$WORK_DIR" remote get-url origin)"
        fi
        ;;
esac

echo ""
echo "=========================================="
echo "  Quickstart OK"
echo "=========================================="
echo ""
echo "Baseline registered.  Next steps:"
echo ""
echo "  # install the cron entry (default = every 15 min):"
echo "  bash ${SERVICE_ROOT}/scripts/install_cron.sh install"
echo ""
echo "  # or run a single tick manually (good for dry-run debugging):"
echo "  python3 ${SERVICE_ROOT}/scripts/poll_tick.py --mode tick"
echo ""
echo "  # watch the journal grow:"
echo "  tail -f ${WORK_DIR}/log.md"
echo "  less    ${WORK_DIR}/memory.md"
echo ""
echo "Re-run \`quickstart.sh\` any time you change userprompt.yaml or"
echo "harness.yaml — preflight is idempotent."
