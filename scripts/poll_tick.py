#!/usr/bin/env python3
"""
Auto-Research Service — single tick orchestrator.

Architecture:
  SERVICE_ROOT  = engine repo (read-only). Contains scripts, program.md template,
                  and meta_info/project.yaml (used ONLY at workspace init).
  WORK_DIR      = persistent workspace (repo B). Agent runs here.
                  Owns harness.yaml + userprompt.yaml + plan.md/log.md/.state/.
  harness.yaml  = WORK_DIR/harness.yaml is the SINGLE source of truth at runtime.
                  SERVICE_ROOT no longer has its own harness.yaml.

Config resolution:
  1. SERVICE_ROOT  ← location of this script's parent (or $SERVICE_ROOT env).
  2. WORK_DIR      ← $WORK_DIR env, else SERVICE_ROOT/meta_info/project.yaml
                     ::harness.workspace.dir (the bootstrap pointer).
  3. HARNESS       ← WORK_DIR/harness.yaml (rendered by init_workspace.sh from
                     meta_info, owned by B thereafter).

Called by cron (or manually).  Each invocation:
  1. Acquires flock on WORK_DIR/.state/tick.lock
  2. Ensures GitNexus index is fresh for each target repo
  3. Scans RESULT_ROOT for new/updated training logs
  4. Parses metrics, maps to plan anchors
  5. Git: tag good results, reset bad ones (in WORK_DIR)
  6. Appends summaries to WORK_DIR/log.md
  7. Invokes agent with --workspace WORK_DIR --approve-mcps
  8. Checks stop conditions -> disables cron if met
"""

import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("[FATAL] PyYAML not installed. Run: pip install pyyaml")

sys.path.insert(0, str(Path(__file__).parent))
from parse_log import parse_training_log, RunResult

# ── Engine root (read-only repo A) ──
SERVICE_ROOT = Path(os.environ.get("SERVICE_ROOT",
                                    str(Path(__file__).resolve().parent.parent)))

# ── Bootstrap: resolve WORK_DIR (repo B) ──
# Order: explicit env var > meta_info/project.yaml::harness.workspace.dir.
# meta_info is the ONLY file in A that may carry a project-specific value;
# touching A/harness.yaml is intentionally not supported (it no longer exists).
_meta_file = SERVICE_ROOT / "meta_info" / "project.yaml"
_wd_env = os.environ.get("WORK_DIR", "").strip()
if _wd_env:
    WORK_DIR = Path(_wd_env)
else:
    if not _meta_file.exists():
        sys.exit(
            f"[FATAL] WORK_DIR env not set and meta_info missing at {_meta_file}.\n"
            "        Either source env.sh, or run init_workspace.sh first.")
    with open(_meta_file) as _f:
        _meta = yaml.safe_load(_f) or {}
    _wd_meta = (((_meta.get("harness") or {}).get("workspace") or {})
                .get("dir", "") or "")
    if not _wd_meta or "<" in _wd_meta:
        sys.exit(
            f"[FATAL] WORK_DIR unresolved.  meta_info workspace.dir = "
            f"'{_wd_meta}' (placeholder?). Fill {_meta_file} or export WORK_DIR.")
    WORK_DIR = Path(_wd_meta)

# ── Runtime config: WORK_DIR/harness.yaml is the single source of truth ──
HARNESS_FILE = WORK_DIR / "harness.yaml"
if not HARNESS_FILE.exists():
    sys.exit(
        f"[FATAL] B/harness.yaml not found at {HARNESS_FILE}.\n"
        "        Run scripts/init_workspace.sh to render it from meta_info.")
with open(HARNESS_FILE) as f:
    HARNESS = yaml.safe_load(f)

# ── Derive runtime config from B/harness.yaml (env vars override if set) ──
_ws = HARNESS.get("workspace", {})
_agent = HARNESS.get("agent", {})
_sched = HARNESS.get("schedule", {})
_target = HARNESS.get("targets", [{}])[0]  # primary target
_tools = HARNESS.get("tools", {})

# Sanity check: B/harness.yaml's recorded workspace.dir should match WORK_DIR
# (both point to "this directory"). Mismatch => B was relocated since init;
# warn but trust the actual filesystem location, not the stale yaml field.
_yaml_wd = (_ws.get("dir") or "").strip()
if _yaml_wd and Path(_yaml_wd).resolve() != WORK_DIR.resolve():
    print(f"[WARN] B/harness.yaml::workspace.dir ({_yaml_wd}) does not match "
          f"actual WORK_DIR ({WORK_DIR}). Using actual location.",
          file=sys.stderr)

RESULT_ROOT = Path(os.environ.get("RESULT_ROOT", _target.get("result_path", "")))
LOG_GLOB = os.environ.get("LOG_GLOB", _target.get("log_glob", "nohup_train.log"))
PRIMARY_METRIC_OP = os.environ.get("PRIMARY_METRIC_OP", _target.get("metric_op", "lt"))
GLOBAL_STOP_THRESHOLD = float(os.environ.get(
    "GLOBAL_STOP_THRESHOLD", _target.get("stop_threshold", 0.04)))

AGENT_BIN = os.environ.get("AGENT_BIN", _agent.get("bin", "agent"))
AGENT_MODEL = os.environ.get("AGENT_MODEL", _agent.get("model", ""))
AGENT_MAX_LOG_LINES = int(os.environ.get(
    "AGENT_MAX_LOG_LINES", _agent.get("max_log_lines", 50)))
AGENT_TIMEOUT_SEC = int(os.environ.get(
    "AGENT_TIMEOUT_SEC", _agent.get("timeout_sec", 300)))
AGENT_FLAGS = _agent.get("flags", ["--trust"])

MAX_CONSECUTIVE_FAILURES = int(os.environ.get(
    "MAX_CONSECUTIVE_FAILURES", _sched.get("max_consecutive_failures", 5)))
TRAIN_TIME_BUDGET_SEC = int(os.environ.get(
    "TRAIN_TIME_BUDGET_SEC", _sched.get("train_time_budget_sec", 0)))
MAX_CYCLE = int(os.environ.get(
    "MAX_CYCLE", _sched.get("max_cycle", 0)))
STOP_PROTOCOL = os.environ.get(
    "STOP_PROTOCOL", _sched.get("stop_protocol", "graceful")).lower()
GIT_EXPERIMENT_MGMT = (
    os.environ.get("GIT_EXPERIMENT_MGMT", "").lower() == "true"
    or _ws.get("git_experiment_mgmt", False)
)
BEST_METRIC_FILE = Path(os.environ.get("BEST_METRIC_FILE",
                                        str(WORK_DIR / ".state" / "best_metric.txt")))

# GitNexus tool config
_gitnexus_cfg = _tools.get("gitnexus", {})
GITNEXUS_ENABLED = _gitnexus_cfg.get("enabled", False)
GITNEXUS_AUTO_REINDEX = _gitnexus_cfg.get("auto_reindex", True)
GITNEXUS_REINDEX_ON = _gitnexus_cfg.get("reindex_on", "git_head_change")
GITNEXUS_PACKAGE = _gitnexus_cfg.get("package", "gitnexus@1.3.11")
GITNEXUS_ANALYZE_TIMEOUT = int(_gitnexus_cfg.get("analyze_timeout_sec", 600))

# All target repos (for multi-target indexing). Includes the agent
# file-creation sandbox path so we can surface it in the agent prompt.
TARGET_REPOS = [
    {
        "name": t["name"],
        "repo_path": Path(t["repo_path"]),
        "agent_addition_dir": t.get("agent_addition_dir", "add_by_HARP"),
    }
    for t in HARNESS.get("targets", [])
    if "repo_path" in t
]

# Workspace paths (all mutable state lives here)
STATE_DIR = WORK_DIR / ".state"
SCAN_FILE = STATE_DIR / "last_scan.json"
ACTIVE_FILE = STATE_DIR / "iteration_active"
CYCLE_FILE = STATE_DIR / "cycle_count.txt"
USERPROMPT_HASH_FILE = STATE_DIR / "userprompt.sha256"
LOCKFILE = Path(os.environ.get("LOCKFILE", str(STATE_DIR / "tick.lock")))

PLAN_FILE = WORK_DIR / "plan.md"
LOG_FILE = WORK_DIR / "log.md"
MEMORY_FILE = WORK_DIR / "memory.md"
USERPROMPT_FILE = WORK_DIR / "userprompt.yaml"
WORKSPACE_PROGRAM_FILE = WORK_DIR / "program.md"
PENDING_MEMORY_FILE = STATE_DIR / "pending_memory.json"
CHECK_FILE = WORK_DIR / "check.md"
PREFLIGHT_OK_FILE = STATE_DIR / "preflight_ok"

AGENT_MAX_MEMORY_BLOCKS = int(os.environ.get(
    "AGENT_MAX_MEMORY_BLOCKS", _agent.get("memory_tail_blocks", 5)))

# Template paths (read-only)
PROGRAM_FILE = SERVICE_ROOT / "program.md"

# USER-INJECTED markers in program.md
USER_INJECTED_BEGIN = "<!-- USER-INJECTED-BEGIN -->"
USER_INJECTED_END = "<!-- USER-INJECTED-END -->"
USER_INJECTED_RE = re.compile(
    re.escape(USER_INJECTED_BEGIN) + r"(.*?)" + re.escape(USER_INJECTED_END),
    re.DOTALL,
)


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


# ── Git experiment management (operates on WORK_DIR) ──

def _git(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        ["git"] + cmd,
        capture_output=True, text=True,
        cwd=str(WORK_DIR),
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


def git_keep(anchor: str) -> str:
    """Commit + tag the kept experiment.  Returns the timestamp string
    used in the tag, so callers can build a matching memory.md EXP_ID."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"exp/{anchor}/{ts}"
    _git(["add", "-A"])
    _git(["commit", "-m", f"experiment result: {anchor} ({ts})"])
    _git(["tag", tag])
    print(f"  [git] kept commit, tagged {tag}")
    return ts


def git_discard():
    rc, out = _git(["log", "--oneline", "-1"])
    if rc != 0:
        return
    print(f"  [git] discarding: {out}")
    _git(["reset", "--hard", "HEAD~1"])


# ── In-flight log scanning (F3) ──

# Default regex captures KERMT's epoch-line format:
#   "Epoch: 0042 ...  mae_val: 0.0832 ..."
# Override per target via harness.yaml::targets[].inflight_pattern  (re_metric)
# and ::targets[].inflight_metric_name.
_INFLIGHT_DEFAULT_RE = (
    r"Epoch:\s*(?P<epoch>\d+)\b.*?mae_val:\s*(?P<metric>[0-9.]+)"
)


def _scan_inflight_log(path: Path, *, pattern: str, lower_is_better: bool
                       ) -> dict | None:
    """Parse an in-flight training log.  Returns None if the log has no
    epoch-line matches (training hasn't produced its first val epoch).
    Otherwise returns:
        {
          "anchor": <run-dir name>,
          "log": <abs path>,
          "epochs": <int>,           # number of validated epochs seen
          "last_epoch": <int>,
          "best": <float>,           # best metric so far
          "best_epoch": <int>,
          "plateau_epochs": <int>,   # epochs since best_epoch with |Δbest| < 0.005
          "mtime": <float>,          # log's last write time (epoch seconds)
        }"""
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    rx = re.compile(pattern)
    rows: list[tuple[int, float]] = []
    for m in rx.finditer(text):
        try:
            ep = int(m.group("epoch"))
            mv = float(m.group("metric"))
        except (ValueError, IndexError):
            continue
        rows.append((ep, mv))
    if not rows:
        return None
    last_ep, _ = rows[-1]
    if lower_is_better:
        best_idx = min(range(len(rows)), key=lambda i: rows[i][1])
    else:
        best_idx = max(range(len(rows)), key=lambda i: rows[i][1])
    best_ep, best_val = rows[best_idx]
    # plateau = epochs after best_idx with |delta vs best| < 0.005
    plateau = 0
    for ep, mv in rows[best_idx + 1:]:
        if abs(mv - best_val) < 0.005:
            plateau += 1
        else:
            plateau = 0
    return {
        "anchor": path.parent.name,
        "log": str(path),
        "epochs": len(rows),
        "last_epoch": last_ep,
        "best": round(best_val, 6),
        "best_epoch": best_ep,
        "plateau_epochs": plateau,
        "mtime": path.stat().st_mtime,
    }


def _is_log_completed(path: Path) -> bool:
    """A log is 'completed' if it ends with an early-stop or final-epoch
    summary, or hasn't been touched in >30 min.  Used to skip in-flight
    scanning for finished runs (those are handled by find_new_logs)."""
    try:
        st = path.stat()
    except OSError:
        return True
    # Stale logs (>30 min idle) are considered done — training crashed
    # or finished without writing the usual marker.
    if time.time() - st.st_mtime > 1800:
        return True
    try:
        # cheap tail read
        with open(path, "rb") as f:
            f.seek(max(0, st.st_size - 8192))
            tail = f.read().decode(errors="ignore")
    except OSError:
        return False
    for marker in (
        "Training finished", "Best epoch:", "Early stopping",
        "training done", "Final test"
    ):
        if marker in tail:
            return True
    return False


def collect_inflight_runs() -> list[dict]:
    """Scan every results subdir for in-flight runs and return their
    per-run snapshot dicts (see _scan_inflight_log).  Empty list if
    nothing is in flight or RESULT_ROOT is missing."""
    if not _training_in_progress():
        return []
    out: list[dict] = []
    pattern = _INFLIGHT_DEFAULT_RE
    metric_name = "mae_val"
    lower_is_better = (PRIMARY_METRIC_OP == "lt")
    # Allow per-target override
    for t in HARNESS.get("targets", []) or []:
        if t.get("inflight_pattern"):
            pattern = t["inflight_pattern"]
        if t.get("inflight_metric_name"):
            metric_name = t["inflight_metric_name"]
    for logp in discover_logs():
        if _is_log_completed(logp):
            continue
        snap = _scan_inflight_log(logp,
                                  pattern=pattern,
                                  lower_is_better=lower_is_better)
        if snap:
            snap["metric_name"] = metric_name
            out.append(snap)
    return out


def emit_inflight_log_md(inflight: list[dict]):
    """Append in-flight snapshots to log.md *only when meaningful change
    has occurred since the last emission* (best improved, OR ≥5 new
    epochs).  Tracked in .state/inflight_emit.json so we don't spam
    log.md every tick."""
    if not inflight:
        return
    state_file = STATE_DIR / "inflight_emit.json"
    try:
        prev = json.loads(state_file.read_text())
    except (OSError, json.JSONDecodeError):
        prev = {}
    new_state: dict = dict(prev)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    new_lines: list[str] = []
    for snap in inflight:
        key = snap["anchor"]
        prev_snap = prev.get(key, {})
        improved = (
            prev_snap.get("best") is None
            or (PRIMARY_METRIC_OP == "lt" and snap["best"] < prev_snap.get("best", float("inf")))
            or (PRIMARY_METRIC_OP == "gt" and snap["best"] > prev_snap.get("best", float("-inf")))
        )
        epoch_advanced = snap["last_epoch"] - prev_snap.get("last_epoch", -999) >= 5
        if not (improved or epoch_advanced):
            continue
        new_lines.append(
            f"TS={ts};PLAN=?;ANCHOR={snap['anchor']};AXIS=?;"
            f"TEST_MAE=N/A;BEST_VAL_MAE={snap['best']};"
            f"STATUS=in_progress;GIT=N/A;"
            f"HP=epochs={snap['last_epoch']},best_epoch={snap['best_epoch']},"
            f"plateau_epochs={snap['plateau_epochs']}"
        )
        new_state[key] = {"best": snap["best"], "last_epoch": snap["last_epoch"]}
    if new_lines:
        with open(LOG_FILE, "a") as f:
            for l in new_lines:
                f.write(l + "\n")
        state_file.write_text(json.dumps(new_state, indent=2))


def format_inflight_for_prompt(inflight: list[dict]) -> str:
    if not inflight:
        return ""
    lines = ["=== IN-FLIGHT TRAINING (live log scan, NOT yet committed) ==="]
    for snap in inflight:
        flag = ""
        if snap["plateau_epochs"] >= 10:
            flag = "   ⚠ PLATEAU"
        lines.append(
            f"  - {snap['anchor']}: epoch {snap['last_epoch']} "
            f"(best {snap['metric_name']}={snap['best']} @ ep {snap['best_epoch']}, "
            f"plateau_epochs={snap['plateau_epochs']}){flag}"
        )
    lines.append(
        "Per userprompt rules, plateau_epochs ≥ 10 with no improvement "
        "is grounds to abandon (manual git_discard + start next plan). "
        "Do NOT touch a run that is still actively improving."
    )
    return "\n".join(lines) + "\n"


# ── Training-process detection ──

def _training_in_progress() -> bool:
    """Return True if any python training process matching the editable_files
    pattern is currently running.  Used by run_tick() to decide whether to
    enter propose-mode (seed the next experiment) vs wait for an in-flight
    run to finish.  False on any error — cheaper to over-propose than to
    deadlock the loop."""
    try:
        ps_out = subprocess.check_output(["ps", "-eo", "args"], text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    train_scripts: list[str] = []
    for t in HARNESS.get("targets", []) or []:
        for ef in t.get("editable_files", []) or []:
            if ef.endswith(".py") and "train" in os.path.basename(ef).lower():
                train_scripts.append(os.path.basename(ef))
    if not train_scripts:
        train_scripts = ["train_c_v3_v4.py", "train.py"]
    for line in ps_out.strip().splitlines()[1:]:
        if "python" not in line:
            continue
        if any(s in line for s in train_scripts) or "nohup_train.log" in line:
            return True
    return False


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


# ── Cycle counter & stop protocol ──

def load_cycle_count() -> int:
    if CYCLE_FILE.exists():
        try:
            return int(CYCLE_FILE.read_text().strip())
        except ValueError:
            pass
    return 0


def increment_cycle_count() -> int:
    n = load_cycle_count() + 1
    CYCLE_FILE.write_text(str(n))
    return n


def reset_cycle_count():
    if CYCLE_FILE.exists():
        CYCLE_FILE.unlink()


def commit_workspace_tick(cycle: int, kept: bool) -> bool:
    """Commit any agent/HARP edits to the writeable WORK_DIR files
    (plan.md, log.md, memory.md, program.md USER-INJECTED block) into
    repo B's git history.  Returns True iff a commit was actually made.

    Without this commit, repeated edits to plan.md / memory.md / log.md
    would just pile up in the working tree forever, defeating both the
    git-as-experiment-management story and any push-to-remote backup.
    """
    if not (WORK_DIR / ".git").exists():
        return False
    pathspec = sorted(_WORKSPACE_AGENT_WRITEABLE)
    rc, _ = _git_in(WORK_DIR, ["add", "--"] + pathspec)
    if rc != 0:
        return False
    rc, _ = _git_in(WORK_DIR, ["diff", "--cached", "--quiet"])
    if rc == 0:
        return False
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    verdict = "keep" if kept else "no-keep"
    msg = f"tick T{cycle}: workspace state ({verdict}) [{ts}]"
    rc, out = _git_in(
        WORK_DIR,
        ["-c", "user.email=harp@local", "-c", "user.name=HARP",
         "commit", "-m", msg],
    )
    if rc != 0:
        print(f"[tick] workspace commit failed: {out}")
        return False
    print(f"[tick] workspace commit recorded: {msg}")
    return True


def push_workspace_if_due(*, kept_in_tick: bool, tag_pushed: bool = False):
    """Honour workspace_remote.{mode,push_on} from harness.yaml.

    Called after commit_workspace_tick (so the to-push commit already
    exists locally).  Never fatal — a push failure logs a warning and
    moves on; the next eligible tick will retry.
    """
    if not (WORK_DIR / ".git").exists():
        return
    wr = HARNESS.get("workspace_remote") or {}
    mode = (wr.get("mode") or "none").strip().lower()
    push_on = (wr.get("push_on") or "keep").strip().lower()
    if mode == "none" or push_on == "never":
        return
    rc, _ = _git_in(WORK_DIR, ["remote", "get-url", "origin"])
    if rc != 0:
        print("[push] workspace_remote configured but no 'origin' remote — "
              "run quickstart.sh to set it up; skipping")
        return
    if push_on == "keep" and not kept_in_tick:
        return
    rc, branch = _git_in(WORK_DIR, ["symbolic-ref", "--short", "HEAD"])
    if rc != 0:
        branch = "master"
    rc, out = _git_in(WORK_DIR, ["push", "origin", branch])
    if rc != 0:
        print(f"[push] workspace push failed (branch={branch}): {out}")
        return
    print(f"[push] workspace pushed to origin/{branch}")
    if tag_pushed:
        rc2, out2 = _git_in(WORK_DIR, ["push", "--tags", "origin"])
        if rc2 != 0:
            print(f"[push] tag push failed: {out2}")


def trigger_stop(reason: str, *, tag_final: bool = False):
    """Unified stop protocol — always idempotent.

    reason ∈ {threshold_met, agent_requested, max_cycle_exhausted,
              scope_violation, constitution_drift, manual}
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with open(LOG_FILE, "a") as f:
        f.write(f"TS={ts};EVENT=iteration_stopped;REASON={reason};"
                f"PROTOCOL={STOP_PROTOCOL};CYCLES={load_cycle_count()}\n")
    set_active(False)
    disable_cron()
    if tag_final or STOP_PROTOCOL == "hard":
        if GIT_EXPERIMENT_MGMT:
            tag = f"final/{reason}/{ts}"
            _git(["tag", tag])
            print(f"[stop] tagged final commit: {tag}")
    print(f"[stop] iteration halted (reason={reason}, "
          f"protocol={STOP_PROTOCOL}, cycles={load_cycle_count()})")


def disable_cron():
    try:
        raw = subprocess.check_output(["crontab", "-l"],
                                      stderr=subprocess.DEVNULL, text=True)
    except subprocess.CalledProcessError:
        return

    tag = "# harness-auto-research"
    lines = [l for l in raw.splitlines() if tag not in l]
    proc = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                          text=True, capture_output=True)
    if proc.returncode == 0:
        print("[tick] cron entry removed")


# ── userprompt.yaml → program.md USER-INJECTED block sync ──

def _userprompt_hash() -> str:
    if not USERPROMPT_FILE.exists():
        return ""
    return hashlib.sha256(USERPROMPT_FILE.read_bytes()).hexdigest()


def userprompt_dirty() -> tuple[bool, str]:
    """Return (dirty, current_hash). Dirty == file changed since last sync."""
    cur = _userprompt_hash()
    if not cur:
        return False, ""
    prev = (USERPROMPT_HASH_FILE.read_text().strip()
            if USERPROMPT_HASH_FILE.exists() else "")
    return (cur != prev), cur


def mark_userprompt_synced(h: str):
    USERPROMPT_HASH_FILE.write_text(h)


def _extract_user_injected(text: str) -> str | None:
    m = USER_INJECTED_RE.search(text)
    return m.group(1) if m else None


# ── program.md ownership model ───────────────────────────────────────
#
# SERVICE_ROOT/program.md (repo A) is a **template**, not a runtime
# authority.  After the first init (init_workspace.sh copies the
# template into WORK_DIR), repo B owns its own program.md.  The harness
# never overwrites B/program.md from A at runtime — that would couple
# every tick to A and contradict the "A is template-only" architecture
# (see README "Three repositories at play").
#
# Drift safeguard:
#   - At preflight end, the harness records sha256 of B/program.md
#     **with the USER-INJECTED block stripped** into
#     `.state/program_constitution.sha256`.
#   - At the end of every tick, the harness recomputes the same hash.
#     A mismatch means the agent edited program.md outside the
#     USER-INJECTED markers — that triggers atomic rollback +
#     trigger_stop("constitution_drift").
#
# To pull in framework-rule upgrades from A after the engineer has
# edited A/program.md, run `python3 scripts/sync_program.py`.  That
# script previews the diff, applies it on confirmation, and refreshes
# `.state/program_constitution.sha256`.  Framework upgrades are
# therefore explicit human actions, never silent per-tick syncs.

PROGRAM_CONST_HASH_FILE = STATE_DIR / "program_constitution.sha256"


def ensure_workspace_program_initialized() -> str:
    """If WORK_DIR/program.md is missing, seed it from SERVICE_ROOT/program.md
    (one-time bootstrap; init_workspace.sh normally already did this, this
    function is a defensive no-op for properly initialised workspaces).

    Returns the current B/program.md content (NEVER A's), so callers can
    inject the live constitution into agent prompts without touching A."""
    if WORKSPACE_PROGRAM_FILE.exists():
        return WORKSPACE_PROGRAM_FILE.read_text()
    if not PROGRAM_FILE.exists():
        return ""
    template = PROGRAM_FILE.read_text()
    WORKSPACE_PROGRAM_FILE.write_text(template)
    return template


def _constitution_text(text: str) -> str:
    """Return program.md content with the USER-INJECTED block (markers
    included) removed.  This is the part the agent must NEVER touch."""
    return USER_INJECTED_RE.sub("", text)


def _constitution_hash(text: str) -> str:
    return hashlib.sha256(
        _constitution_text(text).encode("utf-8")).hexdigest()


def record_program_constitution_hash() -> str:
    """Compute and persist the canonical constitution hash.  Called at the
    end of a successful preflight (workspace is "blessed" at that point)
    and from scripts/sync_program.py after a manual template upgrade."""
    if not WORKSPACE_PROGRAM_FILE.exists():
        return ""
    h = _constitution_hash(WORKSPACE_PROGRAM_FILE.read_text())
    PROGRAM_CONST_HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROGRAM_CONST_HASH_FILE.write_text(h)
    return h


def verify_program_constitution() -> tuple[bool, str, str]:
    """Returns (ok, expected_hash, current_hash).

    If no expected hash on disk yet (workspace was init'd before this
    safeguard existed), this call records the current hash AND returns
    ok=True — i.e. accept-and-record on first sight.  Subsequent ticks
    enforce strict equality.  This avoids spurious drift alarms when
    upgrading an existing workspace to the new poll_tick.py."""
    if not WORKSPACE_PROGRAM_FILE.exists():
        return (True, "", "")
    cur = _constitution_hash(WORKSPACE_PROGRAM_FILE.read_text())
    if not PROGRAM_CONST_HASH_FILE.exists():
        PROGRAM_CONST_HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROGRAM_CONST_HASH_FILE.write_text(cur)
        return (True, cur, cur)
    expected = PROGRAM_CONST_HASH_FILE.read_text().strip()
    return (cur == expected, expected, cur)


# ── memory.md — agent's research journal ─────────────────────────────
#
# memory.md is an append-only narrative of every closed experiment
# (motivation → hypothesis → change → result → conclusion → next).
# Unlike log.md (single-line KV records the harness writes), memory.md
# is written by the AGENT and is its primary mechanism for cross-tick
# learning.
#
# Lifecycle per closed experiment:
#   1. Tick T: harness keeps or discards the run, then calls
#      `enqueue_pending_memory(...)` which appends an entry to
#      .state/pending_memory.json keyed by EXP_ID = "<anchor>__<ts>".
#      EXP_ID is identical to the suffix of the git tag exp/<anchor>/<ts>.
#   2. Tick T (same call): build_agent_prompt() prepends a
#      "PENDING MEMORY ENTRIES" directive listing every queued EXP_ID.
#   3. Agent appends a "## EXP_ID: <id>" block to memory.md and emits
#      `MEMORY_DONE=<id>` (one line per entry written).
#   4. After invoke, run_tick() parses MEMORY_DONE markers and removes
#      those EXP_IDs from the queue.  Anything left in the queue is
#      re-prompted on the next tick.
#
# memory.md additionally appears in every prompt as the last K experiment
# blocks (configurable via agent.memory_tail_blocks), giving the agent
# its own short-term research history.

# Heading marker for one entry; used both for parsing and for telling the
# agent the exact format.
MEMORY_HEADING_RE = re.compile(r"^##\s+EXP_ID:\s*(\S+)\s*$", re.MULTILINE)
MEMORY_DONE_RE = re.compile(r"^\s*MEMORY_DONE=(\S+)\s*$", re.MULTILINE)
# Line-anchored marker detection.  We must NOT match the marker when it
# appears inside an agent's prose (e.g. "STOP_ITERATION=1 is not emitted"
# inside a paragraph).  Only stand-alone lines count.
STOP_ITERATION_RE = re.compile(r"^\s*STOP_ITERATION=1\s*$", re.MULTILINE)
PROGRAM_SYNC_DONE_RE = re.compile(r"^\s*PROGRAM_SYNC_DONE=1\s*$", re.MULTILINE)


def _stop_requested(output: str) -> bool:
    return bool(STOP_ITERATION_RE.search(output or ""))


def _program_sync_done(output: str) -> bool:
    return bool(PROGRAM_SYNC_DONE_RE.search(output or ""))


def load_pending_memory() -> dict:
    if not PENDING_MEMORY_FILE.exists():
        return {}
    try:
        return json.loads(PENDING_MEMORY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_pending_memory(data: dict):
    PENDING_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_MEMORY_FILE.write_text(json.dumps(data, indent=2))


def enqueue_pending_memory(*, anchor: str, ts: str, verdict: str,
                           test_mae, best_val_mae, plan_id: str = "?"):
    """Add a closed experiment to the pending-memory queue.  Idempotent
    on (anchor, ts): if it's already queued, this is a no-op."""
    exp_id = f"{anchor}__{ts}"
    queue = load_pending_memory()
    if exp_id in queue:
        return exp_id
    queue[exp_id] = {
        "anchor": anchor,
        "ts": ts,
        "verdict": verdict,
        "test_mae": test_mae if test_mae is not None else "N/A",
        "best_val_mae": best_val_mae if best_val_mae is not None else "N/A",
        "plan_id": plan_id,
    }
    save_pending_memory(queue)
    return exp_id


def dequeue_pending_memory(exp_ids):
    """Remove the given EXP_IDs from the queue (called after the agent
    confirms via MEMORY_DONE=<id> that it wrote the entries)."""
    queue = load_pending_memory()
    removed = [e for e in exp_ids if queue.pop(e, None) is not None]
    if removed:
        save_pending_memory(queue)
    return removed


def tail_memory(k: int) -> str:
    """Return the last `k` `## EXP_ID:` blocks from memory.md, joined
    verbatim.  k <= 0 returns the whole file (minus the header)."""
    if not MEMORY_FILE.exists():
        return ""
    text = MEMORY_FILE.read_text()
    matches = list(MEMORY_HEADING_RE.finditer(text))
    if not matches:
        return ""
    if k > 0 and len(matches) > k:
        start = matches[-k].start()
    else:
        start = matches[0].start()
    return text[start:].rstrip() + "\n"


def parse_memory_done(agent_output: str) -> list[str]:
    """Extract the list of EXP_IDs the agent claims to have written.
    Each `MEMORY_DONE=<EXP_ID>` line counts as one confirmation."""
    if not agent_output:
        return []
    return MEMORY_DONE_RE.findall(agent_output)


def build_agent_prompt(new_results: list[tuple[RunResult, dict | None]],
                       inflight: list[dict] | None = None) -> str:
    # Read B/program.md as-is (B owns it after init; A is template-only).
    # If somehow missing, ensure_* falls back to seeding from A — defensive.
    program_text = ensure_workspace_program_initialized()
    plan_text = PLAN_FILE.read_text() if PLAN_FILE.exists() else ""

    # Detect userprompt drift → ask the agent to translate it FIRST
    dirty, cur_hash = userprompt_dirty()
    sync_directive = ""
    if dirty:
        up_text = USERPROMPT_FILE.read_text()
        sync_directive = f"""
=== PROGRAM SYNC REQUIRED (do this BEFORE everything else) ===
The user has updated userprompt.yaml since the last tick.  Your first task
THIS tick is to translate it into the canonical HARD-CONSTRAINT format used
by program.md, and write the result into the USER-INJECTED block.

Steps (mandatory, in order):
  1. Read the rules array in `userprompt.yaml` (shown below).
  2. For each entry, rewrite it as a single, numbered, imperative HARD
     CONSTRAINT in the same voice as the rest of program.md
     (e.g. "1. NEVER replace the `grover_base` checkpoint with the
     Rf-fine-tuned GROVER variant; treat that direction as forbidden.").
  3. Edit ONLY between `{USER_INJECTED_BEGIN}` and `{USER_INJECTED_END}`
     in `{WORKSPACE_PROGRAM_FILE}`.  Replace any prior content there.
  4. Emit a line `PROGRAM_SYNC_DONE=1` so the harness can mark the
     userprompt hash synced; otherwise this directive will repeat next tick.
  5. AFTER syncing, treat the new constraints as binding and proceed with
     the normal optimisation reasoning below.

=== userprompt.yaml (source of truth) ===
{up_text}
=== end userprompt.yaml ===
"""

    # stash current hash so caller can mark synced after a successful invoke
    build_agent_prompt._pending_userprompt_hash = cur_hash if dirty else ""

    log_lines = []
    if LOG_FILE.exists():
        all_lines = [l for l in LOG_FILE.read_text().splitlines()
                     if l.strip() and not l.startswith("#")]
        log_lines = all_lines[-AGENT_MAX_LOG_LINES:]

    # memory.md tail — recent research history for the agent to reference
    # when writing motivations / hypotheses for the next experiment.
    memory_tail_text = tail_memory(AGENT_MAX_MEMORY_BLOCKS)

    # Pending memory entries — closed experiments that still need a
    # narrative block.  Re-prompted every tick until the agent confirms
    # via MEMORY_DONE=<EXP_ID>.
    pending_memory = load_pending_memory()
    memory_directive = ""
    if pending_memory:
        lines = []
        for exp_id, info in pending_memory.items():
            lines.append(
                f"  - EXP_ID={exp_id} VERDICT={info['verdict']} "
                f"PLAN={info['plan_id']} TEST_MAE={info['test_mae']} "
                f"BEST_VAL_MAE={info['best_val_mae']}"
            )
        memory_directive = f"""
=== PENDING MEMORY ENTRIES (you MUST append to memory.md this tick) ===
The following experiments closed in a recent tick but do not yet have a
narrative entry in memory.md.  For EACH item, append one block in this
exact format to memory.md, then emit a single line `MEMORY_DONE=<EXP_ID>`
(once per entry written) so the harness can dequeue it:

  ## EXP_ID: <EXP_ID>
  - TS:           <ts>
  - PARENT_PLAN:  <plan_id from log.md>
  - ANCHOR:       <anchor>
  - VERDICT:      keep | discard
  - METRIC:       test_mae=<x>; best_val_mae=<y>; delta_vs_prev_best=<+/-z or N/A>

  ### Motivation
  Why did we try this?  Reference prior memory EXP_IDs, userprompt rules,
  or specific log.md observations.  No vague claims like "improve model".

  ### Hypothesis
  A single falsifiable "if X then Y" statement.

  ### What changed
  - editable_files diff: <file>:<line range> (one-line summary)
  - new files under add_by_HARP/: <list> (or "none")
  - new YAML configs: <list> (or "none")

  ### Result interpretation
  Compare to the hypothesis.  Did the result support, refute, or partially
  refute it?  Quote the relevant numbers.

  ### Lesson / Next
  - What is now established?
  - Which directions are pruned?
  - Which direction is the next obvious experiment?

Items to write THIS tick:
{chr(10).join(lines)}
"""

    results_summary = []
    for r, p in new_results:
        pid = p["plan_id"] if p else "unmapped"
        results_summary.append(
            f"  - {r.anchor}: test_mae={r.test_mae}, "
            f"best_val_mae={r.best_val_mae}, plan={pid}"
        )

    propose_hint = ""
    if not new_results:
        propose_hint = (
            "\n=== TICK MODE: PROPOSE ===\n"
            "No new training results closed this tick AND no training is in "
            "flight.  Your job THIS tick is to seed/propose the next "
            "experiment: register a new PLAN_ID in plan.md (or pick an "
            "existing 'pending' one), create its config YAML and any "
            "AGENT-EDITABLE edits, git-commit in the target repo, then "
            "kick off training via the whitelisted nohup pattern from "
            "program.md.  Do NOT emit STOP_ITERATION unless the global "
            "stop threshold is genuinely met.\n"
        )

    gitnexus_hint = ""
    if GITNEXUS_ENABLED:
        repo_names = [t["name"] for t in TARGET_REPOS]
        gitnexus_hint = f"""
=== CODEBASE UNDERSTANDING (GitNexus MCP) ===
You have GitNexus knowledge-graph tools available via MCP. Indexed repos: {repo_names}
Key tools (call them as MCP tools, not shell commands):
  - query({{query: "...", repo: "..."}})  — find execution flows related to a concept
  - context({{name: "symbol_name", repo: "..."}})  — 360° view: callers, callees, processes
  - impact({{target: "symbol", direction: "upstream", repo: "..."}})  — blast radius before editing
  - cypher({{query: "MATCH ...", repo: "..."}})  — raw graph queries
Use these BEFORE modifying code to understand dependencies and assess safety.
"""

    # Surface the per-target writeable surfaces so the agent doesn't have to
    # re-derive them from program.md every tick.
    surfaces_lines = []
    for t in TARGET_REPOS:
        add_abs = t["repo_path"] / t["agent_addition_dir"]
        surfaces_lines.append(
            f"  - {t['name']}: edit AGENT-EDITABLE blocks in editable_files; "
            f"create new YAML configs under <repo>/tlc/configs/; "
            f"create ANY new file ONLY under {add_abs}/"
        )
    surfaces_hint = "=== WRITEABLE SURFACES (per target) ===\n" + "\n".join(surfaces_lines)

    prompt = f"""You are the HARP (auto-research) agent.  Read the constraints below
and decide the next action.

IMPORTANT: You are operating in workspace directory: {WORK_DIR}
- You may edit plan.md, log.md, and code inside AGENT-EDITABLE blocks.
- {WORKSPACE_PROGRAM_FILE} is READ-ONLY EXCEPT inside the
  <!-- USER-INJECTED-BEGIN --> / <!-- USER-INJECTED-END --> markers
  (only writable when "PROGRAM SYNC REQUIRED" is signalled).
- userprompt.yaml is the user's natural-language input — NEVER edit it.
- All file changes persist in this workspace only; the template is never touched.
{sync_directive}{memory_directive}{gitnexus_hint}
{surfaces_hint}

=== program.md (CONSTRAINTS — MUST OBEY) ===
{program_text}

=== plan.md (current plans) ===
{plan_text}

=== memory.md (last {AGENT_MAX_MEMORY_BLOCKS} experiment block(s)) ===
{memory_tail_text or '(memory.md is empty — this is the first experiment journal entry to come)'}

=== log.md (recent {len(log_lines)} lines) ===
{chr(10).join(log_lines)}

=== NEW RESULTS THIS TICK ===
{chr(10).join(results_summary) if results_summary else '(none — see TICK MODE: PROPOSE above)'}
{propose_hint}{format_inflight_for_prompt(inflight or [])}

Based on the above:
1. Use GitNexus tools to understand the target codebase before making changes.
2. Update plan.md if needed (add new plan with orthogonal axis, or mark
   completed).
3. If a promising next experiment is clear, create its config YAML and/or
   edit code inside AGENT-EDITABLE blocks, then git commit and start training.
4. If global stop threshold is met, output the line: STOP_ITERATION=1
5. Apply the simplicity criterion: prefer fewer changes for equal gain.
6. Summarize your reasoning and action in 2-3 sentences.
"""
    return prompt


def _get_or_create_chat_id() -> str | None:
    """Maintain a persistent agent chat session across ticks."""
    chat_file = STATE_DIR / "agent_chat_id.txt"
    if chat_file.exists():
        cid = chat_file.read_text().strip()
        if cid:
            return cid
    try:
        proc = subprocess.run(
            [AGENT_BIN, "create-chat",
             "--workspace", str(WORK_DIR)],
            capture_output=True, text=True, timeout=30,
        )
        cid = proc.stdout.strip()
        if cid and len(cid) > 10:
            chat_file.write_text(cid)
            print(f"[tick] created persistent chat: {cid[:8]}...")
            return cid
    except Exception as e:
        print(f"[tick] create-chat failed (non-fatal): {e}")
    return None


def _run_agent_streaming(cmd: list[str], timeout: int) -> tuple[int, str, bool]:
    """Run the agent CLI streaming stdout+stderr to a live file so we
    keep partial output on timeout (subprocess.run discards its buffer
    on TimeoutExpired).  Returns (returncode_or_-1, raw_text, timed_out).

    Raw text is the full stream-json output (one JSON object per line).
    Use _parse_agent_stream() to extract the assistant's textual reply
    + token usage from it."""
    raw_path = STATE_DIR / "last_agent_stream.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    rc = -1
    with open(raw_path, "w") as fout:
        proc = subprocess.Popen(
            cmd,
            stdout=fout, stderr=subprocess.STDOUT,
            cwd=str(WORK_DIR),
        )
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            print(f"[tick] agent timed out after {timeout}s — partial stream kept at {raw_path}")
    try:
        text = raw_path.read_text()
    except OSError:
        text = ""
    return rc, text, timed_out


def _parse_agent_stream(raw: str) -> tuple[str, dict]:
    """Walk the cursor-agent stream-json output.  Returns (assistant_text,
    usage_dict).  Falls back gracefully if the output is plain text
    (e.g. older agent versions or `--output-format text`).

    Each line in stream-json is one JSON event.  The agent's textual
    reply lives in the FINAL `assistant` event whose `message.content`
    is a single chunk holding the complete answer (deltas are dropped
    by us — the final aggregated chunk has the full text).  Token
    usage lives in the `result` event."""
    if not raw:
        return "", {}
    if not raw.lstrip().startswith("{"):
        # Not JSON — agent ran with text format or stream-json was unsupported.
        return raw, {}

    assistant_chunks: list[str] = []
    usage: dict = {}
    final_result_text = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        et = ev.get("type")
        if et == "assistant":
            msg = ev.get("message", {}) or {}
            for chunk in msg.get("content", []) or []:
                if chunk.get("type") == "text":
                    t = chunk.get("text", "")
                    if t:
                        assistant_chunks.append(t)
        elif et == "result":
            usage = ev.get("usage", {}) or {}
            r = ev.get("result")
            if isinstance(r, str):
                final_result_text = r

    # The cursor-agent stream emits the answer as many small `assistant`
    # delta events PLUS one final `assistant` event with the complete
    # text.  The simplest robust merge: prefer the result event's
    # `result` field (always the full final answer).  If that's absent,
    # take the longest assistant chunk seen.
    if final_result_text:
        text = final_result_text
    elif assistant_chunks:
        text = max(assistant_chunks, key=len)
    else:
        text = ""
    return text, usage


def _record_usage(usage: dict, *, mode: str, timed_out: bool, cycle: int):
    """Append a usage record + maintain a rolling summary.  Cheap and
    robust to missing keys (older agent versions)."""
    if not usage:
        return
    rec = {
        "ts": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "cycle": cycle,
        "mode": mode,
        "timed_out": timed_out,
        "input_tokens": usage.get("inputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "cache_read_tokens": usage.get("cacheReadTokens", 0),
        "cache_write_tokens": usage.get("cacheWriteTokens", 0),
    }
    usage_log = STATE_DIR / "usage.jsonl"
    usage_log.parent.mkdir(parents=True, exist_ok=True)
    with open(usage_log, "a") as f:
        f.write(json.dumps(rec) + "\n")

    # Recompute rolling summary from the full log so it stays consistent
    # even if a record was lost.  Cheap: ticks are infrequent.
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
              "ticks": 0, "timed_out_ticks": 0}
    try:
        for line in usage_log.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            totals["input"] += int(r.get("input_tokens", 0))
            totals["output"] += int(r.get("output_tokens", 0))
            totals["cache_read"] += int(r.get("cache_read_tokens", 0))
            totals["cache_write"] += int(r.get("cache_write_tokens", 0))
            totals["ticks"] += 1
            if r.get("timed_out"):
                totals["timed_out_ticks"] += 1
    except OSError:
        pass

    summary = (
        f"# HARP token usage summary (last update {rec['ts']})\n"
        f"ticks_with_usage:    {totals['ticks']}\n"
        f"timed_out_ticks:     {totals['timed_out_ticks']}\n"
        f"input_tokens_total:  {totals['input']}\n"
        f"output_tokens_total: {totals['output']}\n"
        f"cache_read_total:    {totals['cache_read']}\n"
        f"cache_write_total:   {totals['cache_write']}\n"
        f"# avg per tick (input + output, no cache): "
        f"{(totals['input'] + totals['output']) // max(totals['ticks'], 1)}\n"
    )
    (STATE_DIR / "usage_summary.txt").write_text(summary)


def invoke_agent(prompt: str, *, mode: str = "tick", cycle: int = 0
                 ) -> tuple[bool, str]:
    if not _agent_available():
        prompt_path = STATE_DIR / "last_prompt.txt"
        prompt_path.write_text(prompt)
        print(f"[tick] agent not available; prompt saved to {prompt_path}")
        return False, "[dry-run]"

    # stream-json gives us per-event records (assistant deltas + final
    # result with `usage`).  We always use it now so we can log token
    # cost per tick and recover partial output on timeout.
    cmd = [AGENT_BIN, "-p", "--force",
           "--output-format", "stream-json", "--stream-partial-output",
           "--workspace", str(WORK_DIR)]
    for flag in AGENT_FLAGS:
        if flag not in cmd:
            cmd.append(flag)
    if AGENT_MODEL:
        cmd.extend(["--model", AGENT_MODEL])

    chat_id = _get_or_create_chat_id()
    if chat_id:
        cmd.extend(["--resume", chat_id])

    cmd.append(prompt)

    rc, raw, timed_out = _run_agent_streaming(cmd, AGENT_TIMEOUT_SEC)
    output, usage = _parse_agent_stream(raw)
    stop = _stop_requested(output)

    # Persist a human-readable copy of just the assistant's reply for
    # the existing tooling (tail -f, audit, etc.) that expects
    # last_agent_output.txt to be plain text.
    (STATE_DIR / "last_agent_output.txt").write_text(output or "[empty]")

    # If we were resuming and got nothing useful, retry once with a fresh
    # session.  Resumed sessions can corrupt or accumulate context to the
    # point of stalling; a fresh chat usually unblocks.
    if (not output.strip() or timed_out) and chat_id:
        print("[tick] empty/timed-out response with --resume; clearing chat_id and retrying fresh")
        try:
            (STATE_DIR / "agent_chat_id.txt").unlink()
        except FileNotFoundError:
            pass
        cmd_retry = [AGENT_BIN, "-p", "--force",
                     "--output-format", "stream-json", "--stream-partial-output",
                     "--workspace", str(WORK_DIR)]
        for flag in AGENT_FLAGS:
            if flag not in cmd_retry:
                cmd_retry.append(flag)
        if AGENT_MODEL:
            cmd_retry.extend(["--model", AGENT_MODEL])
        cmd_retry.append(prompt)
        rc, raw, timed_out = _run_agent_streaming(cmd_retry, AGENT_TIMEOUT_SEC)
        output, usage = _parse_agent_stream(raw)
        stop = _stop_requested(output)
        (STATE_DIR / "last_agent_output.txt").write_text(output or "[empty]")

    _record_usage(usage, mode=mode, timed_out=timed_out, cycle=cycle)
    if usage:
        print(f"[tick] usage: in={usage.get('inputTokens',0)} "
              f"out={usage.get('outputTokens',0)} "
              f"cache_r={usage.get('cacheReadTokens',0)} "
              f"cache_w={usage.get('cacheWriteTokens',0)}")

    if timed_out:
        return False, output  # partial output retained
    if rc != 0:
        print(f"[tick] agent exit rc={rc}")
    return stop, output


def _agent_available() -> bool:
    try:
        subprocess.run([AGENT_BIN, "--version"],
                       capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def _get_git_head(repo_path: Path) -> str:
    """Return the current HEAD commit hash of a git repo."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(repo_path), timeout=10,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def _get_indexed_commit(repo_name: str) -> str:
    """Read the last indexed commit from GitNexus state."""
    state_file = STATE_DIR / "gitnexus_indexed_commits.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            return data.get(repo_name, "")
        except (json.JSONDecodeError, KeyError):
            pass
    return ""


def _save_indexed_commit(repo_name: str, commit: str):
    state_file = STATE_DIR / "gitnexus_indexed_commits.json"
    data = {}
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            pass
    data[repo_name] = commit
    state_file.write_text(json.dumps(data, indent=2))


def _find_node_bin() -> str:
    """Find a node >=20 binary. Prefers explicit config, then PATH, then cursor-server."""
    explicit = _tools.get("node_bin", "")
    if explicit:
        return explicit
    try:
        proc = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=5)
        ver = proc.stdout.strip().lstrip("v").split(".")[0]
        if int(ver) >= 20:
            return "node"
    except Exception:
        pass
    import glob as _glob
    for p in _glob.glob("/root/.cursor-server/bin/linux-x64/*/node"):
        try:
            proc = subprocess.run(
                [p, "--version"], capture_output=True, text=True, timeout=5)
            ver = proc.stdout.strip().lstrip("v").split(".")[0]
            if int(ver) >= 20:
                return p
        except Exception:
            continue
    return "node"


def ensure_gitnexus_index():
    """Re-index target repos via GitNexus if HEAD has changed since last index."""
    if not GITNEXUS_ENABLED:
        return

    npx = "npx"
    for target in TARGET_REPOS:
        name = target["name"]
        repo_path = target["repo_path"]
        if not repo_path.is_dir():
            print(f"[gitnexus] skip {name}: repo not found at {repo_path}")
            continue

        current_head = _get_git_head(repo_path)
        if not current_head:
            print(f"[gitnexus] skip {name}: not a git repo or no commits")
            continue

        if GITNEXUS_REINDEX_ON == "git_head_change":
            last_indexed = _get_indexed_commit(name)
            if current_head == last_indexed:
                print(f"[gitnexus] {name}: index up-to-date ({current_head[:8]})")
                continue

        print(f"[gitnexus] re-indexing {name} ({current_head[:8]})...")
        # Prefer locally cached package (faster than re-resolving via npx).
        cached_paths = list(Path("/root/.npm/_npx").glob(
            "*/node_modules/gitnexus/dist/cli/index.js"))
        node_bin = _find_node_bin()
        if cached_paths:
            gitnexus_cmd = [node_bin, str(cached_paths[0]),
                            "analyze", str(repo_path)]
        else:
            gitnexus_cmd = [npx, "-y", GITNEXUS_PACKAGE,
                            "analyze", str(repo_path)]
        try:
            proc = subprocess.run(
                gitnexus_cmd,
                capture_output=True, text=True,
                timeout=GITNEXUS_ANALYZE_TIMEOUT,
                cwd=str(SERVICE_ROOT),
            )
            if proc.returncode == 0:
                _save_indexed_commit(name, current_head)
                print(f"[gitnexus] {name}: indexed successfully")
            else:
                errmsg = (proc.stdout + proc.stderr)[:300]
                if "GLIBC" in errmsg or "GLIBCXX" in errmsg:
                    print(f"[gitnexus] {name}: native library requires newer glibc "
                          f"(non-fatal, MCP queries still work on existing index). "
                          f"Pin to gitnexus<=1.3.x for KuzuDB backend.")
                    _save_indexed_commit(name, current_head)
                else:
                    print(f"[gitnexus] {name}: analyze failed: {errmsg[:200]}")
        except subprocess.TimeoutExpired:
            print(f"[gitnexus] {name}: analyze timed out "
                  f"({GITNEXUS_ANALYZE_TIMEOUT}s)")
        except Exception as e:
            print(f"[gitnexus] {name}: analyze error (non-fatal): {e}")


# ── Post-tick scope audit ──
#
# Detect and undo any agent file change that falls outside the writeable
# surfaces declared in program.md.  The enforcement is strictly post-hoc
# (LLM compliance + audit, no filesystem ACLs) — see README "Why isn't the
# scope physically enforced?".
#
# Algorithm:
#   1. Snapshot HEAD + currently-dirty files in every target repo and in
#      WORK_DIR BEFORE invoking the agent.  Pre-existing dirt is never
#      blamed on the agent.
#   2. After the agent returns, diff against the snapshot to compute the
#      set of (changed_tracked, new_untracked) paths attributable to it.
#   3. Classify each path against a per-target / workspace allowlist.
#   4. If ANY violation: ATOMIC FULL ROLLBACK of every snapshot
#      (target repos + workspace), append a `scope_violation` event to
#      log.md, and call trigger_stop("scope_violation").
#
# Why atomic rollback (vs. surgical revert of just the offending file)?
#   - Simpler invariant: either the whole tick lands or none of it does.
#   - Removes ambiguity for git_keep / git_discard downstream.
#   - Strong incentive: one stray file → lose all of this tick's work.

def _git_in(repo: Path, args: list[str]) -> tuple[int, str]:
    p = subprocess.run(["git"] + args, cwd=str(repo),
                       capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def _git_in_raw(repo: Path, args: list[str]) -> tuple[int, str]:
    """Like `_git_in` but does NOT `.strip()` stdout. Required for
    `git status --porcelain`, whose format is `XY␣path` with a leading
    space when only the work-tree is modified — `.strip()` would chop
    the first character of every such path."""
    p = subprocess.run(["git"] + args, cwd=str(repo),
                       capture_output=True, text=True)
    return p.returncode, p.stdout


def _parse_porcelain(out: str) -> tuple[set[str], set[str]]:
    """Parse `git status --porcelain` v1 output into
    (modified_or_staged_paths, untracked_paths)."""
    modified: set[str] = set()
    untracked: set[str] = set()
    for ln in out.splitlines():
        if len(ln) < 4:
            continue
        code, path = ln[:2], ln[3:].split(" -> ")[-1]
        if code == "??":
            untracked.add(path)
        else:
            modified.add(path)
    return modified, untracked


def _snapshot_repo(repo: Path) -> dict:
    """Capture HEAD commit + currently-dirty paths so we can ignore
    pre-existing dirt in the audit."""
    if not (repo / ".git").exists():
        return {"head": "", "dirty_at_start": set(), "tracked": True}
    rc, head = _git_in(repo, ["rev-parse", "HEAD"])
    # `-uall` forces git to expand untracked directories into individual
    # file paths.  Without it, an untracked directory shows up as
    # "<dir>/" and any new file the agent adds inside that directory
    # would be hidden under the same collapsed entry, defeating the audit.
    rc2, status = _git_in_raw(repo, ["status", "--porcelain", "-uall"])
    mod, unt = _parse_porcelain(status) if rc2 == 0 else (set(), set())
    return {
        "head": head if rc == 0 else "",
        "dirty_at_start": mod | unt,
        "tracked": True,
    }


def _changed_paths_since(repo: Path, snap: dict) -> tuple[set[str], set[str]]:
    """Return (changed_tracked, new_untracked) caused by the agent."""
    if not snap.get("tracked") or not (repo / ".git").exists():
        return set(), set()
    changed: set[str] = set()
    if snap.get("head"):
        rc, out = _git_in(repo, ["diff", "--name-only", snap["head"], "HEAD"])
        if rc == 0 and out:
            changed.update(out.splitlines())
    rc2, out2 = _git_in_raw(repo, ["status", "--porcelain", "-uall"])
    mod, untracked = _parse_porcelain(out2) if rc2 == 0 else (set(), set())
    changed |= mod
    pre = snap.get("dirty_at_start", set())
    return changed - pre, untracked - pre


def _path_allowed_in_target(rel: str, target: dict) -> bool:
    """Mirror of program.md "CAN do" rules, applied to a path relative
    to repo_path."""
    rel = rel.replace("\\", "/")
    if rel in set(target.get("editable_files", [])):
        return True
    cfg = target.get("config_dir", "tlc/configs").rstrip("/") + "/"
    if rel.startswith(cfg) and rel.endswith((".yaml", ".yml")):
        return True
    add = target.get("agent_addition_dir", "add_by_HARP").rstrip("/") + "/"
    if rel.startswith(add):
        return True
    return False


# Files in WORK_DIR the agent may modify.  `program.md` is allowed in
# full here because line-level scope (USER-INJECTED block only) is
# enforced by the constitution-hash check immediately after this audit
# (verify_program_constitution), which catches any byte change outside
# the USER-INJECTED markers.  `memory.md` is the agent's research
# journal (append-only by convention; the audit only checks scope).
_WORKSPACE_AGENT_WRITEABLE = {"plan.md", "log.md", "program.md", "memory.md"}


def _path_allowed_in_workspace(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    return rel in _WORKSPACE_AGENT_WRITEABLE


def audit_target(target: dict, snap: dict) -> list[str]:
    repo = Path(target["repo_path"])
    changed, untracked = _changed_paths_since(repo, snap)
    return sorted(p for p in (changed | untracked)
                  if not _path_allowed_in_target(p, target))


def audit_workspace(snap: dict) -> list[str]:
    changed, untracked = _changed_paths_since(WORK_DIR, snap)
    return sorted(p for p in (changed | untracked)
                  if not _path_allowed_in_workspace(p))


def rollback_repo(repo: Path, snap: dict):
    """Hard-reset to snapshot HEAD and remove untracked (gitignored
    paths are preserved by `git clean -fd` default behaviour, so .state/
    and .backup/ are safe)."""
    if not (repo / ".git").exists():
        return
    if snap.get("head"):
        _git_in(repo, ["reset", "--hard", snap["head"]])
    _git_in(repo, ["clean", "-fd"])


def perform_scope_audit(target_snaps: dict, ws_snap: dict) -> bool:
    """Returns True if a violation was found and rollback was performed."""
    all_violations: list[tuple[str, list[str]]] = []
    for t in HARNESS.get("targets", []):
        snap = target_snaps.get(t["name"])
        if snap is None:
            continue
        vs = audit_target(t, snap)
        if vs:
            all_violations.append((t["name"], vs))
    ws_vs = audit_workspace(ws_snap)
    if ws_vs:
        all_violations.append(("__workspace__", ws_vs))

    if not all_violations:
        return False

    print("[audit] *** SCOPE VIOLATION(S) DETECTED — full rollback ***")
    for name, vs in all_violations:
        for v in vs:
            print(f"[audit]   {name}: {v}")

    for t in HARNESS.get("targets", []):
        snap = target_snaps.get(t["name"])
        if snap is not None:
            rollback_repo(Path(t["repo_path"]), snap)
            print(f"[audit]   rolled back {t['name']} to {snap.get('head','')[:8]}")
    rollback_repo(WORK_DIR, ws_snap)
    print(f"[audit]   rolled back workspace to {ws_snap.get('head','')[:8]}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    paths_str = ";".join(f"{n}:{p}" for n, vs in all_violations for p in vs)
    if len(paths_str) > 800:
        paths_str = paths_str[:800] + "...TRUNCATED"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"TS={ts};EVENT=scope_violation;ACTION=full_rollback;"
                f"PATHS={paths_str}\n")
    return True


# ── Preflight (agent-driven self-check) ─────────────────────────────
#
# Invoked via `poll_tick.py --mode preflight` (typically from
# scripts/quickstart.sh).  The agent reads check.md and performs the
# bootstrap checklist (verify AGENT-EDITABLE markers, pick baseline,
# register baseline metric, write baseline memory entry, tag baseline
# in target repo).  Same snapshot/audit guarantees as a normal tick.

PREFLIGHT_DONE_RE = re.compile(
    r"PREFLIGHT_DONE=1\s+TARGETS_OK=(\d+)\s+WARNINGS=(\d+)\s+FAILS=(\d+)"
)
PREFLIGHT_FAIL_RE = re.compile(r"PREFLIGHT_FAIL=\S+")


def _summarize_result_path(repo_path: Path, result_path: Path) -> str:
    """List candidate run subdirs under result_path so the agent doesn't
    have to recurse blindly. Each entry: <name> <log_size_bytes>."""
    if not result_path.is_dir():
        return f"  (result_path does not exist: {result_path})"
    rows = []
    for sub in sorted(result_path.iterdir()):
        if not sub.is_dir():
            continue
        log = sub / "nohup_train.log"
        cfg = sub / "effective_config.yaml"
        if log.exists():
            rows.append(f"  - {sub.name}  log_bytes={log.stat().st_size} "
                        f"has_config={'y' if cfg.exists() else 'n'}")
    if not rows:
        return "  (no run subdirs with nohup_train.log found)"
    return "\n".join(rows)


def _summarize_editable_files(target: dict) -> str:
    repo = Path(target["repo_path"])
    rows = []
    for rel in target.get("editable_files", []):
        abs_p = repo / rel
        rows.append(f"  - {rel}  exists={'y' if abs_p.exists() else 'n'}")
    return "\n".join(rows) if rows else "  (no editable_files declared)"


def build_preflight_prompt() -> str:
    """Wrapper prompt that embeds check.md + per-target context.  The
    agent's job is to follow check.md verbatim and emit the marker
    lines (PREFLIGHT_INFO=, PREFLIGHT_WARN=, PREFLIGHT_FAIL=,
    PREFLIGHT_DONE=) the harness greps for."""
    program_text = ensure_workspace_program_initialized()

    # check.md lives in WORK_DIR after init; fall back to template.
    if CHECK_FILE.exists():
        check_text = CHECK_FILE.read_text()
    elif (SERVICE_ROOT / "check.md").exists():
        check_text = (SERVICE_ROOT / "check.md").read_text()
    else:
        check_text = "(check.md missing — abort and report PREFLIGHT_FAIL=no_check_md)"

    # PROGRAM SYNC directive piggybacks on userprompt drift.
    dirty, cur_hash = userprompt_dirty()
    sync_directive = ""
    if dirty:
        up_text = USERPROMPT_FILE.read_text()
        sync_directive = f"""
=== PROGRAM SYNC REQUIRED (Step 0 of check.md) ===
The user has updated userprompt.yaml since the last sync.  Translate
each entry into the canonical HARD-CONSTRAINT format inside
`{USER_INJECTED_BEGIN}` / `{USER_INJECTED_END}` of
`{WORKSPACE_PROGRAM_FILE}`, then emit `PROGRAM_SYNC_DONE=1`.

=== userprompt.yaml ===
{up_text}
=== end userprompt.yaml ===
"""
    build_preflight_prompt._pending_userprompt_hash = cur_hash if dirty else ""

    # Per-target context block.
    target_blocks = []
    for t in HARNESS.get("targets", []):
        name = t.get("name", "?")
        repo = t.get("repo_path", "?")
        result_path = Path(t.get("result_path", ""))
        baseline_anchor = t.get("baseline_anchor", "(unset — auto-pick best)")
        primary_metric = t.get("primary_metric", "best_val_mae")
        metric_op = t.get("metric_op", "lt")
        add_dir = t.get("agent_addition_dir", "add_by_HARP")
        target_blocks.append(f"""
--- TARGET: {name} ---
repo_path:           {repo}
result_path:         {result_path}
baseline_anchor:     {baseline_anchor}
primary_metric:      {primary_metric}  (op={metric_op})
agent_addition_dir:  {repo}/{add_dir}/

editable_files:
{_summarize_editable_files(t)}

candidate runs under result_path:
{_summarize_result_path(Path(repo), result_path)}
""")

    memory_tail_text = tail_memory(AGENT_MAX_MEMORY_BLOCKS)

    gitnexus_hint = ""
    if GITNEXUS_ENABLED:
        repo_names = [t["name"] for t in TARGET_REPOS]
        gitnexus_hint = (
            "\n=== CODEBASE UNDERSTANDING (GitNexus MCP) ===\n"
            f"Indexed repos: {repo_names}\n"
            "Use query/context/impact/cypher to inspect editable_files "
            "and locate AGENT-EDITABLE markers (Step 1 of check.md).\n"
        )

    prompt = f"""You are the HARP (auto-research) agent in PREFLIGHT mode.

Your job THIS invocation is to execute the checklist in `check.md`
verbatim and emit the marker lines specified there.  DO NOT propose
new experiments, edit `editable_files` content, create files under
`{add_dir}/`, or train anything.  The next normal tick will do all of
that.

Workspace: {WORK_DIR}
{sync_directive}{gitnexus_hint}
=== program.md (CONSTRAINTS — STILL BINDING IN PREFLIGHT) ===
{program_text}

=== check.md (PREFLIGHT PROTOCOL — EXECUTE THIS) ===
{check_text}

=== TARGETS ({len(HARNESS.get("targets", []))}) ===
{''.join(target_blocks)}

=== memory.md (last {AGENT_MAX_MEMORY_BLOCKS} block(s)) ===
{memory_tail_text or '(memory.md is empty — Step 5 of check.md will write the first entry.)'}

Begin now.  Final line of your output MUST be:

  PREFLIGHT_DONE=1 TARGETS_OK=<n> WARNINGS=<m> FAILS=<k>
"""
    return prompt


def run_preflight():
    """Agent-driven bootstrap.  Same audit guarantees as run_tick."""
    ensure_state_dir()

    # check.md must be available somewhere.  init_workspace.sh copies it
    # into WORK_DIR; SERVICE_ROOT is the canonical source.
    if not CHECK_FILE.exists() and not (SERVICE_ROOT / "check.md").exists():
        sys.exit("[preflight] FATAL: check.md not found in WORK_DIR or SERVICE_ROOT")

    ensure_gitnexus_index()

    prompt = build_preflight_prompt()
    pending_hash = getattr(build_preflight_prompt,
                           "_pending_userprompt_hash", "")

    target_snaps = {
        t["name"]: _snapshot_repo(Path(t["repo_path"]))
        for t in HARNESS.get("targets", [])
        if t.get("repo_path")
    }
    ws_snap = _snapshot_repo(WORK_DIR)

    _, agent_output = invoke_agent(prompt, mode="preflight", cycle=0)

    if agent_output and agent_output != "[dry-run]":
        (STATE_DIR / "last_preflight_output.txt").write_text(agent_output)
        print(f"[preflight] agent output saved to "
              f"{STATE_DIR / 'last_preflight_output.txt'}")

    if perform_scope_audit(target_snaps, ws_snap):
        print("[preflight] *** scope violation during preflight — full rollback ***")
        trigger_stop("scope_violation", tag_final=False)
        sys.exit(2)

    if pending_hash:
        if agent_output and _program_sync_done(agent_output):
            mark_userprompt_synced(pending_hash)
            print(f"[preflight] userprompt synced (sha={pending_hash[:8]})")
        else:
            print("[preflight] WARN: userprompt sync NOT confirmed — "
                  "next normal tick will re-prompt")

    confirmed = parse_memory_done(agent_output or "")
    if confirmed:
        dequeue_pending_memory(confirmed)
        print(f"[preflight] memory entries written: {len(confirmed)} "
              f"({', '.join(confirmed[:5])})")

    fails = len(PREFLIGHT_FAIL_RE.findall(agent_output or ""))
    done_m = PREFLIGHT_DONE_RE.search(agent_output or "")

    if not done_m:
        print("[preflight] FAIL: agent did not emit PREFLIGHT_DONE=1 marker")
        sys.exit(3)

    targets_ok, warnings, k_fails = (int(x) for x in done_m.groups())
    print(f"[preflight] summary: targets_ok={targets_ok} "
          f"warnings={warnings} fails={k_fails}")

    if k_fails > 0 or fails > 0:
        print("[preflight] FAIL: see PREFLIGHT_FAIL= lines in agent output above")
        if PREFLIGHT_OK_FILE.exists():
            PREFLIGHT_OK_FILE.unlink()
        sys.exit(4)

    PREFLIGHT_OK_FILE.write_text(
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "\n")
    print(f"[preflight] OK — marker written to {PREFLIGHT_OK_FILE}")

    # Bless the current B/program.md as the canonical constitution.
    # Subsequent ticks compare against this hash; any drift outside the
    # USER-INJECTED block triggers atomic rollback + constitution_drift stop.
    h = record_program_constitution_hash()
    if h:
        print(f"[preflight] constitution hash recorded: sha256={h[:16]}… "
              f"-> {PROGRAM_CONST_HASH_FILE}")

    print(f"[preflight] next: bash {SERVICE_ROOT}/scripts/install_cron.sh install")


def run_tick():
    ensure_state_dir()

    if not is_active():
        print("[tick] iteration_active is false — ensuring cron is also disabled")
        disable_cron()
        return

    if not RESULT_ROOT.is_dir():
        print(f"[tick] RESULT_ROOT not found: {RESULT_ROOT}")
        return

    check_and_kill_overtime_training()

    scan_state = load_scan_state()
    new_logs = find_new_logs(scan_state)

    # Always scan in-flight logs so we can (a) update log.md with
    # STATUS=in_progress snapshots and (b) decide whether to wake the
    # agent for an abandon-on-plateau decision.
    inflight = collect_inflight_runs()
    emit_inflight_log_md(inflight)
    plateaued = [s for s in inflight if s["plateau_epochs"] >= 10]
    if inflight:
        print(f"[tick] in-flight: {len(inflight)} run(s), "
              f"{len(plateaued)} plateaued (≥10 epoch no improvement)")

    # Cold-start / propose-mode handling.
    # Reactive-only tick (the original behaviour) deadlocks on a fresh
    # workspace: preflight is forbidden from proposing, ticks only fire
    # on new logs, so without external nudge the loop never starts.
    # Solution: when there are NO new logs AND NO training currently
    # running, fall through to invoke the agent in propose-only mode.
    # If training IS in flight we wait UNLESS a plateau is detected, in
    # which case enter monitor mode so the agent can decide abandon.
    propose_mode = False
    monitor_mode = False
    if not new_logs:
        if _training_in_progress():
            if plateaued:
                monitor_mode = True
                print(f"[tick] no new logs but {len(plateaued)} plateaued "
                      f"in-flight run(s) — entering monitor mode")
            else:
                print("[tick] no new logs; training in flight, no plateau — waiting")
                return
        else:
            print("[tick] no new logs; entering propose mode (no training in flight)")
            propose_mode = True

    if not (propose_mode or monitor_mode):
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
        plan_id_for_mem = plan["plan_id"] if plan else "unmapped"

        if GIT_EXPERIMENT_MGMT and result.test_mae is not None:
            if is_improvement(result.test_mae, best_metric):
                git_action = "keep"
                ts = git_keep(result.anchor)
                save_best_metric(result.test_mae)
                best_metric = result.test_mae
                print(f"  [git] NEW BEST: {result.test_mae}")
                enqueue_pending_memory(
                    anchor=result.anchor, ts=ts, verdict="keep",
                    test_mae=result.test_mae,
                    best_val_mae=result.best_val_mae,
                    plan_id=plan_id_for_mem,
                )
            else:
                git_action = "discard"
                print(f"  [git] no improvement ({result.test_mae} vs best {best_metric})")
                # Even discarded runs deserve a memory entry — that's
                # often where the best lessons are ("dropout sweep
                # didn't help, prune that direction").
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                enqueue_pending_memory(
                    anchor=result.anchor, ts=ts, verdict="discard",
                    test_mae=result.test_mae,
                    best_val_mae=result.best_val_mae,
                    plan_id=plan_id_for_mem,
                )

        status = append_log_line(result, plan, git_action)
        new_results.append((result, plan))
        pid = plan["plan_id"] if plan else "unmapped"
        print(f"  [{status}] {result.anchor} -> plan={pid} "
              f"test_mae={result.test_mae} git={git_action}")

    update_scan_state(scan_state, new_logs)
    save_scan_state(scan_state)

    # In propose / monitor mode there's nothing to evaluate — skip
    # results-processing guards and go straight to agent invocation.
    if not (propose_mode or monitor_mode) and not new_results:
        print("[tick] no completed runs to process")
        return

    all_results = [r for r, _ in new_results]
    if not (propose_mode or monitor_mode) and check_global_stop(all_results):
        print("[tick] *** GLOBAL STOP THRESHOLD MET ***")
        # No agent ran this tick, so cycle counter is unchanged; capture
        # the final log.md / memory.md state for posterity, then push.
        final_committed = commit_workspace_tick(load_cycle_count(), kept=True)
        if final_committed:
            push_workspace_if_due(kept_in_tick=True, tag_pushed=True)
        trigger_stop("threshold_met", tag_final=True)
        return

    ensure_gitnexus_index()
    prompt = build_agent_prompt(new_results, inflight=inflight)
    pending_hash = getattr(build_agent_prompt, "_pending_userprompt_hash", "")

    # Snapshot every target repo + workspace BEFORE the agent runs, so the
    # post-tick audit can attribute changes precisely.  See perform_scope_audit.
    target_snaps = {
        t["name"]: _snapshot_repo(Path(t["repo_path"]))
        for t in HARNESS.get("targets", [])
        if t.get("repo_path")
    }
    ws_snap = _snapshot_repo(WORK_DIR)

    pending_cycle = load_cycle_count() + 1
    if propose_mode:
        invoke_mode = "propose"
    elif monitor_mode:
        invoke_mode = "monitor"
    else:
        invoke_mode = "tick"
    stop_requested, agent_output = invoke_agent(
        prompt,
        mode=invoke_mode,
        cycle=pending_cycle,
    )

    cycle = increment_cycle_count()
    print(f"[tick] cycle counter -> {cycle}"
          + (f" / {MAX_CYCLE}" if MAX_CYCLE > 0 else " (unlimited)"))

    if agent_output and agent_output != "[dry-run]":
        agent_log = STATE_DIR / "last_agent_output.txt"
        agent_log.write_text(agent_output)
        print(f"[tick] agent output saved to {agent_log}")

    # Post-tick scope audit: any out-of-bounds file change → atomic
    # rollback of all target repos + workspace, and stop iteration.
    # Runs BEFORE userprompt-sync / memory-sync confirmation and stop
    # handling so a rolled-back tick can't accidentally mark anything
    # as "done".
    if perform_scope_audit(target_snaps, ws_snap):
        trigger_stop("scope_violation", tag_final=False)
        return

    # Constitution drift check: program.md is in the workspace allowlist
    # (so the agent can update USER-INJECTED), but everything outside the
    # USER-INJECTED markers must remain byte-identical to what preflight
    # blessed.  A mismatch == agent edited the constitution.  Same atomic
    # rollback semantics as scope_violation, separate stop reason for the
    # incident report.
    ok, expected_h, current_h = verify_program_constitution()
    if not ok:
        print("[tick] *** CONSTITUTION DRIFT DETECTED — full rollback ***")
        print(f"[tick]   expected sha256[:16]: {expected_h[:16]}")
        print(f"[tick]   current  sha256[:16]: {current_h[:16]}")
        for t in HARNESS.get("targets", []):
            snap = target_snaps.get(t["name"])
            if snap is not None:
                rollback_repo(Path(t["repo_path"]), snap)
                print(f"[tick]   rolled back {t['name']} to "
                      f"{snap.get('head','')[:8]}")
        rollback_repo(WORK_DIR, ws_snap)
        print(f"[tick]   rolled back workspace to {ws_snap.get('head','')[:8]}")
        ts_now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"TS={ts_now};EVENT=constitution_drift;"
                    f"ACTION=full_rollback;EXPECTED={expected_h[:16]};"
                    f"CURRENT={current_h[:16]}\n")
        trigger_stop("constitution_drift", tag_final=False)
        return

    # Mark userprompt synced only if the agent confirmed it did the translation.
    # Otherwise the directive will repeat on the next tick.
    if pending_hash:
        if agent_output and _program_sync_done(agent_output):
            mark_userprompt_synced(pending_hash)
            print(f"[tick] userprompt sync confirmed (sha={pending_hash[:8]})")
        else:
            print("[tick] userprompt sync NOT confirmed by agent — will retry next tick")

    # Dequeue any memory entries the agent confirmed it wrote.  Anything
    # left in the queue is re-prompted on the next tick.
    confirmed = parse_memory_done(agent_output or "")
    if confirmed:
        removed = dequeue_pending_memory(confirmed)
        print(f"[tick] memory entries confirmed: {len(removed)} "
              f"({', '.join(removed[:5])}{'…' if len(removed) > 5 else ''})")
    still_pending = load_pending_memory()
    if still_pending:
        print(f"[tick] memory entries still pending: {len(still_pending)} "
              f"(will re-prompt next tick)")

    kept_in_tick = any(
        GIT_EXPERIMENT_MGMT
        and r.test_mae is not None
        and r.test_mae == best_metric
        for r, _ in new_results
    )
    workspace_committed = commit_workspace_tick(cycle, kept=kept_in_tick)
    if workspace_committed:
        push_workspace_if_due(kept_in_tick=kept_in_tick)

    if stop_requested:
        print("[tick] agent requested stop")
        trigger_stop("agent_requested", tag_final=True)
        return

    if MAX_CYCLE > 0 and cycle >= MAX_CYCLE:
        print(f"[tick] *** MAX_CYCLE REACHED ({cycle} >= {MAX_CYCLE}) ***")
        trigger_stop("max_cycle_exhausted", tag_final=True)
        return


def main():
    import argparse
    p = argparse.ArgumentParser(description="HARP orchestrator (tick or preflight)")
    p.add_argument("--mode", choices=["tick", "preflight"], default="tick",
                   help="tick = normal scheduled iteration; "
                        "preflight = agent-driven self-check (one-shot)")
    args = p.parse_args()

    ensure_state_dir()
    LOCKFILE.parent.mkdir(parents=True, exist_ok=True)

    lock_fd = open(LOCKFILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"[{args.mode}] another tick is running — exiting")
        return

    try:
        if args.mode == "preflight":
            run_preflight()
        else:
            run_tick()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
