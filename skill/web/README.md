# HARP web UI

Single-page browser app that wraps the same scripts as the skill, so
nothing here owns state — `app.py` is a thin FastAPI layer over file
reads + `subprocess` calls into `skill/scripts/*.sh`. Safe to start /
stop at any time without disturbing the cron loop.

## Quick start

```bash
bash skill/scripts/harp_web.sh
# first run: ~5 s pip install of fastapi/uvicorn (per-user, ~/.local)
# then:      http://127.0.0.1:8765
```

Custom port / host:

```bash
PORT=9000 bash skill/scripts/harp_web.sh
HOST=0.0.0.0 bash skill/scripts/harp_web.sh   # CAUTION: no auth
```

## What it shows

| Tab | Reads | Writes |
|------|--------|--------|
| **Dashboard** | `B/.state/*`, `B/harness.yaml`, `crontab`, `ps`, `git` | nothing |
| **Config** | `meta_info/project.yaml`, `B/userprompt.yaml` | the same files (with YAML validation; userprompt save also clears `.state/userprompt.sha256` so the next tick re-syncs) |
| **Logs** | `B/{log,memory,plan,program}.md`, `B/.state/zh/*.zh.md` | nothing |
| **Actions** | live SSE from `harp_polish.sh`, `harp_doctor.sh`, `poll_tick.py` | only what those scripts write |
| **Usage** | `B/.state/usage.jsonl` | nothing |

The Actions tab also has a "live tail" of `tick.log` via SSE, so you
can watch cron ticks roll in.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Browser (Tailwind + Alpine.js + HTMX, CDN-only)    │
│  - tabs render via fetch() against /api/*           │
│  - SSE for action output + log tailing              │
└──────────────────┬──────────────────────────────────┘
                   │ HTTP
┌──────────────────▼──────────────────────────────────┐
│  uvicorn  +  FastAPI  (skill/web/app.py)            │
│  - /api/status      cached 5 s                      │
│  - /api/config/*    YAML-validated PUT              │
│  - /api/files/*     plain-text reads                │
│  - /api/actions/*   subprocess + SSE wrapper        │
│  - /api/usage       parses .state/usage.jsonl       │
│  - /api/tail/*      poll-based file tailing         │
└──────────────────┬──────────────────────────────────┘
                   │ subprocess + file I/O
┌──────────────────▼──────────────────────────────────┐
│  skill/scripts/*.sh   +   workspace B files         │
└─────────────────────────────────────────────────────┘
```

## Security

The default bind is `127.0.0.1` — **no auth layer** is provided. If you
need remote access, do it via SSH port-forward:

```bash
# on your laptop
ssh -L 8765:127.0.0.1:8765 your-remote-box
# then open http://localhost:8765 in your laptop browser
```

Do NOT expose this on a public network — anyone who reaches the port
can run `tick`, edit `meta_info`, and trigger arbitrary `cursor-agent`
calls (= burns your token quota).

## Endpoint reference

See `/api/docs` (auto-generated FastAPI Swagger UI) when the server is
running, or the docstring at the top of `app.py`.

## Stopping

```bash
# foreground: Ctrl-C
# background:
pkill -f "uvicorn app:app"
```

## Files

```
skill/web/
├── README.md            # this file
├── app.py               # FastAPI backend (single file, ~280 LOC)
└── templates/
    └── index.html       # SPA (single file, no build step)
```
