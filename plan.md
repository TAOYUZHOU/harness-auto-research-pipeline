# HARP plan registry — TEMPLATE

This file in SERVICE_ROOT is the **template** copied into every new
workspace. The real, project-specific plans live in WORK_DIR/plan.md;
the agent edits that copy on every tick (registers new plans, marks
them `running`/`completed`/`abandoned`).

The harness reads the global stop condition from
WORK_DIR/harness.yaml:

    targets[].primary_metric
    targets[].metric_op           (lt | gt)
    targets[].stop_threshold

so plans only need to declare per-plan target thresholds (a sub-goal
on the way to the global threshold).

## Convention

Each plan is a YAML block separated by `---`. Required fields:

| field | meaning |
|---|---|
| `PLAN_ID` | unique short id (`p1`, `p2`, … or descriptive snake_case) |
| `anchor` | result-dir name the agent will create when this plan runs. Must be unique across plan.md + log.md. |
| `axis` | one-word handle for the axis being explored. Two `pending`/`running` plans MUST NOT share an axis (orthogonality rule). |
| `status` | `pending` \| `running` \| `completed` \| `abandoned` |
| `metric`, `op`, `threshold` | per-plan success criterion (sub-goal). |
| `motivation` | WHY (must cite a prior `EXP_ID`, a `userprompt.yaml` rule, or a `log.md` observation — see `program.md` §"Plan generation rules"). |
| `expect` | mirror of `metric`/`op`/`threshold` for the harness to validate. |

## Lifecycle

1. Agent (or this template) registers a plan with `status: pending`.
2. When the agent picks the plan to run, it flips `status: running`
   and starts training.
3. parse_log writes the result to `log.md`. The agent reads it,
   judges keep/discard against `expect.threshold`, and flips
   `status: completed` (kept) or `abandoned` (rejected, exhausted,
   or superseded).
4. When `## EXP_ID:` block lands in `memory.md`, the corresponding
   plan must already be in `completed` or `abandoned` state.

## Seeding

This template ships with **one example plan** (below) so the agent
has a starting point on the very first tick of a fresh workspace.
Replace it with real plans for your project before running
`quickstart.sh`, OR leave it in place — the agent will register its
own first plan during the first non-preflight tick anyway, citing
your `userprompt.yaml` rules + the baseline numbers from `memory.md`.

---

### PLAN_ID: p1_example
anchor: <baseline_anchor>__<short_descriptive_suffix>
axis: <one_word_axis_handle>
status: pending
metric: <primary_metric_from_harness_yaml>
threshold: <number_better_than_baseline_but_short_of_global_stop_threshold>
op: lt                                  # or gt, must match metric_op
orthogonal_to: []                       # other PLAN_IDs this plan must not overlap with
expand: 1                               # 1 = single point experiment; >1 = sweep size
motivation: |
  REPLACE THIS BLOCK before running. A good motivation:
    - Cites the baseline (EXP_ID = <baseline_anchor>__BASELINE) and
      its concrete numbers (e.g. "best_val_mae=X at epoch Y, train
      loss converged to Z by epoch W").
    - Cites at least one rule in WORK_DIR/userprompt.yaml that this
      plan respects or directly addresses.
    - Names the EXACT code/config surface to be changed (which
      editable_files file, which config field, or which new file
      under add_by_HARP/).
    - Explains WHY this is the cheapest, most informative orthogonal
      probe to try next — not "improve performance".
expect:
  metric: <primary_metric_from_harness_yaml>
  op: lt                                # mirror of `op` above
  threshold: <same_number_as_above>
