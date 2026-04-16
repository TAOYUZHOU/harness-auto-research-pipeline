#!/usr/bin/env python3
"""
Auto-Research Service — single tick orchestrator.

Called by cron (or manually).  Each invocation:
  1. Acquires flock (skip if another tick is running)
  2. Scans RESULT_ROOT for new/updated training logs
  3. Parses metrics, maps to plan anchors
  4. Git: commit good results, reset bad ones
  5. Appends summaries to log.md
  6. Optionally invokes agent to propose next experiment
  7. Checks stop conditions -> disables cron if met
"""

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parse_log import parse_training_log, RunResult

SERVICE_ROOT = Path(os.environ.get("SERVICE_ROOT",
                                    str(Path(__file__).resolve().parent.parent)))
RESULT_ROOT = Path(os.environ.get("RESULT_ROOT", ""))
LOG_GLOB = os.environ.get("LOG_GLOB", "nohup_train.log")
PRIMARY_METRIC_OP = os.environ.get("PRIMARY_METRIC_OP", "lt")
GLOBAL_STOP_THRESHOLD = float(os.environ.get("GLOBAL_STOP_THRESHOLD", "0.04"))
AGENT_BIN = os.environ.get("AGENT_BIN", "agent")
AGENT_MAX_LOG_LINES = int(os.environ.get("AGENT_MAX_LOG_LINES", "50"))
AGENT_TIMEOUT_SEC = int(os.environ.get("AGENT_TIMEOUT_SEC", "300"))
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("MAX_CONSECUTIVE_FAILURES", "5"))
TRAIN_TIME_BUDGET_SEC = int(os.environ.get("TRAIN_TIME_BUDGET_SEC", "0"))
GIT_EXPERIMENT_MGMT = os.environ.get("GIT_EXPERIMENT_MGMT", "false").lower() == "true"
BEST_METRIC_FILE = Path(os.environ.get("BEST_METRIC_FILE",
                                        str(SERVICE_ROOT / ".state" / "best_metric.txt")))

STATE_DIR = SERVICE_ROOT / ".state"
SCAN_FILE = STATE_DIR / "last_scan.json"
ACTIVE_FILE = STATE_DIR / "iteration_active"
LOCKFILE = Path(os.environ.get("LOCKFILE", str(STATE_DIR / "tick.lock")))

PLAN_FILE = SERVICE_ROOT / "plan.md"
LOG_FILE = SERVICE_ROOT / "log.md"
PROGRAM_FILE = SERVICE_ROOT / "program.md"


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def is_active() -> bool:
    if not ACTIVE_FILE.exists():
        return False
    return ACTIVE_FILE.read_text().strip().lower() == "true"


def set_active(val: bool):
    ACTIVE_FILE.write_text("true" if val else "false")


def load_scan_state() -> dict:
    if SCAN_FILE.exists():
        try:
            return json.loads(SCAN_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_scan_state(state: dict):
    SCAN_FILE.write_text(json.dumps(state, indent=2))


def discover_logs() -> list[Path]:
    logs = []
    for dirpath, _, filenames in os.walk(RESULT_ROOT):
        for fn in filenames:
            if fn == LOG_GLOB:
                logs.append(Path(dirpath) / fn)
    return sorted(logs)


def find_new_logs(scan_state: dict) -> list[Path]:
    new = []
    for logp in discover_logs():
        key = str(logp)
        mtime = logp.stat().st_mtime
        size = logp.stat().st_size
        prev = scan_state.get(key, {})
        if mtime > prev.get("mtime", 0) or size > prev.get("size", 0):
            new.append(logp)
    return new


def update_scan_state(scan_state: dict, paths: list[Path]):
    for p in paths:
        s = p.stat()
        scan_state[str(p)] = {"mtime": s.st_mtime, "size": s.st_size}


def load_plan_anchors() -> dict[str, dict]:
    anchors: dict[str, dict] = {}
    if not PLAN_FILE.exists():
        return anchors

    text = PLAN_FILE.read_text()
    blocks = re.split(r"^---\s*$", text, flags=re.MULTILINE)
    plan_id_re = re.compile(r"###\s*PLAN_ID:\s*(\S+)")

    for block in blocks:
        pid_m = plan_id_re.search(block)
        if not pid_m:
            continue
        pid = pid_m.group(1)
        info: dict = {"plan_id": pid}
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("anchor:"):
                info["anchor"] = line.split(":", 1)[1].strip()
            elif line.startswith("axis:"):
                info["axis"] = line.split(":", 1)[1].strip()
            elif line.startswith("status:"):
                info["status"] = line.split(":", 1)[1].strip()
            elif line.startswith("metric:"):
                info.setdefault("expect", {})["metric"] = line.split(":", 1)[1].strip()
            elif line.startswith("threshold:"):
                try:
                    info.setdefault("expect", {})["threshold"] = float(
                        line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("op:"):
                info.setdefault("expect", {})["op"] = line.split(":", 1)[1].strip()

        if "anchor" in info:
            anchors[info["anchor"]] = info
    return anchors


def map_result_to_plan(result: RunResult, anchors: dict) -> dict | None:
    return anchors.get(result.anchor)


# ── Git experiment management ──

def _git(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        ["git"] + cmd,
        capture_output=True, text=True,
        cwd=cwd or str(SERVICE_ROOT),
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def load_best_metric() -> float:
    if BEST_METRIC_FILE.exists():
        try:
            return float(BEST_METRIC_FILE.read_text().strip())
        except ValueError:
            pass
    return float("inf") if PRIMARY_METRIC_OP == "lt" else float("-inf")


def save_best_metric(val: float):
    BEST_METRIC_FILE.write_text(str(val))


def is_improvement(new_val: float, best_val: float) -> bool:
    if PRIMARY_METRIC_OP == "lt":
        return new_val < best_val
    return new_val > best_val


def git_keep(anchor: str):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"exp/{anchor}/{ts}"
    _git(["tag", tag])
    print(f"  [git] kept commit, tagged {tag}")


def git_discard():
    rc, out = _git(["rev-parse", "HEAD"])
    if rc != 0:
        return
    rc, out = _git(["log", "--oneline", "-1"])
    print(f"  [git] discarding: {out}")
    _git(["reset", "--hard", "HEAD~1"])


# ── Training time budget ──

def check_and_kill_overtime_training():
    """Kill training processes exceeding TRAIN_TIME_BUDGET_SEC."""
    if TRAIN_TIME_BUDGET_SEC <= 0:
        return

    try:
        ps_out = subprocess.check_output(
            ["ps", "-eo", "pid,etimes,args"], text=True
        )
    except subprocess.CalledProcessError:
        return

    for line in ps_out.strip().splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, elapsed_s, cmd = parts
        if "train" not in cmd or "python" not in cmd:
            continue
        if "nohup_train.log" not in cmd and "train.py" not in cmd:
            continue
        try:
            elapsed = int(elapsed_s)
            pid = int(pid_s)
        except ValueError:
            continue
        if elapsed > TRAIN_TIME_BUDGET_SEC:
            print(f"  [budget] killing PID {pid} (elapsed {elapsed}s > "
                  f"budget {TRAIN_TIME_BUDGET_SEC}s): {cmd[:80]}")
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(2)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# ── Log append ──

def append_log_line(result: RunResult, plan: dict | None,
                    git_action: str = "N/A"):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    plan_id = plan["plan_id"] if plan else "?"
    axis = plan.get("axis", "?") if plan else "?"
    status = "ok"

    if not result.is_valid:
        status = "crash" if result.error else "unmapped"
    elif plan and "expect" in plan:
        exp = plan["expect"]
        threshold = exp.get("threshold", float("inf"))
        op = exp.get("op", "lt")
        val = result.test_mae or float("inf")
        if op == "lt" and val >= threshold:
            status = "below_expect"
        elif op == "gt" and val <= threshold:
            status = "below_expect"

    line = (
        f"TS={ts};PLAN={plan_id};ANCHOR={result.anchor};"
        f"AXIS={axis};TEST_MAE={result.test_mae or 'N/A'};"
        f"BEST_VAL_MAE={result.best_val_mae or 'N/A'};"
        f"STATUS={status};GIT={git_action};HP={result.hp_summary()}"
    )
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    return status


def check_global_stop(results: list[RunResult]) -> bool:
    for r in results:
        if r.test_mae is not None:
            if PRIMARY_METRIC_OP == "lt" and r.test_mae < GLOBAL_STOP_THRESHOLD:
                return True
            if PRIMARY_METRIC_OP == "gt" and r.test_mae > GLOBAL_STOP_THRESHOLD:
                return True
    return False


def disable_cron():
    try:
        raw = subprocess.check_output(["crontab", "-l"],
                                      stderr=subprocess.DEVNULL, text=True)
    except subprocess.CalledProcessError:
        return

    tag = "# auto-research-service"
    lines = [l for l in raw.splitlines() if tag not in l]
    proc = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                          text=True, capture_output=True)
    if proc.returncode == 0:
        print("[tick] cron entry removed")


def build_agent_prompt(new_results: list[tuple[RunResult, dict | None]]) -> str:
    program_text = PROGRAM_FILE.read_text() if PROGRAM_FILE.exists() else ""
    plan_text = PLAN_FILE.read_text() if PLAN_FILE.exists() else ""

    log_lines = []
    if LOG_FILE.exists():
        all_lines = [l for l in LOG_FILE.read_text().splitlines()
                     if l.strip() and not l.startswith("#")]
        log_lines = all_lines[-AGENT_MAX_LOG_LINES:]

    results_summary = []
    for r, p in new_results:
        pid = p["plan_id"] if p else "unmapped"
        results_summary.append(
            f"  - {r.anchor}: test_mae={r.test_mae}, "
            f"best_val_mae={r.best_val_mae}, plan={pid}"
        )

    prompt = f"""You are the auto-research agent.  Read the constraints below
and decide the next action.

=== program.md (CONSTRAINTS — MUST OBEY) ===
{program_text}

=== plan.md (current plans) ===
{plan_text}

=== log.md (recent {len(log_lines)} lines) ===
{chr(10).join(log_lines)}

=== NEW RESULTS THIS TICK ===
{chr(10).join(results_summary)}

Based on the above:
1. Update plan.md if needed (add new plan with orthogonal axis, or mark
   completed).
2. If a promising next experiment is clear, create its config YAML and/or
   edit code inside AGENT-EDITABLE blocks, then git commit and start training.
3. If global stop threshold is met, output the line: STOP_ITERATION=1
4. Apply the simplicity criterion: prefer fewer changes for equal gain.
5. Summarize your reasoning and action in 2-3 sentences.
"""
    return prompt


def invoke_agent(prompt: str) -> tuple[bool, str]:
    if not _agent_available():
        prompt_path = STATE_DIR / "last_prompt.txt"
        prompt_path.write_text(prompt)
        print(f"[tick] agent not available; prompt saved to {prompt_path}")
        return False, "[dry-run]"

    try:
        proc = subprocess.run(
            [AGENT_BIN, "-p", "--force", prompt],
            capture_output=True, text=True,
            timeout=AGENT_TIMEOUT_SEC,
            cwd=str(SERVICE_ROOT),
        )
        output = proc.stdout + proc.stderr
        stop = "STOP_ITERATION=1" in output
        return stop, output
    except subprocess.TimeoutExpired:
        print("[tick] agent timed out")
        return False, "[timeout]"
    except Exception as e:
        print(f"[tick] agent error: {e}")
        return False, str(e)


def _agent_available() -> bool:
    try:
        subprocess.run([AGENT_BIN, "--version"],
                       capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def run_tick():
    ensure_state_dir()

    if not is_active():
        print("[tick] iteration_active is false — skipping")
        return

    if not RESULT_ROOT.is_dir():
        print(f"[tick] RESULT_ROOT not found: {RESULT_ROOT}")
        return

    check_and_kill_overtime_training()

    scan_state = load_scan_state()
    new_logs = find_new_logs(scan_state)

    if not new_logs:
        print("[tick] no new logs found")
        return

    print(f"[tick] found {len(new_logs)} new/updated log(s)")
    anchors = load_plan_anchors()
    best_metric = load_best_metric()

    new_results: list[tuple[RunResult, dict | None]] = []
    for logp in new_logs:
        result = parse_training_log(str(logp))
        if not result.is_complete:
            print(f"  [skip] {result.anchor}: training not complete yet")
            continue

        plan = map_result_to_plan(result, anchors)
        git_action = "N/A"

        if GIT_EXPERIMENT_MGMT and result.test_mae is not None:
            if is_improvement(result.test_mae, best_metric):
                git_action = "keep"
                git_keep(result.anchor)
                save_best_metric(result.test_mae)
                best_metric = result.test_mae
                print(f"  [git] NEW BEST: {result.test_mae}")
            else:
                git_action = "discard"
                print(f"  [git] no improvement ({result.test_mae} vs best {best_metric})")

        status = append_log_line(result, plan, git_action)
        new_results.append((result, plan))
        pid = plan["plan_id"] if plan else "unmapped"
        print(f"  [{status}] {result.anchor} -> plan={pid} "
              f"test_mae={result.test_mae} git={git_action}")

    update_scan_state(scan_state, new_logs)
    save_scan_state(scan_state)

    if not new_results:
        print("[tick] no completed runs to process")
        return

    all_results = [r for r, _ in new_results]
    if check_global_stop(all_results):
        print("[tick] *** GLOBAL STOP THRESHOLD MET ***")
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        with open(LOG_FILE, "a") as f:
            f.write(f"TS={ts};EVENT=iteration_stopped;REASON=threshold_met\n")
        set_active(False)
        disable_cron()
        return

    prompt = build_agent_prompt(new_results)
    stop_requested, agent_output = invoke_agent(prompt)

    if agent_output and agent_output != "[dry-run]":
        agent_log = STATE_DIR / "last_agent_output.txt"
        agent_log.write_text(agent_output)
        print(f"[tick] agent output saved to {agent_log}")

    if stop_requested:
        print("[tick] agent requested stop")
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        with open(LOG_FILE, "a") as f:
            f.write(f"TS={ts};EVENT=iteration_stopped;REASON=agent_requested\n")
        set_active(False)
        disable_cron()


def main():
    ensure_state_dir()
    LOCKFILE.parent.mkdir(parents=True, exist_ok=True)

    lock_fd = open(LOCKFILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[tick] another tick is running — exiting")
        return

    try:
        run_tick()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
