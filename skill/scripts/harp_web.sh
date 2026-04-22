#!/usr/bin/env bash
# harp_web.sh — launch the HARP web UI (FastAPI + HTMX).
#
# Single-page app for monitoring + controlling the HARP loop.  Pages:
#   - Dashboard : same as harp_status.sh, auto-refreshes
#   - Config    : edit meta_info/project.yaml + B/userprompt.yaml in browser
#   - Logs      : view log/memory/plan side-by-side with .state/zh/ polish
#   - Actions   : trigger polish / doctor / one-shot tick from buttons
#   - Usage     : per-cycle + cumulative token cost
#
# Usage:
#   bash harp_web.sh               # listen on 127.0.0.1:8765
#   PORT=9000 bash harp_web.sh     # custom port
#   HOST=0.0.0.0 bash harp_web.sh  # listen on all interfaces (DANGEROUS — no auth)
#   bash harp_web.sh --install     # ensure FastAPI/uvicorn installed, exit
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
ENGINE_DIR="$(dirname "$SKILL_DIR")"
WEB_DIR="$SKILL_DIR/web"

export PATH="$HOME/.local/bin:$PATH"
export PYTHONUNBUFFERED=1
export HARP_ENGINE_DIR="$ENGINE_DIR"
export HARP_SKILL_DIR="$SKILL_DIR"

PORT="${PORT:-8765}"
HOST="${HOST:-127.0.0.1}"

ensure_deps() {
  python3 - <<'PY' >/dev/null 2>&1 && return 0 || true
import fastapi, uvicorn, yaml  # noqa
PY
  echo "  installing fastapi + uvicorn (one-time, ~5 s) ..."
  pip install --quiet --user fastapi 'uvicorn[standard]' pyyaml 2>&1 | tail -3
}

case "${1:-}" in
  --install) ensure_deps; echo "✓ deps ready"; exit 0 ;;
  -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
  "") ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;;
esac

echo "==[ HARP web ]=================================="
echo "  engine     : $ENGINE_DIR"
echo "  serving on : http://$HOST:$PORT"
echo "  Ctrl-C to stop"
echo

ensure_deps

cd "$WEB_DIR"
exec python3 -m uvicorn app:app --host "$HOST" --port "$PORT" --no-access-log
