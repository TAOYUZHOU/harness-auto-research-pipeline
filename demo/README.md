# Demo — Self-contained pipeline validation

A minimal, synthetic example to verify the auto-research-service pipeline
without needing KERMT or GPU training.

## Quick start

```bash
cd auto-research-service

# 1. Generate synthetic TLC-like data (500 train, 100 val, 100 test)
python3 demo/generate_data.py

# 2. Run baseline training (~3 seconds, CPU only)
cd demo && python3 train.py --epochs 30 --lr 0.001 --result_dir results/baseline

# 3. Run a single poll tick (from repo root)
cd ..
source demo/env_demo.sh
python3 scripts/poll_tick.py
```

After step 3, check `log.md` — it should have one result line with
`PLAN=DEMO_P0`, `TEST_MAE=~0.07`, `STATUS=ok`.

## AGENT-EDITABLE block

`demo/model.py` contains a `# AGENT-EDITABLE-BEGIN` / `# AGENT-EDITABLE-END`
block where the agent (or you) can modify `hidden_dim`, `n_layers`,
`activation`, and `dropout`.  The training script produces logs in the same
format as KERMT, so `parse_log.py` can parse them.

## What this validates

- `parse_log.py` correctly extracts metrics from the demo log
- `poll_tick.py` discovers new logs, maps to plan anchors, appends to `log.md`
- Agent prompt is built and saved (dry-run mode if agent not installed)
- Global stop condition triggers when threshold is met
