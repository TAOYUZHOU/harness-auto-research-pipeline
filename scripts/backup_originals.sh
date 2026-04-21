#!/usr/bin/env bash
# Back up the ORIGINAL versions of every editable file declared in
# harness.yaml under targets[].editable_files, BEFORE the agent makes any
# changes. The agent is forbidden by program.md to touch a file until its
# backup exists at WORK_DIR/.backup/<target_name>/<rel_path>.
#
# ── Backup scope (allowlist, not blacklist) ──────────────────────
# This script backs up ONLY files whose path is explicitly enumerated
# in harness.yaml → targets[].editable_files. There is no glob/dir
# expansion and therefore no need for a `.backupignore` file.
#
# Recommended convention for editable_files (NOT enforced here, just policy):
#   - text source only (.py, .yaml, .json, .toml, .md, .sh)
#   - small files (<1 MiB each)
#   - never list:
#       * binary artifacts (.pt, .pkl, .npz, .h5, .onnx, .safetensors)
#       * datasets (.csv > 1 MiB, .parquet, .arrow, .feather)
#       * media (.png, .jpg, .svg, .pdf)
#       * archives (.zip, .tar.gz, .7z)
#       * checkpoints / model weights of any kind
#       * directories (this script will skip them with a MISSING warning)
#
# Reason: .backup/ is committed-into-disk for swap operations; ballooning
# it with non-source assets defeats the point of a fast A/B toggle and
# clogs the workspace.  GitNexus also chokes on those (see KERMT/.gitnexusignore).
#
# Layout:
#   WORK_DIR/.backup/
#     <target_name>/
#       <rel_path>            ← byte-identical copy of original
#       <rel_path>.sha256     ← sha256 of original (for swap-script integrity)
#       <rel_path>.meta.json  ← {ts, source_abs_path, source_repo_head}
#
# Usage:
#   source env.sh && bash scripts/backup_originals.sh           # skip if exists
#   source env.sh && bash scripts/backup_originals.sh --force   # overwrite
#
# Idempotent. Called automatically by init_workspace.sh. Safe to re-run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Allow callers (e.g. init_workspace.sh during fresh init) to pre-set
# WORK_DIR + SERVICE_ROOT, in which case we skip env.sh.  Otherwise source it.
if [[ -z "${WORK_DIR:-}" || -z "${SERVICE_ROOT:-}" ]]; then
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/../env.sh"
fi
: "${WORK_DIR:?WORK_DIR not set}"
: "${SERVICE_ROOT:?SERVICE_ROOT not set}"

FORCE=false
[[ "${1:-}" == "--force" ]] && FORCE=true

BACKUP_ROOT="$WORK_DIR/.backup"
mkdir -p "$BACKUP_ROOT"

# Pull (target_name, repo_path, editable_file) triples from B/harness.yaml.
# Reading from WORK_DIR (B), not SERVICE_ROOT (A) — A no longer carries a
# project-specific harness.yaml after the meta_info refactor.
mapfile -t ENTRIES < <(python3 - <<PY
import yaml
d = yaml.safe_load(open("${WORK_DIR}/harness.yaml"))
for t in d.get("targets", []):
    name = t["name"]; repo = t["repo_path"]
    for f in t.get("editable_files", []) or []:
        print(f"{name}|{repo}|{f}")
PY
)

if [[ ${#ENTRIES[@]} -eq 0 ]]; then
    echo "[backup] no editable_files declared in harness.yaml — nothing to do"
    exit 0
fi

# ── Soft guards (warn-only) for the recommended scope ─────────────
# Failing these does NOT abort backup — the explicit allowlist always wins —
# but they print a loud warning so stray entries in editable_files don't
# silently bloat .backup/.
SIZE_WARN_BYTES=$((1024 * 1024))                                 # 1 MiB
EXT_BLOCKLIST_RE='\.(pt|pkl|npz|h5|onnx|safetensors|parquet|arrow|feather|zip|gz|tar|7z|png|jpg|jpeg|svg|pdf|so|dylib|dll|bin|ckpt)$'

made=0; skipped=0; missing=0; warned=0
for entry in "${ENTRIES[@]}"; do
    IFS='|' read -r name repo rel <<< "$entry"
    src="$repo/$rel"
    dst="$BACKUP_ROOT/$name/$rel"
    sha_file="$dst.sha256"
    meta_file="$dst.meta.json"

    if [[ ! -f "$src" ]]; then
        echo "[backup] MISSING source, skip: $src"
        missing=$((missing + 1))
        continue
    fi

    if [[ -f "$dst" ]] && ! $FORCE; then
        echo "[backup] EXISTS:   $name/$rel  (use --force to overwrite)"
        skipped=$((skipped + 1))
        continue
    fi

    # Soft guards — warn but proceed. The explicit allowlist always wins.
    src_size=$(stat -c '%s' "$src" 2>/dev/null || echo 0)
    if [[ "$src_size" -gt "$SIZE_WARN_BYTES" ]]; then
        echo "[backup] WARN:     $name/$rel  is $((src_size / 1024)) KiB (>1 MiB)"
        echo "                   .backup/ is meant for source code, not data/checkpoints."
        warned=$((warned + 1))
    fi
    if [[ "$rel" =~ $EXT_BLOCKLIST_RE ]]; then
        echo "[backup] WARN:     $name/$rel  has a non-source extension"
        echo "                   (binary/data file types are out of scope; check harness.yaml)"
        warned=$((warned + 1))
    fi

    mkdir -p "$(dirname "$dst")"
    cp -p "$src" "$dst"
    sha=$(sha256sum "$dst" | awk '{print $1}')
    echo "$sha" > "$sha_file"

    head=""
    if git -C "$repo" rev-parse HEAD >/dev/null 2>&1; then
        head=$(git -C "$repo" rev-parse HEAD)
    fi
    cat > "$meta_file" <<META
{
  "target": "$name",
  "rel_path": "$rel",
  "source_abs_path": "$src",
  "sha256": "$sha",
  "source_repo_head": "$head",
  "backed_up_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
META
    echo "[backup] OK:       $name/$rel  (sha=${sha:0:8})"
    made=$((made + 1))
done

echo ""
echo "[backup] done — made=$made skipped=$skipped missing=$missing warned=$warned"
echo "[backup] backups live under: $BACKUP_ROOT"
if [[ "$warned" -gt 0 ]]; then
    echo "[backup] NOTE: $warned file(s) violate the recommended scope"
    echo "        (text source only, <1 MiB).  Review your harness.yaml editable_files."
fi
