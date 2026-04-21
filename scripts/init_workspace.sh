#!/usr/bin/env bash
# init_workspace.sh — bootstrap a HARP workspace (repo B) from repo A.
#
# Inputs:
#   A/meta_info/project.yaml    — per-project personalization (sole input).
#                                 Defines workspace.dir, target paths, rules,
#                                 and the .cursorrules header.  See
#                                 meta_info/README.md for the schema.
#
# Outputs in WORK_DIR (B):
#   harness.yaml      ← rendered from project.yaml::harness
#   userprompt.yaml   ← rendered from project.yaml::userprompt
#   .cursorrules      ← rendered from project.yaml::cursorrules with
#                       ${KERMT_ROOT}/${SERVICE_ROOT}/${WORK_DIR} substituted
#   program.md        ← copied verbatim from A (constitution; B owns it after
#                       init, drift enforced via .state/program_constitution.sha256)
#   plan.md           ← copied verbatim from A (project-agnostic seed)
#   memory.md         ← copied verbatim from A (empty research journal)
#   check.md          ← copied verbatim from A (preflight protocol)
#   log.md            ← copied verbatim from A (header only)
#   .mcp.json         ← copied verbatim from A (gitnexus MCP config)
#   .gitignore        ← auto-generated
#   .state/           ← created empty (gitignored)
#   .git/             ← initialised, single commit "workspace initialized from template"
#
# Decoupling guarantee: after this script finishes, B is fully self-contained
# at the config level.  Nothing in B references A; nothing in A is needed at
# runtime except scripts/ (engine code) and meta_info/project.yaml::harness.workspace.dir
# (read by env.sh as the bootstrap pointer to find B).
#
# Usage:
#   bash scripts/init_workspace.sh [--force]
#
#     --force   wipe WORK_DIR if it already exists
#
# All paths derive from meta_info/project.yaml; nothing is hardcoded.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
META_FILE="${SERVICE_ROOT}/meta_info/project.yaml"

if [[ ! -f "$META_FILE" ]]; then
    echo "[FATAL] meta_info not found: $META_FILE" >&2
    echo "        Copy meta_info/project.yaml.example, fill it in, and retry." >&2
    exit 2
fi

FORCE=false
[[ "${1:-}" == "--force" ]] && FORCE=true

# ── Read WORK_DIR + a few key fields from meta_info, in one Python call ──────
# (Bash YAML parsing is brittle; one shot keeps it consistent.)
read -r WORK_DIR TARGET_NAME REPO_PATH < <(python3 - "$META_FILE" <<'PY'
import sys, yaml
m = yaml.safe_load(open(sys.argv[1])) or {}
ws = (m.get("harness") or {}).get("workspace") or {}
tg = ((m.get("harness") or {}).get("targets") or [{}])[0]
print(ws.get("dir", ""), tg.get("name", ""), tg.get("repo_path", ""))
PY
)

if [[ -z "$WORK_DIR" || "$WORK_DIR" == *"<"*">"* ]]; then
    echo "[FATAL] meta_info project.yaml: harness.workspace.dir is empty or" >&2
    echo "        still a placeholder ('$WORK_DIR'). Fill it in first." >&2
    exit 2
fi
if [[ -z "$REPO_PATH" || ! -d "$REPO_PATH" ]]; then
    echo "[FATAL] target.repo_path does not exist on disk: '$REPO_PATH'" >&2
    echo "        Either the path is wrong or the target repo is not cloned yet." >&2
    exit 2
fi

if [[ -d "$WORK_DIR" ]]; then
    if $FORCE; then
        echo "[init] --force: removing existing workspace at $WORK_DIR"
        rm -rf "$WORK_DIR"
    else
        echo "[init] WORK_DIR already exists: $WORK_DIR"
        echo "[init] Use --force to recreate, or remove it manually."
        exit 1
    fi
fi

echo "[init] creating workspace: $WORK_DIR"
echo "[init]   target:  $TARGET_NAME @ $REPO_PATH"
echo "[init]   source:  $META_FILE"
mkdir -p "$WORK_DIR/.state"

# ── Render harness.yaml + userprompt.yaml + .cursorrules in one Python pass ──
# Why one pass: keep the rendering logic centralised; bash heredocs for YAML
# emission are fragile, especially with nested quoted strings in user rules.
SERVICE_ROOT="$SERVICE_ROOT" WORK_DIR="$WORK_DIR" python3 - "$META_FILE" <<'PY'
import os, sys, yaml
from pathlib import Path

meta_path = Path(sys.argv[1])
m = yaml.safe_load(open(meta_path)) or {}

SR = Path(os.environ["SERVICE_ROOT"])
B = Path(os.environ["WORK_DIR"])

harness = m.get("harness") or {}
userprompt = m.get("userprompt") or {}
cursorrules = m.get("cursorrules") or {}

# ----- render B/harness.yaml ----------------------------------------------
banner = (
    "# ============================================================\n"
    f"# harness.yaml — runtime config for HARP workspace {B.name}\n"
    "# ============================================================\n"
    "# AUTO-GENERATED at init by scripts/init_workspace.sh from\n"
    f"# {meta_path.relative_to(SR)} ::harness.\n"
    "#\n"
    "# After init, this file is OWNED by repo B (this workspace).\n"
    "# Edit it directly to change runtime behaviour. The original\n"
    "# meta_info/project.yaml is kept as the bootstrap record only;\n"
    "# editing it does NOT propagate to a live workspace.\n"
    "#\n"
    "# Read by: env.sh, poll_tick.py, backup_originals.sh, swap_editable.sh.\n"
    "# ============================================================\n\n"
)
(B / "harness.yaml").write_text(
    banner + yaml.safe_dump(harness, sort_keys=False, allow_unicode=True,
                            default_flow_style=False, width=88)
)
print(f"[init] rendered: {B}/harness.yaml")

# ----- render B/userprompt.yaml -------------------------------------------
up_banner = (
    "# ============================================================\n"
    "# userprompt.yaml — natural-language user instructions\n"
    "# ============================================================\n"
    "# This is the ONLY place the user writes harness rules.\n"
    "# Edit any time — the next tick will:\n"
    "#   1. Detect the file changed (sha256 differs from .state/userprompt.sha256).\n"
    "#   2. Prepend 'PROGRAM SYNC REQUIRED' to the agent prompt.\n"
    "#   3. The agent translates each `rules:` entry into a properly numbered,\n"
    "#      imperative HARD CONSTRAINT inside the\n"
    "#      <!-- USER-INJECTED-BEGIN --> / <!-- USER-INJECTED-END --> markers\n"
    "#      of program.md.\n"
    "#   4. The agent emits `PROGRAM_SYNC_DONE=1` and the harness updates the\n"
    "#      synced-hash marker, so subsequent ticks don't re-translate.\n"
    "#\n"
    "# The user NEVER needs to learn program.md's strict format.\n"
    "# The agent NEVER edits this file (it is read-only to the agent).\n"
    "#\n"
    "# AUTO-GENERATED at init from meta_info/project.yaml::userprompt.\n"
    "# Editing this file is the canonical way to evolve the rules; the\n"
    "# meta_info copy is only consulted for re-init of new workspaces.\n"
    "# ============================================================\n\n"
)
(B / "userprompt.yaml").write_text(
    up_banner + yaml.safe_dump(userprompt, sort_keys=False, allow_unicode=True,
                               default_flow_style=False, width=88)
)
print(f"[init] rendered: {B}/userprompt.yaml")

# ----- render B/.cursorrules with ${TOKEN} substitution -------------------
header = cursorrules.get("header", "")
target = (harness.get("targets") or [{}])[0]
subs = {
    "${SERVICE_ROOT}": str(SR),
    "${WORK_DIR}":     str(B),
    "${KERMT_ROOT}":   target.get("repo_path", ""),
}
rendered = header
for k, v in subs.items():
    rendered = rendered.replace(k, v)
(B / ".cursorrules").write_text(rendered)
print(f"[init] rendered: {B}/.cursorrules")

# Stash whether to run the dynamic scanner so the bash side can branch.
flag = B / ".state" / "_init_dynamic_scan"
flag.write_text("1" if cursorrules.get("dynamic_scan") else "0")
PY

# ── Copy project-agnostic templates verbatim ─────────────────────────────────
# These files have NO project-specific content; they are identical across
# all HARP workspaces and copied byte-for-byte.
for f in program.md plan.md memory.md check.md log.md .mcp.json; do
    if [[ -f "$SERVICE_ROOT/$f" ]]; then
        cp "$SERVICE_ROOT/$f" "$WORK_DIR/$f"
        echo "[init] copied:   $WORK_DIR/$f"
    else
        echo "[init] WARN: template missing in A: $f (skipped)"
    fi
done

# ── Workspace .gitignore ─────────────────────────────────────────────────────
cat > "$WORK_DIR/.gitignore" << 'GITIGNORE'
# ==========================================================================
# HARP workspace (repo B) .gitignore
# ==========================================================================
# What we DO commit: every user/agent-facing file in the workspace root
# (plan.md, memory.md, harness.yaml, program.md, userprompt.yaml, log.md,
# check.md, .cursorrules, .mcp.json) so the research history is reproducible.
#
# What we DON'T commit: harness runtime state, large editable-file backups,
# atomic-swap tmp files, and any per-target results that happen to land here.
# ==========================================================================

# Harness runtime state (tick counter, last-applied-config, constitution
# hash, sync diffs, preflight markers, ...). Recreated each tick.
.state/

# Editable-file snapshots for swap_editable.sh. Can balloon to MBs of source
# code; not useful in B's git history (the originals already live in their
# real repos with their own git).
.backup/

# Atomic-swap temporaries written by swap_editable.sh (e.g. foo.py.swap.42).
*.swap.*

# Python bytecode that some tools may drop here.
__pycache__/
*.pyc

# Results subdir — only present if the user pointed result_path inside B for
# convenience. Real data still lives in the target repo's results/ tree.
results/
GITIGNORE
echo "[init] wrote:    $WORK_DIR/.gitignore"

# ── Iteration starts disabled (user explicitly enables via quickstart) ──────
echo "false" > "$WORK_DIR/.state/iteration_active"

# ── Back up editable_files originals ─────────────────────────────────────────
# Mandatory — program.md forbids edits to a file without its backup.
# backup_originals.sh now reads B/harness.yaml (rendered above), so it works
# without env.sh being sourced first.
echo "[init] backing up originals of editable files..."
WORK_DIR="$WORK_DIR" SERVICE_ROOT="$SERVICE_ROOT" \
    bash "${SCRIPT_DIR}/backup_originals.sh" || {
    echo "[init] WARN: backup_originals.sh failed — agent will refuse to edit"
}

# ── Create agent_addition_dir under each target repo ─────────────────────────
# Reads B/harness.yaml (project-specific) — NOT A's, which would be a leak.
echo "[init] creating agent_addition_dir for each target..."
WORK_DIR="$WORK_DIR" python3 - <<'PY'
import os, yaml
from pathlib import Path

WD = Path(os.environ["WORK_DIR"])
H = yaml.safe_load(open(WD / "harness.yaml"))
seed = """# add_by_HARP — agent file-creation sandbox

This directory is the ONLY place the HARP auto-research agent is allowed to
create new files inside this repo (besides the YAML config dir for new
training-run configs).

## Suggested layout

    add_by_HARP/
      data/        new data-split or augmentation scripts
      eval/        new evaluation / metric scripts
      train/       alt training entry points
      utils/       shared helpers
      README.md    this file (do not delete)

## Rules (enforced by program.md)

- Every new file must be `git add`-ed and committed in the SAME commit as
  the experiment that depends on it.
- If the experiment is reverted, the file must be reverted too.
- No duplication of functionality already present elsewhere in the repo.
- snake_case filenames.
- One concern per file; use subdirectories above to group.
"""
for t in H.get("targets", []):
    repo = Path(t["repo_path"])
    if not repo.is_dir():
        print(f"  SKIP: target repo missing on disk: {repo}")
        continue
    add_dir_rel = t.get("agent_addition_dir", "add_by_HARP")
    add_dir = repo / add_dir_rel
    if not add_dir.exists():
        add_dir.mkdir(parents=True, exist_ok=True)
        print(f"  created: {add_dir}")
    readme = add_dir / "README.md"
    if not readme.exists():
        readme.write_text(seed)
        print(f"  seeded:  {readme}")
    else:
        print(f"  exists:  {readme}")
PY

# ── Optional: dynamic context scan to enrich .cursorrules ───────────────────
# generate_context.py walks the target repo and appends "available scripts /
# configs / top results" tables.  Skipped if cursorrules.dynamic_scan = false
# in meta_info, or if the script is not present.
if [[ -f "$WORK_DIR/.state/_init_dynamic_scan" ]] \
        && [[ "$(cat "$WORK_DIR/.state/_init_dynamic_scan")" == "1" ]] \
        && [[ -f "${SCRIPT_DIR}/generate_context.py" ]]; then
    echo "[init] running generate_context.py to enrich .cursorrules..."
    SERVICE_ROOT="$SERVICE_ROOT" WORK_DIR="$WORK_DIR" \
        python3 "${SCRIPT_DIR}/generate_context.py" \
        || echo "[init] WARN: generate_context.py failed (non-fatal)"
fi
rm -f "$WORK_DIR/.state/_init_dynamic_scan"

# ── git init + first commit ──────────────────────────────────────────────────
# Single baseline commit: every file we just placed in B becomes the
# starting point.  Subsequent ticks will commit on top of this.
if command -v git >/dev/null 2>&1; then
    cd "$WORK_DIR"
    git init -q
    git add -A
    git -c user.email=harp@local -c user.name=HARP \
        commit -q -m "workspace initialized from template (meta_info)"
    echo "[init] git: baseline commit recorded"
fi

cat <<EOF

=== Workspace ready ===
  WORK_DIR:      $WORK_DIR
  SERVICE_ROOT:  $SERVICE_ROOT  (engine, read-only)
  meta_info:     $META_FILE     (init record, read-only after first init)

Next steps (preferred = quickstart):
  bash $SERVICE_ROOT/scripts/quickstart.sh
    └─ runs preflight: agent reads check.md, picks baseline, registers
       metric, seeds memory.md, syncs userprompt -> program.md.

Then enable scheduling:
  bash $SERVICE_ROOT/scripts/install_cron.sh install

Manual fallback:
  1. Review $WORK_DIR/plan.md (seed plans)
  2. Edit $WORK_DIR/userprompt.yaml to refine rules (this is the canonical
     copy now; meta_info is only re-read at re-init time)
  3. Edit $WORK_DIR/harness.yaml to tune metric/threshold/baseline_anchor
  4. Index target repos: cd $SERVICE_ROOT && npx gitnexus@1.3.11 analyze $REPO_PATH
  5. Authenticate agent: agent login
  6. Start: bash $SERVICE_ROOT/scripts/install_cron.sh install

After ticks start, watch the journal grow:
  tail -f $WORK_DIR/log.md       # one-line metric stream
  less    $WORK_DIR/memory.md    # narrative per closed experiment
EOF
