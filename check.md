# HARP preflight self-check (agent-executed)

When the harness invokes you with `--mode preflight` (e.g. via
`scripts/quickstart.sh`), the prompt that wraps this file replaces the
normal "what experiment to run next" loop with the **bootstrap
checklist** below.

You are still bound by `program.md` (constraints, simplicity criterion,
USER-INJECTED rules, scope-audit allowlist).  Anything outside the
allowlist will be atomically rolled back exactly the same way it would
be during a normal tick — preflight is **not** a free-for-all.

The checklist below is the **only** work you do this invocation.  Do
NOT propose new experiments, do NOT start training, do NOT tag with
anchors that don't already exist on disk.  The next normal tick will
do all of that.

---

## Step 0 — sync USER-INJECTED block (if needed)

If the wrapper prompt contains `=== PROGRAM SYNC REQUIRED ===`,
translate `userprompt.yaml` into the canonical HARD CONSTRAINT format
inside `<!-- USER-INJECTED-BEGIN --> / <!-- USER-INJECTED-END -->` of
`${WORK_DIR}/program.md` and emit `PROGRAM_SYNC_DONE=1` exactly as
described in program.md §"USER-INJECTED RULES".  This is identical to
the regular-tick behaviour, just done first thing.

If the directive is absent, skip Step 0.

---

## Step 1 — classify each editable file (marker-scoped vs whole-file)

For every `targets[].editable_files` entry in `harness.yaml`, inspect
the file (GitNexus `query` / `context`, or fall back to `cat | grep`)
and count matched pairs of:

```
# AGENT-EDITABLE-BEGIN: <slug>
# AGENT-EDITABLE-END: <slug>
```

This is **classification, not validation** — both outcomes are
legitimate per program.md §7:

- **`COUNT > 0`** → marker-scoped mode.  In normal ticks you may only
  edit lines strictly between marker pairs.  Emit:
  ```
  PREFLIGHT_INFO=editable_mode=marker FILE=<rel/path> SLUGS=<csv> COUNT=<n>
  ```
- **`COUNT == 0`** → whole-file mode.  In normal ticks the entire
  file is editable.  Emit:
  ```
  PREFLIGHT_INFO=editable_mode=whole FILE=<rel/path>
  ```

A file that is **declared in `editable_files` but missing on disk** is
a real failure:

```
PREFLIGHT_FAIL=editable_file_missing FILE=<rel/path>
```

You MUST NOT add or remove markers yourself in either case — marker
layout is a human engineer-level decision (program.md §7).

---

## Step 2 — verify result_path is reachable and non-empty

For each target, list the immediate subdirectories of
`targets[].result_path`.  Each subdirectory whose name contains a
`nohup_train.log` is a candidate prior experiment.

Emit:

```
PREFLIGHT_INFO=result_path TARGET=<name> EXISTS=<yes|no> RUN_COUNT=<n>
```

If a target has zero candidate runs and no `baseline_anchor` is set in
its `harness.yaml`, emit `PREFLIGHT_WARN=no_baseline TARGET=<name>` and
skip Step 5 for that target — the user will have to either (a) train
once manually, or (b) set `baseline_anchor` to a planned anchor name.

---

## Step 3 — pick the baseline anchor

For each target:

1. If `targets[].baseline_anchor` is set in `harness.yaml`, that is the
   baseline.  Find the matching subdir under `result_path`.  If the
   subdir does not exist, emit
   `PREFLIGHT_FAIL=baseline_anchor_missing TARGET=<name> ANCHOR=<...>`
   and stop — do not write anything else for that target.
2. Otherwise, parse the metric (default `best_val_mae` per
   `targets[].primary_metric`) from every candidate run's
   `nohup_train.log` (use `parse_log.py` semantics: regex
   `Best val MAE: ([\d.]+) at epoch (\d+)` plus per-epoch
   `mae_val:` lines).  Pick the run with the best value (lowest if
   `metric_op=lt`, highest otherwise).
3. Emit `PREFLIGHT_INFO=baseline TARGET=<name> ANCHOR=<...> METRIC=<key>=<val> EPOCH=<...>`.

---

## Step 4 — register the baseline metric in WORK_DIR

Write the chosen baseline metric value (single float, no other text) to
`${WORK_DIR}/.state/best_metric.txt`.  This is the only file under
`.state/` you ever write directly; it's gitignored so the post-tick
audit will not see it.  Do not touch any other `.state/` file.

If `best_metric.txt` already exists and its current value is at least
as good as the baseline (lower for `lt`, higher for `gt`), DO NOT
overwrite it — the user is re-running preflight on top of a workspace
that already has improvements; emit
`PREFLIGHT_INFO=best_metric_kept VALUE=<existing>` and move on.

---

## Step 5 — write the baseline `## EXP_ID:` block to `memory.md`

Append exactly one block to `${WORK_DIR}/memory.md` using the format
from program.md and the template at the top of `memory.md` itself.
Use:

- `EXP_ID = <baseline_anchor>__BASELINE`  (the literal string
  `BASELINE` instead of a timestamp signals "this is the starting
  point, not an exp/<...> tag in workspace git").
- `PARENT_PLAN: baseline`
- `VERDICT: keep (starting point)`
- Fill `### Motivation`, `### Hypothesis` (= `None — reference point`),
  `### What changed` (= `none / starting state`), `### Result
  interpretation`, `### Lesson / Next` based on what you can actually
  observe in the baseline log + config.  Cite at least one concrete
  number (e.g. final `loss_train`, `mae_val`, train/val gap).
- Then emit `MEMORY_DONE=<EXP_ID>` so the (currently empty) pending
  queue stays empty.  This is also the contractual signal that the
  preflight succeeded for this target.

If a `## EXP_ID: <baseline_anchor>__BASELINE` block already exists in
`memory.md`, DO NOT append a duplicate.  Emit
`PREFLIGHT_INFO=memory_baseline_exists EXP_ID=<id>` and skip.

---

## Step 6 — tag the baseline commit in the *target* repo (best-effort)

In `targets[].repo_path`, run (via the agent's shell tool, NOT via a
new file):

```
git tag -a baseline/<anchor> -m "HARP baseline: <metric>=<val> epoch <e>"
```

The tag is local to the repo; the post-tick audit ignores tag-only
operations because they don't change `git diff --name-only` or
`git status`.  If the tag already exists, skip.

If git tag fails for any reason (no git, detached HEAD, etc.), emit a
warning but do not fail preflight:

```
PREFLIGHT_WARN=baseline_tag_failed TARGET=<name> REASON=<one-liner>
```

---

## Step 7 — final report

End your output with a one-line summary the harness can grep for:

```
PREFLIGHT_DONE=1 TARGETS_OK=<n> WARNINGS=<m> FAILS=<k>
```

If `FAILS > 0`, the harness will refuse to start cron and the user
must address each `PREFLIGHT_FAIL=` line before re-running quickstart.

---

## What you MUST NOT do during preflight

- Propose new plans or modify `plan.md` (that's the next tick's job).
- Modify `editable_files` content (no experiments yet).
- Create files in `${agent_addition_dir}` (no experiments yet).
- Train anything.
- Commit to the target repo (only `git tag` is allowed, see Step 6).
- Write anywhere in `WORK_DIR` other than:
  - `program.md` USER-INJECTED block (Step 0, only if directed)
  - `memory.md` (append, Step 5)
  - `.state/best_metric.txt` (Step 4 only)
- Edit `harness.yaml`, `userprompt.yaml`, `check.md`, or any
  `scripts/` file — those belong to the user and the template.
