# Auto-Research Program — KERMT TLC Constraints

You are an autonomous research agent optimising **TLC Rf prediction** using
the KERMT (GROVER-base) framework.  Your actions are bounded by this document.

---

## Simplicity criterion

**All else being equal, simpler is better.**  A small metric improvement that
adds ugly complexity is not worth it.  Conversely, removing code/config and
getting equal or better results is a great outcome — that is a simplification
win.  When evaluating whether to keep a change, weigh the complexity cost
against the improvement magnitude.  A 0.001 MAE improvement that adds 30
lines of hacky code?  Probably not worth it.  A 0.001 MAE improvement from
deleting code?  Definitely keep.

---

## What you CAN do

1. **Edit `plan.md`** — add new YAML plan blocks or update `status` of
   existing blocks.  Every new plan MUST have a unique `PLAN_ID`, an `anchor`,
   an `axis` that is orthogonal to all prior axes in `log.md`, and an
   `expect` block.
2. **Append to `log.md`** — write exactly one summary line per completed
   training run (format defined below).
3. **Start training** — only via the whitelisted commands:
   ```bash
   cd ${KERMT_ROOT} && nohup python tlc/scripts/train.py \
       --config tlc/configs/<your_config>.yaml \
       > tlc/results/<dir>/nohup_train.log 2>&1 &
   ```
   or the `train_c_v3_v4.py` / `train_bidirectional_c_v1.py` variants with
   the same pattern.  For the demo: `python demo/train.py`.
4. **Create new YAML config files** under `${KERMT_ROOT}/tlc/configs/` —
   they must follow the schema of existing configs.
5. **Read any file** under `${RESULT_ROOT}`, `${KERMT_ROOT}/tlc/configs/`,
   `${SERVICE_ROOT}`.
6. **Edit Python code ONLY inside `# AGENT-EDITABLE-BEGIN` / `# AGENT-EDITABLE-END` blocks.**
   These blocks are explicitly marked in the following files:
   - `${KERMT_ROOT}/tlc/scripts/c_v3_c_v4_model.py`
   - `${KERMT_ROOT}/tlc/scripts/train_c_v3_v4.py`
   - `demo/model.py` (demo only)

   Rules for code edits:
   - You MUST NOT modify anything outside these markers.
   - Every code edit MUST be `git commit`-ed BEFORE training starts.
   - If the result is worse or crashes, the commit MUST be `git reset --hard`
     back to the previous good commit.
   - Keep diffs small and focused (one hypothesis per commit).

## What you CANNOT do

- Modify `${KERMT_ROOT}/kermt/` (core GNN source code).
- Edit Python code outside `AGENT-EDITABLE` blocks.
- Run arbitrary shell commands beyond the whitelisted training entry points.
- Delete or overwrite existing result directories.
- Install or remove Python packages.
- Modify `program.md` itself.
- Push to git remotes or rewrite published git history.

---

## Metric extraction

From `nohup_train.log`, the relevant lines are:

```
Epoch: NNNN loss_train: X.XXXXXX loss_val: X.XXXXXX mae_val: X.XXXX cur_lr: X.XXXXX t_time: Xs v_time: Xs
```

and at the end:

```
Model 0 test mae = X.XXXXXX
overall_scaffold_balanced_test_mae=X.XXXXXX
```

The **primary metric** is `overall_scaffold_balanced_test_mae` (lower is
better).  The **validation metric** used for tracking convergence is
`mae_val` from epoch lines.

Hyperparameters are read from `effective_config.yaml` in the same result
directory.  Key fields: `max_lr`, `init_lr`, `final_lr`, `weight_decay`,
`dropout`, `batch_size`, `epochs`, `early_stop_epoch`, `ffn_hidden_size`,
`ffn_num_layers`, `activation`, `solvent_emb_dim`, `checkpoint_path`.

---

## Training time budget

If `TRAIN_TIME_BUDGET_SEC` is set in `env.sh` (e.g. `600` for 10 minutes),
the service will kill any training process that exceeds this wall-clock limit.
Set to `0` or leave unset to let training run to completion (default).

This is useful for rapid iteration: short budget = many experiments overnight.

---

## Git experiment management

The service manages experiments via git:

- **Before training**: `git commit` all pending changes (config + code edits).
- **After training**:
  - If the primary metric **improves** (or meets plan expect): **keep** the
    commit.  Tag it `exp/<anchor>/<timestamp>`.
  - If the metric is **worse or equal**: `git reset --hard HEAD~1` to discard.
  - If training **crashes**: `git reset --hard HEAD~1`, log as `crash`.

The HEAD of the branch always represents the best-known configuration.

---

## log.md line format

```
TS=<ISO8601>;PLAN=<id>;ANCHOR=<dir>;AXIS=<axis>;TEST_MAE=<float>;BEST_VAL_MAE=<float>;STATUS=<ok|below_expect|crash|unmapped>;GIT=<keep|discard|crash>;HP=<key=val,...>
```

---

## Stop conditions

Iteration stops when ANY of:

1. `overall_scaffold_balanced_test_mae < ${GLOBAL_STOP_THRESHOLD}` (from env.sh).
2. All active plans have `status: completed` in `plan.md`.
3. `${MAX_CONSECUTIVE_FAILURES}` consecutive ticks produce no new results and
   no agent action.

---

## Plan generation rules

When proposing a new plan:

- The `axis` MUST differ from every axis already present in `log.md`.
- `orthogonal_to` MUST list all prior PLAN_IDs whose axes overlap the
  parameter space you are exploring.
- `expand: 3` means you must be able to derive 3 meaningfully different
  concrete configs from the intent (e.g. 3 learning rate values, or 3
  architectural variants).
- Keep `intent` to one sentence.
- Prefer smaller, testable hypotheses over sweeping changes.
- Apply the **simplicity criterion**: if two plans would achieve similar
  results, prefer the one with fewer moving parts.
