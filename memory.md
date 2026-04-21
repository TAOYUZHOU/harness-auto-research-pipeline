# HARP research journal

Append-only narrative of every closed experiment.  One block per
experiment.  Format (the harness echoes this same skeleton in the
`PENDING MEMORY ENTRIES` directive — keep them in sync):

    ## EXP_ID: <anchor>__<YYYYMMDDTHHMMSSZ>
    - TS:           <YYYYMMDDTHHMMSSZ>
    - PARENT_PLAN:  <PLAN_ID from plan.md, or "baseline" / "preflight">
    - ANCHOR:       <result-dir name>
    - VERDICT:      keep | discard
    - METRIC:       test_mae=<x>; best_val_mae=<y>; delta_vs_prev_best=<+/-z or N/A>

    ### Motivation
    Why did we try this?  Cite at least one prior EXP_ID, one
    userprompt.yaml rule, or one specific log.md observation.  No
    vague claims like "improve performance".

    ### Hypothesis
    A single falsifiable "if X then Y" statement.

    ### What changed
    - editable_files diff: <file>:<line range> (one-line summary, or "none")
    - new files under add_by_HARP/: <list> (or "none")
    - new YAML configs: <list> (or "none")

    ### Result interpretation
    Compare to the hypothesis.  Did the result support, refute, or
    partially refute it?  Quote the relevant numbers.

    ### Lesson / Next
    - What is now established?
    - Which directions are pruned?
    - Which direction is the next obvious experiment?

EXP_ID matches the suffix of the corresponding `exp/<anchor>/<ts>` git
tag in WORK_DIR, so each entry is one click away from the underlying
code diff.

The agent NEVER edits past entries.  To correct a wrong claim, append
a new entry that supersedes the prior one and references its EXP_ID
in `### Motivation`.

The harness injects the most recent K blocks (default 5, configurable
via `agent.memory_tail_blocks` in harness.yaml) into every tick's
prompt.  Older blocks remain on disk and are queryable via GitNexus.

---

<!-- The first real entry below this line is normally written by the
     preflight agent (see check.md, "Step 5 — register baseline").
     Until then, the file is intentionally empty so a fresh tick sees
     "(memory.md is empty — this is the first experiment journal entry
     to come)" in the prompt. -->
