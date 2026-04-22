# `meta_info/` — per-project personalization for HARP

This directory is the **only** place inside the HARP engine repo (A,
`SERVICE_ROOT`) where project-specific values live. Everything else in A is
deliberately project-agnostic so the engine itself can be reused across
arbitrary research projects.

## Why it exists

HARP has three repositories at runtime:

| Role | Path                        | Owns                                   |
|------|-----------------------------|----------------------------------------|
| A    | `harness-auto-research/`    | Engine code + project-agnostic templates |
| B    | `${workspace.dir}/`         | Live runtime config + research history |
| D    | `${target.repo_path}/`      | The codebase being optimised            |

Originally A held some project-specific values directly (in
`harness.yaml` / `userprompt.yaml` / `.cursorrules`), and B was created by
`cp`-ing those files. That coupled A and B together: any change to A's
templates leaked into B, and runtime scripts couldn't tell whether to read
A's "template" copy or B's "live" copy. The result was a class of bugs
where placeholders in A's templates (`<TARGET_NAME>`, `/absolute/path/...`)
silently fed into runtime via env.sh.

`meta_info/project.yaml` cuts the knot:

- **A holds only**: engine code, project-agnostic seed files
  (`program.md`, `memory.md`, `check.md`, `.mcp.json`, plan-registry
  template), and `meta_info/project.yaml` (this folder).
- **B holds**: everything the runtime needs (`harness.yaml`,
  `userprompt.yaml`, `.cursorrules`, `program.md`, ...), all
  rendered/copied at init time, owned by B thereafter.
- **The bridge**: `init_workspace.sh` reads `meta_info/project.yaml`
  ONCE, renders the templates with substituted values, writes them
  into B, then never touches B's config again. `env.sh` reads ONLY
  `meta_info/project.yaml::harness.workspace.dir` to know where B
  lives — every other runtime read targets `B/harness.yaml`.

## What goes in `project.yaml`

One file, three sections:

| Section        | Becomes                  | Substitution                           |
|----------------|--------------------------|----------------------------------------|
| `harness:`     | `B/harness.yaml`         | none — written verbatim                |
| `userprompt:`  | `B/userprompt.yaml`      | none — written verbatim                |
| `cursorrules:` | `B/.cursorrules`         | `${KERMT_ROOT}` `${SERVICE_ROOT}` `${WORK_DIR}` substituted from harness fields |

See `project.yaml` itself for the schema; it is heavily commented.

## Bootstrapping a new project

1. Copy `meta_info/project.yaml` to a new branch / fork of A.
2. Replace the `harness.targets[0]`, `harness.workspace.dir`,
   `userprompt.rules`, and `cursorrules.header` blocks with values for
   your project.
3. Run `bash scripts/init_workspace.sh` (it reads `meta_info/project.yaml`
   automatically). B is created and populated.
4. Run `bash scripts/quickstart.sh` to do the agent preflight.

## What you should never do

- **Never** edit `B/harness.yaml` and expect that change to flow back
  into `meta_info/project.yaml`. The data flow is A → B at init only.
  If you want to capture B's state for re-init, manually update
  `meta_info/project.yaml` to match.
- **Never** put project-agnostic content (engine logic, generic constraints)
  into `meta_info/`. That belongs in `program.md`, `check.md`, or scripts.
- **Never** reference `meta_info/project.yaml` from `B/`. B is decoupled
  from A by design — runtime code reads `B/harness.yaml` only.

---

## Lessons learned (engine-generic patterns)

The following patterns came out of real KERMT iterations and are baked
into the engine (`scripts/poll_tick.py`, `scripts/install_cron.sh`,
`env.sh`). They apply to any new project by default — no per-project
config needed — but are documented here so future authors know **why**
they exist and don't accidentally undo them.

### 1. Cron must run under bash, not dash
`source env.sh` and `set -o pipefail` are bash-only. `install_cron.sh`
wraps the tick command in `bash -lc '...'` because cron's default `/bin/sh`
is dash on most distros. Symptom if removed: cron fires but `tick.log`
never appears (silent failure).

### 2. Cold-start propose mode
A reactive-only loop (only fire on new logs) deadlocks on a fresh
workspace because preflight is forbidden from proposing. `poll_tick.run_tick()`
detects "no new logs ∧ no training in flight" and invokes the agent in
**propose mode** with an explicit hint to register a plan + kick off
training. Without this, the loop never starts on a fresh `meta_info`.

### 3. Line-anchored marker regexes
The agent's prose often *describes* its markers ("`STOP_ITERATION=1` is
**not** emitted") and a substring check (`"STOP_ITERATION=1" in output`)
yields a false positive that auto-uninstalls cron. All marker checks
(`STOP_ITERATION`, `PROGRAM_SYNC_DONE`, `MEMORY_DONE`) use
`re.compile(r"^\s*MARKER=1\s*$", re.MULTILINE)`. Never replace these
with substring checks.

### 4. Stream agent output to disk
`subprocess.run(timeout=...)` discards stdout on `TimeoutExpired`, so a
10-min timeout = zero diagnostic data. Use `_run_agent_streaming()`
(Popen + redirect to a file). Combined with `--output-format
stream-json`, partial output is *always* recoverable.

### 5. Token-cost accounting (F2)
Every agent invocation goes through `_run_agent_streaming()` with
`--output-format stream-json --stream-partial-output` and the `result`
event's `usage` dict is appended to `B/.state/usage.jsonl`. A rolling
summary lives in `B/.state/usage_summary.txt`. This is generic — any
project that uses the cursor-agent CLI inherits it for free.

### 6. In-flight log scanning + abandon-on-plateau (F3)
Long-running training (hours per cycle) blocks the loop unless the
engine can introspect partial progress. `collect_inflight_runs()`
parses every `nohup_train.log` in `RESULT_ROOT` each tick, extracts
`(epoch, val_metric)` pairs, computes `best_val_so_far` and
`plateau_epochs`, and:

- appends a `STATUS=in_progress` line to `log.md` whenever the best
  improves OR ≥ 5 epochs advance (deduped via
  `B/.state/inflight_emit.json`);
- enters **monitor mode** (waking the agent mid-training) when any
  in-flight run shows `plateau_epochs ≥ 10` so the agent can decide
  whether to manually `git_discard` and start the next plan.

The default regex captures `Epoch: NNNN ... mae_val: X.XXXX`. If a new
project uses a different log format, override per target via:

```yaml
harness:
  targets:
    - name: yourproj
      inflight_pattern: "step (?P<epoch>\\d+) .*loss=(?P<metric>[0-9.]+)"
      inflight_metric_name: "loss"
```

### 7. Two-stage screening pattern
Recommended (but not mandatory) addition to `userprompt.rules` for any
project where one full training run is expensive (> 1 h):

> Each new axis runs first as a screening cycle (epochs ≤ 80,
> early_stop_epoch ≤ 20, anchor suffix `_scr`). Only after two
> consecutive screenings improve `best_val` ≥ 5 % does a full
> (epochs=200, early_stop=50) run get scheduled.

Combined with the abandon-on-plateau rule, this caps wall-clock per
unverified direction at ~ 1/4 of a full run.

### 8. Workspace remote auto-create
Long iterations on remote SSH boxes that may sleep need a Git remote
backup. `harness.workspace_remote.mode = auto` triggers
`init_workspace.sh` to create + push to a `gh repo create`-style remote.
Failure mode: `gh CLI` requires `read:org` for *itself* even when only
`repo` scope is needed for the API. The escape hatch is a direct
`curl -X POST https://api.github.com/user/repos` with the user's PAT
(works with classic PAT + `repo` scope alone).

### 9. Program-constitution hash
`B/program.md` is the contract; the agent may only edit content inside
the `<!-- USER-INJECTED-BEGIN/END -->` markers. Preflight records
`sha256(program.md - USER-INJECTED block)` to
`B/.state/program_constitution.sha256`, and every tick verifies. Drift
triggers atomic rollback + `constitution_drift` stop. Never disable.

### 10. SCRIPT_DIR variable hygiene
`env.sh` is sourced by other scripts. Internal helper variables MUST
be prefixed with `_HARP_` (e.g. `_HARP_ENV_DIR`) to avoid clobbering
caller-defined `SCRIPT_DIR`, which silently breaks
`backup_originals.sh` and friends.
