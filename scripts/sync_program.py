#!/usr/bin/env python3
"""sync_program.py - manual template upgrade for WORK_DIR/program.md.

After init, repo B (WORK_DIR) owns its own program.md. The harness
NEVER auto-syncs B/program.md from A/program.md per tick (that would
contradict the "A is template-only" architecture). When the engineer
upgrades A/program.md (e.g. tightens a CANNOT-do rule), this script is
the only sanctioned way to propagate the change into B.

What it does:
  1. Reads template SERVICE_ROOT/program.md (A) and live WORK_DIR/program.md (B).
  2. Builds proposed = A's template with B's existing USER-INJECTED block.
  3. If proposed == current B -> already in sync (exit 0, no-op).
  4. Otherwise prints unified diff and prompts y/N (or --yes).
  5. On confirmation: archives diff to .state/program_sync_<UTC_TS>.diff,
     overwrites B/program.md with proposed, refreshes
     .state/program_constitution.sha256.

Usage:
    python3 scripts/sync_program.py             # interactive
    python3 scripts/sync_program.py --yes       # non-interactive apply
    python3 scripts/sync_program.py --dry-run   # show diff, no changes

Exit codes:
    0  success or no-op
    1  user declined / dry-run only
    2  template or workspace file missing
    3  USER-INJECTED markers missing in template (corrupt template)
"""
from __future__ import annotations

import difflib
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from poll_tick import (  # noqa: E402
    PROGRAM_FILE,
    WORKSPACE_PROGRAM_FILE,
    PROGRAM_CONST_HASH_FILE,
    STATE_DIR,
    USER_INJECTED_BEGIN,
    USER_INJECTED_END,
    USER_INJECTED_RE,
    _extract_user_injected,
    record_program_constitution_hash,
)


def build_proposed(template: str, current_b: str) -> str:
    if not USER_INJECTED_RE.search(template):
        raise ValueError(
            "Template SERVICE_ROOT/program.md is missing the "
            f"{USER_INJECTED_BEGIN}/{USER_INJECTED_END} markers - "
            "cannot do a structured merge."
        )
    user_block = _extract_user_injected(current_b)
    if user_block is None:
        user_block = "\n"
    return USER_INJECTED_RE.sub(
        lambda _m: f"{USER_INJECTED_BEGIN}{user_block}{USER_INJECTED_END}",
        template,
        count=1,
    )


def render_diff(current: str, proposed: str) -> str:
    return "".join(difflib.unified_diff(
        current.splitlines(keepends=True),
        proposed.splitlines(keepends=True),
        fromfile="WORK_DIR/program.md (current)",
        tofile="WORK_DIR/program.md (proposed)",
        n=3,
    ))


def main(argv: list[str]) -> int:
    yes = "--yes" in argv or "-y" in argv
    dry_run = "--dry-run" in argv

    if not PROGRAM_FILE.exists():
        print(f"[sync_program] FATAL: template not found: {PROGRAM_FILE}",
              file=sys.stderr)
        return 2
    if not WORKSPACE_PROGRAM_FILE.exists():
        print(f"[sync_program] FATAL: workspace program.md not found: "
              f"{WORKSPACE_PROGRAM_FILE} - run init_workspace.sh first",
              file=sys.stderr)
        return 2

    template = PROGRAM_FILE.read_text()
    current_b = WORKSPACE_PROGRAM_FILE.read_text()

    try:
        proposed = build_proposed(template, current_b)
    except ValueError as e:
        print(f"[sync_program] FATAL: {e}", file=sys.stderr)
        return 3

    if proposed == current_b:
        print("[sync_program] no-op: B/program.md already matches A "
              "(modulo USER-INJECTED). Nothing to do.")
        return 0

    diff_text = render_diff(current_b, proposed)
    print(f"[sync_program] proposed change to {WORKSPACE_PROGRAM_FILE}:")
    print("[sync_program] (USER-INJECTED block is preserved verbatim)")
    print("-" * 72)
    sys.stdout.write(diff_text)
    if not diff_text.endswith("\n"):
        sys.stdout.write("\n")
    print("-" * 72)

    if dry_run:
        print("[sync_program] --dry-run: no changes written")
        return 1

    if not yes:
        try:
            ans = input("[sync_program] apply this merge? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in {"y", "yes"}:
            print("[sync_program] declined - no changes written")
            return 1

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    diff_path = STATE_DIR / f"program_sync_{ts}.diff"
    diff_path.write_text(diff_text)
    print(f"[sync_program] diff archived -> {diff_path}")

    WORKSPACE_PROGRAM_FILE.write_text(proposed)
    print(f"[sync_program] applied to {WORKSPACE_PROGRAM_FILE}")

    new_hash = record_program_constitution_hash()
    print(f"[sync_program] constitution hash refreshed: "
          f"sha256={new_hash[:16]}... -> {PROGRAM_CONST_HASH_FILE}")
    print("[sync_program] DONE - next tick will treat this as the new "
          "canonical constitution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
