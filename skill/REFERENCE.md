# HARP skill — script reference

Every script is in `skill/scripts/` and is a standalone bash file.
They all resolve the workspace path from `<engine>/meta_info/project.yaml`
(via `harness.workspace.dir`), so you don't need to pass paths manually.

---

## `harp_init.sh` — bootstrap a new project

Interactive wizard. Asks 4 questions, renders `meta_info/project.yaml`
from `skill/templates/project.yaml`, then runs `init_workspace.sh`,
`quickstart.sh`, and `install_cron.sh install`.

**Flags:**
- `--force` — overwrite existing `meta_info/project.yaml`.
- `--no-cron` — skip cron install (you can run it manually later).
- `-h`, `--help` — show usage.

**Exit codes:** 0 = success, 1 = `project.yaml` exists (use `--force`),
2 = bad arg.

**Idempotency:** safe to re-run with `--force` to start over.

---

## `harp_status.sh` — one-screen dashboard

Read-only. Queries `meta_info`, `B/harness.yaml`, `B/.state/*`, `crontab`,
`ps`, and `git` to assemble a snapshot.

**Flags:**
- *(no args)* — coloured text dashboard
- `--json` — same data as JSON to stdout (for piping/automation)
- `--watch` — re-render every 30 s until Ctrl-C
- `-h`, `--help`

**Output sections:** workspace, target, cycle/best/threshold, cron status,
remote sync state, last tick timestamp, in-flight training (PIDs +
elapsed), in-flight metric snapshot from `inflight_emit.json`, token
usage from `usage_summary.txt`, last 3 lines of `log.md`.

---

## `harp_polish.sh` — translate to Chinese

Reads `B/{log,memory,plan}.md`, polishes via a **fresh** `cursor-agent`
chat (no `--resume`), writes to `B/.state/zh/{log,memory,plan}.md.zh.md`.

Mtime-cached: a SHA256 of the source is stored in
`.state/zh/.{kind}.src_sha256`. Same content → no agent call.

**Flags:**
- `--once` *(required)* — polish then exit (use the daemon for continuous)
- `--file log|memory|plan|all` — pick which (default `all`)
- `--force` — ignore SHA cache, re-polish even if unchanged
- `--dry-run` — print prompt, don't call agent
- `-h`, `--help`

**Cost accounting:** every polish call appends a record to
`B/.state/usage.jsonl` with `mode=polish_zh_<kind>`, mirroring how
iteration ticks are tracked. `usage_summary.txt` shows aggregate.

**Output format:** UTF-8 Markdown with HTML comment header naming the
source file and polish timestamp. Original `*.md` files are never
modified.

---

## `harp_polish_daemon.sh` — background polish watcher

Runs `harp_polish.sh --once` whenever any of log/memory/plan changes.

**Env vars:**
- `POLL_SEC` (default 60) — file-change check interval

**Logs:** `B/.state/polish_daemon.log`

**Single-instance:** held by `B/.state/polish_daemon.lock` via `flock`.

**Recommended invocation:**
```bash
nohup bash skill/scripts/harp_polish_daemon.sh > /tmp/harp_polish.log 2>&1 &
disown
```

To stop: `pkill -f harp_polish_daemon.sh`

---

## `harp_doctor.sh` — health check

10 diagnostic checks. Each prints `✓` (pass), `✗` (fail), or `!` (warn).

**Checks:**

| # | What | Severity if missing |
|---|------|---------------------|
| 1 | `cursor-agent` in PATH and responds to `--version` | FAIL |
| 2 | `meta_info/project.yaml` exists and parses | FAIL |
| 3 | Workspace dir exists, is git repo, has `harness.yaml` | FAIL |
| 4 | Cron line installed for this engine | WARN |
| 5 | `tick.log` written within last 30 min | WARN |
| 6 | Target repo D exists, is git repo | FAIL |
| 7 | `program_constitution.sha256` matches current `program.md` | FAIL |
| 8 | Workspace remote reachable (if `mode != none`) | WARN |
| 9 | Disk free > 1 GB on workspace mount | WARN |
| 10 | Python `yaml` module available | FAIL |

**Exit code:** number of FAILs (0 = all good).

---

## Files this skill writes inside the workspace

| Path | Producer | Format |
|------|----------|--------|
| `B/.state/zh/log.md.zh.md` | `harp_polish.sh` | Markdown (zh) |
| `B/.state/zh/memory.md.zh.md` | `harp_polish.sh` | Markdown (zh) |
| `B/.state/zh/plan.md.zh.md` | `harp_polish.sh` | Markdown (zh) |
| `B/.state/zh/.{kind}.src_sha256` | `harp_polish.sh` | sha256 cache |
| `B/.state/zh/.{kind}.last_stream.jsonl` | `harp_polish.sh` | raw cursor-agent stream (debugging) |
| `B/.state/polish_daemon.log` | `harp_polish_daemon.sh` | text log |
| `B/.state/polish_daemon.lock` | `harp_polish_daemon.sh` | flock |

The engine `poll_tick.py` does NOT touch any of these — they live
under `.state/zh/` and `.state/polish_daemon.*` which are outside the
engine's scan paths.

---

---

## `harp_web.sh` — FastAPI web UI

Launches a single-page web app that wraps every other script in this
skill. Auto-installs `fastapi` + `uvicorn` + `pyyaml` to `~/.local`
on first run.

**Env vars:**
- `PORT` (default `8765`)
- `HOST` (default `127.0.0.1`; **never** set to `0.0.0.0` on a public network — no auth)

**Flags:**
- `--install` — only install dependencies, don't start the server
- `-h`, `--help`

**Pages:** Dashboard, Config (edit `meta_info` + `userprompt` in
browser with YAML validation), Logs (raw + Chinese polish side-by-side),
Actions (run polish/doctor/tick with live SSE output + `tick.log` tail),
Usage (token cost dashboard).

**Endpoints:** see `/api/docs` (auto-generated Swagger UI) or
`skill/web/README.md`.

**Recommended remote-machine workflow:**
```bash
# on the remote box
nohup bash skill/scripts/harp_web.sh > /tmp/harp_web.log 2>&1 &
# on your laptop
ssh -L 8765:127.0.0.1:8765 user@remote
# then open http://localhost:8765 in your laptop browser
```

---

## Composition patterns

### Continuous monitoring on a remote SSH box

```bash
# Four things keep running independently:
bash skill/scripts/install_cron.sh install                       # part of init
nohup bash skill/scripts/harp_polish_daemon.sh > /dev/null 2>&1 &
nohup bash skill/scripts/harp_web.sh > /tmp/harp_web.log 2>&1 &  # web UI
# Then on laptop: ssh -L 8765:127.0.0.1:8765 remote-box
```

### Quick manual check between SSH sessions

```bash
bash skill/scripts/harp_status.sh && bash skill/scripts/harp_doctor.sh
```

### One-off catch-up after coming back to a long-running iteration

```bash
bash skill/scripts/harp_polish.sh --once --force   # refresh all 3 zh files
less /workspace/.state/zh/log.md.zh.md
```
