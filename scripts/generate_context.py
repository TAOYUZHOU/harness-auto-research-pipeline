#!/usr/bin/env python3
"""
Append the dynamic-scan section to WORK_DIR/.cursorrules.

The base header of .cursorrules is rendered ONCE at workspace init by
init_workspace.sh from meta_info/project.yaml::cursorrules.header (with
${SERVICE_ROOT}/${WORK_DIR}/${KERMT_ROOT} substituted).  This script appends
"available scripts / configs / top results" tables that change as the user
adds experiments to the target repo.

Idempotent: re-running replaces only the auto-scan block (between the two
HARP-AUTOSCAN markers), leaving the meta_info-derived header untouched.

Project-agnostic: every path comes from B/harness.yaml (targets[0]).
Sections that don't apply (no train*.py, no configs dir, ...) degrade
to a one-line placeholder.

Usage:
    source env.sh && python3 scripts/generate_context.py
"""

import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("[FATAL] PyYAML not installed. Run: pip install pyyaml")

sys.path.insert(0, str(Path(__file__).parent))
try:
    from parse_log import parse_training_log
    HAS_PARSER = True
except Exception:
    HAS_PARSER = False  # parse_log absent => skip results scan, don't crash

SERVICE_ROOT = Path(os.environ.get("SERVICE_ROOT", "")) or None
WORK_DIR = Path(os.environ.get("WORK_DIR", "")) or None

SCAN_BEGIN = "<!-- HARP-AUTOSCAN-BEGIN -->"
SCAN_END   = "<!-- HARP-AUTOSCAN-END -->"


def _load_target():
    if not WORK_DIR or not (WORK_DIR / "harness.yaml").exists():
        sys.exit(f"[context] FATAL: WORK_DIR/harness.yaml not found "
                 f"(WORK_DIR={WORK_DIR}). Source env.sh first.")
    with open(WORK_DIR / "harness.yaml") as f:
        h = yaml.safe_load(f) or {}
    targets = h.get("targets") or []
    if not targets:
        sys.exit("[context] FATAL: no targets[] in B/harness.yaml")
    t = targets[0]
    return {
        "name": t.get("name", "target"),
        "repo": Path(t["repo_path"]),
        "config_dir": t.get("config_dir", ""),
        "result_path": Path(t.get("result_path", "")),
        "log_glob": t.get("log_glob", "nohup_train.log"),
    }


def scan_training_scripts(repo: Path) -> str:
    for sub in ("tlc/scripts", "scripts", "src/scripts"):
        d = repo / sub
        if d.is_dir():
            scripts = sorted(d.glob("train*.py"))
            if scripts:
                lines = ["| Script | Path |", "|--------|------|"]
                for s in scripts:
                    lines.append(f"| `{s.name}` | `{s.relative_to(repo)}` |")
                return "\n".join(lines) + "\n"
    return "_(no train*.py scripts found in tlc/scripts/, scripts/, src/scripts/)_\n"


def scan_configs(repo: Path, config_dir: str) -> str:
    if not config_dir:
        return "_(targets[0].config_dir not set in harness.yaml)_\n"
    d = repo / config_dir
    if not d.is_dir():
        return f"_(config dir `{config_dir}` not found under {repo})_\n"
    yamls = sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml"))
    if not yamls:
        return "_(no YAML configs found)_\n"
    return "\n".join(f"- `{config_dir}/{y.name}`" for y in yamls) + "\n"


def scan_results(result_root: Path, log_glob: str, top_n: int = 10) -> str:
    if not HAS_PARSER:
        return "_(parse_log.py unavailable; results scan skipped)_\n"
    if not result_root.is_dir():
        return f"_(result_path {result_root} not found)_\n"
    results = []
    for logp in sorted(result_root.glob(f"*/{log_glob}")):
        try:
            r = parse_training_log(str(logp))
        except Exception as e:
            print(f"[context] WARN: parse failed for {logp.name}: {e}",
                  file=sys.stderr)
            continue
        if r.is_complete and r.test_mae is not None:
            results.append(r)
    results.sort(key=lambda r: r.test_mae)
    results = results[:top_n]
    if not results:
        return "_(no completed results parsed)_\n"
    lines = ["| Rank | Experiment | test MAE | best val MAE | Key HP |",
             "|------|-----------|----------|-------------|--------|"]
    for i, r in enumerate(results, 1):
        hp = r.hp_summary()
        if len(hp) > 60:
            hp = hp[:57] + "..."
        bv = r.best_val_mae if r.best_val_mae is not None else "N/A"
        lines.append(
            f"| {i} | `{r.anchor}` | {r.test_mae:.4f} | {bv} | {hp} |"
        )
    return "\n".join(lines) + "\n"


def build_scan_section(t: dict) -> str:
    parts = [
        SCAN_BEGIN,
        "",
        "## Available training scripts (auto-scanned)",
        "",
        scan_training_scripts(t["repo"]),
        "",
        "## Existing configs (auto-scanned)",
        "",
        scan_configs(t["repo"], t["config_dir"]),
        "",
        f"## Current results (top 10 by test MAE — auto-scanned from `{t['result_path']}`)",
        "",
        scan_results(t["result_path"], t["log_glob"]),
        SCAN_END,
        "",
    ]
    return "\n".join(parts)


def main():
    if not WORK_DIR or not WORK_DIR.is_dir():
        sys.exit(f"[context] WORK_DIR not set or not found: {WORK_DIR}")
    target = _load_target()
    if not target["repo"].is_dir():
        sys.exit(f"[context] target repo not found: {target['repo']}")

    cursorrules = WORK_DIR / ".cursorrules"
    base = cursorrules.read_text() if cursorrules.exists() else ""

    if SCAN_BEGIN in base and SCAN_END in base:
        pre  = base.split(SCAN_BEGIN, 1)[0].rstrip() + "\n"
        post = base.split(SCAN_END,   1)[1].lstrip("\n")
        base = pre + post

    section = build_scan_section(target)
    out = base.rstrip() + "\n\n" + section
    cursorrules.write_text(out)
    print(f"[context] updated {cursorrules} ({len(out)} bytes; "
          f"target={target['name']})")


if __name__ == "__main__":
    main()
