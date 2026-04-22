# HARP — Harness for Auto-Research Pipelines

Use when the user wants to **set up an autonomous, cron-driven research
loop** on top of an existing ML training repo, asks about
"auto-research / auto-experiment / agent-driven hyperparameter search",
or needs to optimise a metric (e.g. MAE) over many configurations
without manual babysitting.

Example triggers:

- "帮我把这个 ML 项目接入自动迭代 / 自动调参"
- "set up HARP for repo X / 给 X 装 HARP"
- "自动跑实验直到 MAE < 0.05"
- "为 KERMT 之外的项目复用这个 harness"
- "怎么让 cursor-agent 自己跑实验循环"

---

## What HARP gives the user

A self-running loop that, every 15 minutes:

1. Scans for newly-finished training runs in the target repo.
2. Decides keep / discard via Git (commit + tag on improvement, reset
   on regression).
3. Updates `plan.md` (next experiments) and `memory.md` (lessons learned).
4. Wakes the agent only when there's something to decide (new result,
   plateau, cold start) — saves tokens.
5. Tracks token cost and writes Chinese-polished summaries to
   `.state/zh/` for human-readable progress tracking.
6. Stops when a global threshold is hit, max cycles reached, or the
   agent declares convergence.

It runs on a remote SSH machine that may go to sleep — Git is the
durable handoff between ticks.

---

## Three repos at runtime (HARP terminology)

| Role | Path                     | Owns                                |
|------|--------------------------|-------------------------------------|
| **A** | `harness-auto-research/` | Engine code + this skill (read-only at runtime) |
| **B** | `${workspace.dir}/`      | Live config + research history (the "workspace") |
| **D** | `${target.repo_path}/`   | The ML codebase being optimised      |

`meta_info/project.yaml` in A is the **only** place per-project values
live. `init_workspace.sh` renders it into B once; everything afterwards
is owned by B.

---

## What this skill does (workflow)

When invoked, the agent should:

### 1. Discover

```bash
# Find the engine on disk (or clone if missing)
ENGINE_DIR=$(harp_find_engine.sh)   # echoes path, exits 1 if not found
```

### 2. Bootstrap a new project (`harp_init.sh`)

Ask the user for these 4 values, then render
`<ENGINE>/meta_info/project.yaml`:

| # | Question | Example answer |
|---|----------|----------------|
| 1 | Target repo path (D) | `/abs/path/to/myproject` |
| 2 | Training command template (must produce a `nohup_train.log`) | `nohup python train.py --config {cfg} > results/{anchor}/nohup_train.log 2>&1 &` |
| 3 | Primary metric name + direction | `best_val_mae`, `lt` (lower is better) |
| 4 | Starting baseline value (for the stop_threshold cue) | `0.0662` |

Then run:

```bash
bash $ENGINE_DIR/scripts/init_workspace.sh
bash $ENGINE_DIR/scripts/quickstart.sh   # preflight + verify
bash $ENGINE_DIR/scripts/install_cron.sh install
```

### 3. Monitor (`harp_status.sh`)

One-screen dashboard: cycle / best / in-flight training / token usage /
recent log lines / git remote sync status.

### 4. Polish for human consumption (`harp_polish.sh`)

Translates `log.md`, `memory.md`, `plan.md` into Chinese summaries via
a **fresh `cursor-agent` chat** (no `--resume`), preserving grep-key
fields. Runs as a background daemon (`harp_polish_daemon.sh`) or
on-demand (`harp_polish.sh --once`).

### 5. Health check (`harp_doctor.sh`)

Quickly verifies cron / agent CLI / git remote / disk space / engine
consistency. Run when something feels stuck.

### 6. Web UI (`harp_web.sh`) — optional GUI

A single-page FastAPI app on top of all the above scripts. Browse the
dashboard, edit `meta_info/project.yaml` + `userprompt.yaml` in a
textarea (with YAML validation), view raw + Chinese-polished
`log/memory/plan` side-by-side, click buttons to run polish/doctor/tick
with live SSE output, watch token cost over time, and tail `tick.log`
in real time. See [`web/README.md`](web/README.md).

---

## Usage examples

```bash
# Brand-new project (interactive)
bash skill/scripts/harp_init.sh

# Status check (any time, including from cron)
bash skill/scripts/harp_status.sh

# One-off polish (translate latest log/memory/plan to zh)
bash skill/scripts/harp_polish.sh --once

# Background polish daemon (mtime-driven, 60s poll)
nohup bash skill/scripts/harp_polish_daemon.sh > /tmp/harp_polish.log 2>&1 &

# Health check
bash skill/scripts/harp_doctor.sh

# Web UI (optional)
bash skill/scripts/harp_web.sh   # then open http://127.0.0.1:8765
```

---

## Required environment

- `cursor-agent` CLI in `$PATH` (see https://cursor.com/cli)
- `git` ≥ 2.30
- `bash` ≥ 4
- `python3` ≥ 3.10
- `gh` CLI (only if `workspace_remote.mode = auto`)
- A target repo (D) that already has reproducible training entry-point

---

## Files written by HARP at runtime (in workspace B)

| File | Owner | Purpose |
|------|-------|---------|
| `B/harness.yaml` | rendered from A, then frozen | runtime config |
| `B/userprompt.yaml` | rendered, agent-readable | natural-language constraints |
| `B/.cursorrules` | rendered | per-project agent context |
| `B/program.md` | engine + agent (USER-INJECTED block only) | constitution |
| `B/plan.md` | agent | next experiments |
| `B/memory.md` | agent | lessons learned |
| `B/log.md` | engine | append-only event log |
| `B/.state/usage.jsonl` | engine | per-tick token cost |
| `B/.state/zh/*.zh.md` | polish daemon | Chinese summaries |

Engine never edits B outside its own files. Agent never edits A.

---

## Anti-patterns (don't)

- ❌ Edit `B/harness.yaml` and expect it to flow back into
  `meta_info/project.yaml`. The data flow is A → B at init only.
- ❌ Delete `B/.state/program_constitution.sha256` — the engine uses
  it to detect unauthorised edits to the constitution.
- ❌ Put project-specific values into A (outside `meta_info/`). They
  belong in `meta_info/project.yaml`.
- ❌ Run multiple HARP instances against the same workspace B
  concurrently — `B/.state/tick.lock` exists but cron handles serialisation.

---

## See also

- `INSTALL.md` — how to install this skill into Cursor (`~/.cursor/skills/`)
  or Claude Code (`.claude/skills/`).
- `REFERENCE.md` — every script's full CLI + exit codes.
- `EXAMPLES.md` — full KERMT walkthrough (real chemistry MAE optimisation).
- `templates/project.yaml` — starter `meta_info/project.yaml` for new projects.
