# auto-research-service

Cron-driven autonomous experiment loop for **KERMT TLC Rf prediction**.

Periodically polls training result directories, extracts metrics, updates
`plan.md` / `log.md`, optionally invokes an AI agent (Cursor CLI) to propose
the next experiment, and stops itself when the target metric is reached.

Inspired by [autoresearch](https://github.com/karpathy/autoresearch) but
adapted for GNN regression tasks with YAML-based configs.

## Quick start

```bash
# 1. Edit env.sh to match your paths
vim env.sh

# 2. Install cron job (every N minutes)
bash scripts/install_cron.sh

# 3. Or run a single tick manually
source env.sh && python3 scripts/poll_tick.py
```

## Directory layout

```
env.sh              — all configurable paths & thresholds (source before use)
program.md          — hard constraints for the agent (what it CAN / CANNOT do)
plan.md             — YAML-block experiment plans (axis, anchor, expect)
log.md              — append-only experiment result summaries
scripts/
  poll_tick.py      — main orchestrator (one cron tick)
  parse_log.py      — extract metrics from nohup_train.log + effective_config
  invoke_agent.sh   — wrapper: call cursor agent or dry-run
  install_cron.sh   — write / remove crontab entry
.state/
  last_scan.json    — per-log mtime checkpoint
  iteration_active  — "true" / "false"
  tick.lock         — flock guard
```

## How it works

Each **tick** (triggered by cron or manual run):

1. **Lock** — `flock` prevents concurrent ticks.
2. **Scan** — find `nohup_train.log` files newer than last checkpoint.
3. **Parse** — extract `mae_val`, `test_mae`, hyperparams from each new log.
4. **Map** — match result dir name → `anchor:` in `plan.md`.
5. **Log** — append one summary line per new result to `log.md`.
6. **Agent** — (if installed) feed `program.md + plan.md + log.md` → agent
   proposes next plan or starts training.
7. **Stop check** — if global threshold met or plan expects satisfied →
   disable cron, write `iteration_active=false`.
