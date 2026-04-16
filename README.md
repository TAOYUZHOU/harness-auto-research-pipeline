# auto-research-service

Cron-driven autonomous experiment loop for **KERMT TLC Rf prediction**.

Periodically polls training result directories, extracts metrics, manages
experiments via git (keep/discard), updates `plan.md` / `log.md`, optionally
invokes an AI agent (Cursor CLI) to propose the next experiment — including
code edits within marked `AGENT-EDITABLE` blocks — and stops itself when the
target metric is reached.

Inspired by [autoresearch](https://github.com/karpathy/autoresearch) but
adapted for GNN regression tasks with YAML-based configs and code-level edits.

## Quick start

```bash
# 1. Edit env.sh to match your paths
vim env.sh

# 2. Install cron job (every N minutes)
bash scripts/install_cron.sh install

# 3. Or run a single tick manually
source env.sh && python3 scripts/poll_tick.py
```

## Demo (no GPU needed)

```bash
python3 demo/generate_data.py
cd demo && python3 train.py --epochs 30 --result_dir results/baseline && cd ..
source demo/env_demo.sh && python3 scripts/poll_tick.py
cat log.md   # should show DEMO_P0 result
```

See `demo/README.md` for details.

## Key features

- **Code edits via AGENT-EDITABLE blocks** — agent can modify model
  architecture within explicitly marked regions, not just YAML configs
- **Git experiment management** — auto-commit before training, keep on
  improvement, `git reset --hard` on regression
- **Training time budget** — optional wall-clock limit kills overtime runs
  for rapid iteration
- **Simplicity criterion** — program.md enforces "simpler is better" for
  the agent's decision making
- **Multi-experiment parallel monitoring** — scans all result dirs, not
  serial one-at-a-time
- **Orthogonal plan axes** — prevents redundant hyperparameter exploration
- **Auto-stop** — disables cron when global threshold is met

## Directory layout

```
env.sh              — all configurable paths & thresholds
program.md          — hard constraints for the agent
plan.md             — YAML-block experiment plans (axis, anchor, expect)
log.md              — append-only experiment result summaries
scripts/
  poll_tick.py      — main orchestrator (one cron tick)
  parse_log.py      — extract metrics from training logs
  invoke_agent.sh   — cursor CLI wrapper with dry-run fallback
  install_cron.sh   — cron lifecycle (install / remove / status)
demo/
  generate_data.py  — synthetic TLC-like data generator
  model.py          — simple MLP with AGENT-EDITABLE block
  train.py          — training script producing parseable logs
  env_demo.sh       — demo-specific environment overrides
.state/             — runtime state (gitignored)
```
