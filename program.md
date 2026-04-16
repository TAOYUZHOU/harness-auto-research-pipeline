# Auto-Research Program — KERMT TLC Constraints

You are an autonomous research agent optimising **TLC Rf prediction** using
the KERMT (GROVER-base) framework.  Your actions are bounded by this document.

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
   the same pattern.
4. **Create new YAML config files** under `${KERMT_ROOT}/tlc/configs/` —
   they must follow the schema of existing configs.
5. **Read any file** under `${RESULT_ROOT}`, `${KERMT_ROOT}/tlc/configs/`,
   `${SERVICE_ROOT}`.

## What you CANNOT do

- Modify `${KERMT_ROOT}/kermt/` (model source code) or any `*.py` training
  script.  Config-level tuning only.
- Run arbitrary shell commands beyond the whitelisted training entry points.
- Delete or overwrite existing result directories.
- Install or remove Python packages.
- Modify `program.md` itself.
- Push to git remotes or modify git history.

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

## log.md line format

```
TS=<ISO8601>;PLAN=<id>;ANCHOR=<dir>;AXIS=<axis>;TEST_MAE=<float>;BEST_VAL_MAE=<float>;STATUS=<ok|below_expect|crash|unmapped>;HP=<key=val,...>
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
