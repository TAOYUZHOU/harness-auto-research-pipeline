"""HARP web UI — FastAPI backend.

Runs alongside the cron loop; never owns the workspace, only reads
files + delegates writes through the same scripts the user would type.

Endpoints (all under /api unless noted):
  GET  /                       → index.html (SPA)
  GET  /api/status             → harp_status --json (cached 5 s)
  GET  /api/files/{kind}       → markdown content (kind = log|memory|plan)
  GET  /api/files/{kind}/zh    → polished zh markdown
  GET  /api/config/meta        → meta_info/project.yaml (text)
  PUT  /api/config/meta        → write meta_info/project.yaml (validates yaml)
  GET  /api/config/userprompt  → B/userprompt.yaml (text)
  PUT  /api/config/userprompt  → write B/userprompt.yaml (validates yaml)
  POST /api/actions/polish     → run harp_polish.sh --once (SSE output)
  POST /api/actions/doctor     → run harp_doctor.sh (SSE output)
  POST /api/actions/tick       → run one poll_tick.py (SSE output)
  GET  /api/usage              → parsed usage.jsonl (per-cycle + totals)
  GET  /api/tail/{name}        → SSE tail of tick.log / nohup_train.log
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, StreamingResponse)
from pydantic import BaseModel

# ─────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────
ENGINE_DIR = Path(os.environ["HARP_ENGINE_DIR"])
SKILL_DIR = Path(os.environ["HARP_SKILL_DIR"])
WEB_DIR = SKILL_DIR / "web"
META_FILE = ENGINE_DIR / "meta_info" / "project.yaml"


def workspace_dir() -> Path:
    """Resolve B from meta_info every time — meta might be edited live."""
    if not META_FILE.exists():
        raise HTTPException(500, "meta_info/project.yaml not found — run harp_init.sh")
    cfg = yaml.safe_load(META_FILE.read_text())
    try:
        return Path(cfg["harness"]["workspace"]["dir"])
    except (KeyError, TypeError):
        raise HTTPException(500, "meta_info: harness.workspace.dir missing")


# ─────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="HARP web UI", docs_url="/api/docs", redoc_url=None)


# ─────────────────────────────────────────────────────────────────────
# /
# ─────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "templates" / "index.html").read_text())


# ─────────────────────────────────────────────────────────────────────
# /api/status — cached 5 s to avoid hammering ps/git on every poll
# ─────────────────────────────────────────────────────────────────────
_STATUS_CACHE: dict = {"ts": 0.0, "data": None}


@app.get("/api/status")
async def status_json() -> JSONResponse:
    now = time.time()
    if now - _STATUS_CACHE["ts"] < 5.0 and _STATUS_CACHE["data"] is not None:
        return JSONResponse(_STATUS_CACHE["data"])

    wd = workspace_dir()
    state = wd / ".state"
    harness_yaml = wd / "harness.yaml"

    def _read(p: Path, default: str = "") -> str:
        return p.read_text().strip() if p.exists() else default

    cfg = yaml.safe_load(harness_yaml.read_text()) if harness_yaml.exists() else {}
    target = (cfg.get("targets") or [{}])[0]

    cycle = _read(state / "cycle_count.txt", "0")
    active = _read(state / "iteration_active", "false")
    best = _read(state / "best_metric.txt", "n/a")
    threshold = target.get("stop_threshold", "?")
    metric = target.get("primary_metric", "?")
    target_name = target.get("name", "?")
    target_repo = target.get("repo_path", "?")
    max_cycle = (cfg.get("schedule") or {}).get("max_cycle", "?")
    time_budget = int((cfg.get("schedule") or {}).get("train_time_budget_sec", 0))

    cron_installed = False
    try:
        out = subprocess.check_output(["crontab", "-l"], text=True,
                                      stderr=subprocess.DEVNULL)
        cron_installed = "harness-auto-research" in out
    except subprocess.CalledProcessError:
        pass

    inflight_runs = []
    inflight_file = state / "inflight_emit.json"
    if inflight_file.exists():
        try:
            inflight_runs = [
                {"anchor": k, **v} for k, v in
                json.loads(inflight_file.read_text()).items()
            ]
        except json.JSONDecodeError:
            pass

    train_pids = []
    try:
        out = subprocess.check_output(
            ["pgrep", "-af", r"python.*train.*\.py"], text=True,
            stderr=subprocess.DEVNULL)
        for line in out.strip().splitlines():
            if not line.strip():
                continue
            pid_str, _, cmd = line.partition(" ")
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            etime_out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "etimes="], text=True,
                stderr=subprocess.DEVNULL).strip()
            try:
                etime = int(etime_out)
            except ValueError:
                etime = 0
            train_pids.append({
                "pid": pid,
                "elapsed_sec": etime,
                "remaining_sec": max(0, time_budget - etime) if time_budget else None,
                "over_budget": time_budget > 0 and etime > time_budget,
                "cmd": cmd[:120],
            })
    except subprocess.CalledProcessError:
        pass

    tick_log = state / "tick.log"
    last_tick_age = None
    if tick_log.exists():
        last_tick_age = int(time.time() - tick_log.stat().st_mtime)

    remote = None
    try:
        url = subprocess.check_output(
            ["git", "-C", str(wd), "remote", "get-url", "origin"],
            text=True, stderr=subprocess.DEVNULL).strip()
        unpushed = subprocess.check_output(
            ["git", "-C", str(wd), "rev-list", "--count", "@{u}..HEAD"],
            text=True, stderr=subprocess.DEVNULL).strip()
        remote = {"url": url, "unpushed": int(unpushed)}
    except subprocess.CalledProcessError:
        pass

    data = {
        "workspace": str(wd),
        "target": {"name": target_name, "repo": target_repo, "metric": metric},
        "iteration": {
            "active": active == "true",
            "cycle": int(cycle) if cycle.isdigit() else cycle,
            "max_cycle": max_cycle,
            "best": best,
            "stop_threshold": threshold,
            "time_budget_sec": time_budget,
        },
        "cron_installed": cron_installed,
        "remote": remote,
        "last_tick_age_sec": last_tick_age,
        "training_processes": train_pids,
        "inflight_runs": inflight_runs,
        "ts": int(time.time()),
    }
    _STATUS_CACHE.update({"ts": now, "data": data})
    return JSONResponse(data)


# ─────────────────────────────────────────────────────────────────────
# /api/files/{kind}  + /zh
# ─────────────────────────────────────────────────────────────────────
_FILE_KIND = {"log", "memory", "plan", "program"}


@app.get("/api/files/{kind}", response_class=PlainTextResponse)
async def get_file(kind: str) -> PlainTextResponse:
    if kind not in _FILE_KIND:
        raise HTTPException(404, f"unknown kind: {kind}")
    p = workspace_dir() / f"{kind}.md"
    if not p.exists():
        return PlainTextResponse(f"(no {kind}.md yet)", status_code=200)
    return PlainTextResponse(p.read_text())


@app.get("/api/files/{kind}/zh", response_class=PlainTextResponse)
async def get_file_zh(kind: str) -> PlainTextResponse:
    if kind not in {"log", "memory", "plan"}:
        raise HTTPException(404, f"no zh for {kind}")
    p = workspace_dir() / ".state" / "zh" / f"{kind}.md.zh.md"
    if not p.exists():
        return PlainTextResponse(
            f"(no polished {kind} yet — run Actions ▸ Polish or start the daemon)",
            status_code=200)
    return PlainTextResponse(p.read_text())


# ─────────────────────────────────────────────────────────────────────
# /api/config — meta_info/project.yaml + B/userprompt.yaml (text edit)
# ─────────────────────────────────────────────────────────────────────
class ConfigPayload(BaseModel):
    text: str


@app.get("/api/config/meta", response_class=PlainTextResponse)
async def get_meta() -> PlainTextResponse:
    return PlainTextResponse(META_FILE.read_text() if META_FILE.exists() else "")


@app.put("/api/config/meta")
async def put_meta(payload: ConfigPayload) -> JSONResponse:
    try:
        yaml.safe_load(payload.text)
    except yaml.YAMLError as e:
        raise HTTPException(400, f"invalid YAML: {e}")
    META_FILE.write_text(payload.text)
    return JSONResponse({"ok": True, "bytes": len(payload.text),
                         "warning": "meta_info/project.yaml updated. "
                                    "Note: this only affects future workspaces — "
                                    "existing B/harness.yaml is NOT auto-re-rendered."})


@app.get("/api/config/userprompt", response_class=PlainTextResponse)
async def get_userprompt() -> PlainTextResponse:
    p = workspace_dir() / "userprompt.yaml"
    return PlainTextResponse(p.read_text() if p.exists() else "")


@app.put("/api/config/userprompt")
async def put_userprompt(payload: ConfigPayload) -> JSONResponse:
    try:
        yaml.safe_load(payload.text)
    except yaml.YAMLError as e:
        raise HTTPException(400, f"invalid YAML: {e}")
    p = workspace_dir() / "userprompt.yaml"
    p.write_text(payload.text)
    # Force re-sync on next tick by invalidating the hash file.
    sha_file = workspace_dir() / ".state" / "userprompt.sha256"
    if sha_file.exists():
        sha_file.unlink()
    return JSONResponse({"ok": True, "bytes": len(payload.text),
                         "info": "userprompt.yaml updated; .state/userprompt.sha256 "
                                 "cleared to trigger PROGRAM SYNC on next tick."})


# ─────────────────────────────────────────────────────────────────────
# /api/usage — parse usage.jsonl
# ─────────────────────────────────────────────────────────────────────
@app.get("/api/usage")
async def usage() -> JSONResponse:
    p = workspace_dir() / ".state" / "usage.jsonl"
    if not p.exists():
        return JSONResponse({"records": [], "totals": {}, "by_mode": {}})

    records = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    totals = defaultdict(int)
    by_mode = defaultdict(lambda: defaultdict(int))
    for r in records:
        for k in ("input_tokens", "output_tokens",
                  "cache_read_tokens", "cache_write_tokens"):
            v = int(r.get(k, 0) or 0)
            totals[k] += v
            by_mode[r.get("mode", "?")][k] += v
        totals["calls"] += 1
        by_mode[r.get("mode", "?")]["calls"] += 1

    return JSONResponse({
        "records": records[-50:],  # tail
        "totals": dict(totals),
        "by_mode": {k: dict(v) for k, v in by_mode.items()},
        "n_records": len(records),
    })


# ─────────────────────────────────────────────────────────────────────
# /api/actions/* — run a script, stream output as SSE
# ─────────────────────────────────────────────────────────────────────
ACTION_LOCK = asyncio.Lock()


async def _stream_subprocess(cmd: list[str]) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted lines from a subprocess in real time."""
    yield f"event: start\ndata: {json.dumps({'cmd': cmd, 'ts': time.time()})}\n\n"
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ})
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            yield f"data: {json.dumps({'line': line})}\n\n"
        rc = await proc.wait()
        yield f"event: end\ndata: {json.dumps({'rc': rc})}\n\n"
    except Exception as e:
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"


def _action_response(cmd: list[str]) -> StreamingResponse:
    async def gen():
        # serialise actions globally so two clicks don't collide
        if ACTION_LOCK.locked():
            yield f"event: error\ndata: {json.dumps({'error': 'another action is running'})}\n\n"
            return
        async with ACTION_LOCK:
            async for chunk in _stream_subprocess(cmd):
                yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/actions/polish")
async def action_polish() -> StreamingResponse:
    return _action_response(["bash", str(SKILL_DIR / "scripts" / "harp_polish.sh"),
                             "--once"])


@app.post("/api/actions/polish_force")
async def action_polish_force() -> StreamingResponse:
    return _action_response(["bash", str(SKILL_DIR / "scripts" / "harp_polish.sh"),
                             "--once", "--force"])


@app.post("/api/actions/doctor")
async def action_doctor() -> StreamingResponse:
    return _action_response(["bash", str(SKILL_DIR / "scripts" / "harp_doctor.sh")])


@app.post("/api/actions/tick")
async def action_tick() -> StreamingResponse:
    cmd = ["bash", "-lc",
           f"cd {ENGINE_DIR} && source env.sh && python3 scripts/poll_tick.py"]
    return _action_response(cmd)


# ─────────────────────────────────────────────────────────────────────
# /api/tail/{name} — SSE tail -F a specific file
# ─────────────────────────────────────────────────────────────────────
_TAIL_TARGETS = {
    "tick": lambda wd: wd / ".state" / "tick.log",
    "polish_daemon": lambda wd: wd / ".state" / "polish_daemon.log",
}


@app.get("/api/tail/{name}")
async def tail(name: str, request: Request) -> StreamingResponse:
    if name not in _TAIL_TARGETS:
        raise HTTPException(404, f"unknown tail target: {name}")
    path: Path = _TAIL_TARGETS[name](workspace_dir())

    async def gen():
        # send last 50 lines, then stream new content as it arrives
        offset = 0
        if path.exists():
            text = path.read_text()
            tail_lines = text.splitlines()[-50:]
            for ln in tail_lines:
                yield f"data: {json.dumps({'line': ln})}\n\n"
            offset = len(text.encode("utf-8"))

        while True:
            if await request.is_disconnected():
                return
            if not path.exists():
                await asyncio.sleep(2)
                continue
            sz = path.stat().st_size
            if sz < offset:
                # file truncated/rotated — restart
                offset = 0
            if sz > offset:
                with path.open("rb") as f:
                    f.seek(offset)
                    chunk = f.read(sz - offset).decode("utf-8", errors="replace")
                    offset = sz
                    for ln in chunk.splitlines():
                        yield f"data: {json.dumps({'line': ln})}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ─────────────────────────────────────────────────────────────────────
# /api/health
# ─────────────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "engine": str(ENGINE_DIR),
            "workspace": str(workspace_dir())}
