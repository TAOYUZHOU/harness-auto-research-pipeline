#!/usr/bin/env bash
# harp_polish.sh — translate + summarise log.md / memory.md / plan.md
# into Chinese Markdown via a fresh cursor-agent chat.
#
# Output goes to <WORK_DIR>/.state/zh/{log,memory,plan}.zh.md so the
# original files are untouched and the engine's scan_state ignores them.
#
# Each call uses NO --resume → brand-new context window per polish,
# isolated from the main HARP iteration chat.  Token cost is recorded
# alongside iteration cost in .state/usage.jsonl with mode=polish_zh.
#
# Usage:
#   bash harp_polish.sh --once               # all 3 files, only if changed
#   bash harp_polish.sh --once --file log    # only log.md
#   bash harp_polish.sh --once --force       # ignore mtime cache, re-polish
#   bash harp_polish.sh --once --dry-run     # show prompt, don't call agent
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
ENGINE_DIR="$(dirname "$SKILL_DIR")"

export PATH="$HOME/.local/bin:$PATH"

mode_once=0
which_file="all"
force=0
dry_run=0
while [ $# -gt 0 ]; do
  case "$1" in
    --once)    mode_once=1; shift ;;
    --file)    which_file="$2"; shift 2 ;;
    --force)   force=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[ "$mode_once" -eq 1 ] || { echo "ERROR: pass --once (the daemon is harp_polish_daemon.sh)" >&2; exit 2; }

# Resolve workspace via meta_info
WORK_DIR=$(python3 - "$ENGINE_DIR/meta_info/project.yaml" <<'PY'
import sys, yaml, pathlib
print(yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())["harness"]["workspace"]["dir"])
PY
)
[ -d "$WORK_DIR" ] || { echo "ERROR: workspace not found: $WORK_DIR" >&2; exit 1; }

ZH_DIR="$WORK_DIR/.state/zh"
mkdir -p "$ZH_DIR"

# ── Build a per-file polish prompt ────────────────────────────────
build_prompt() {
  local file_kind="$1" content="$2"
  case "$file_kind" in
    log)
      cat <<EOF
你是 HARP 项目的中文润色助手。下面是 HARP 引擎的 log.md 原文,
每行格式: TS=...;PLAN=...;ANCHOR=...;AXIS=...;TEST_MAE=...;BEST_VAL_MAE=...;STATUS=...;GIT=...;HP=...

请把它翻译并润色成中文要点 Markdown,要求:
1. 保留 ANCHOR / PLAN / BEST_VAL_MAE / STATUS / GIT 字段原文 (它们是 grep key,不要翻译)。
2. 每行原文产出 1-2 行中文,说清这一 tick 发生了什么 (新结果? plateau? 弃训? 阈值达成?)。
3. 用 Markdown 列表,按时间倒序 (最新在最上)。
4. 不要凭空增加原文没有的信息。
5. 不要输出任何前置/后置说明,直接出中文 Markdown。

=== log.md 原文 ===
$content
=== 结束 ===
EOF
      ;;
    memory)
      cat <<EOF
你是 HARP 中文润色助手。下面是 memory.md 原文,每个 ### EXP_ID 是一个实验回顾块。

请翻译润色成中文 Markdown,要求:
1. 保留 EXP_ID / ANCHOR / PLAN / BEST_VAL_MAE 等关键字段原文。
2. 把每个实验块的"做了什么 / 结果如何 / 学到什么 / 下一步"四个角度用中文清楚说出来。
3. 保留原文的层级 (### EXP_ID 仍然用 ###)。
4. 不要凭空增加内容。
5. 直接出中文 Markdown,不要前置说明。

=== memory.md 原文 ===
$content
=== 结束 ===
EOF
      ;;
    plan)
      cat <<EOF
你是 HARP 中文润色助手。下面是 plan.md 原文,每个 ### PLAN_ID 是一个实验计划块。

请翻译润色成中文 Markdown,要求:
1. 保留 PLAN_ID / anchor / axis / status / metric / threshold 等字段原文。
2. 把每个 plan 的"动机 / 改动 / 期望 / 当前状态"用中文清楚说出来。
3. 保留 ### PLAN_ID 层级。
4. 不要凭空增加内容。
5. 直接出中文 Markdown。

=== plan.md 原文 ===
$content
=== 结束 ===
EOF
      ;;
  esac
}

# ── Polish one file (single fresh cursor-agent chat) ──────────────
polish_file() {
  local kind="$1"   # log | memory | plan
  local src="$WORK_DIR/${kind}.md"
  local dst="$ZH_DIR/${kind}.md.zh.md"
  local cache="$ZH_DIR/.${kind}.src_sha256"

  [ -f "$src" ] || { echo "  skip $kind: $src not found"; return 0; }

  local sha
  sha=$(sha256sum "$src" | awk '{print $1}')
  if [ "$force" -eq 0 ] && [ -f "$cache" ] && [ "$(cat "$cache")" = "$sha" ]; then
    echo "  skip $kind: unchanged since last polish (sha matches)"
    return 0
  fi

  local content
  content=$(cat "$src")
  # Truncate excessively large files (cursor-agent has a context limit)
  local max_chars=120000
  if [ "${#content}" -gt $max_chars ]; then
    echo "  WARN $kind: source ${#content} chars > $max_chars, polishing tail only"
    content=$(tail -c $max_chars "$src")
  fi

  local prompt
  prompt=$(build_prompt "$kind" "$content")

  if [ "$dry_run" -eq 1 ]; then
    echo "==[ DRY-RUN $kind ]=================================="
    printf '%s\n' "$prompt" | head -50
    echo "==[ ... ${#prompt} chars total ]====================="
    return 0
  fi

  echo "  polishing $kind ($(printf '%s' "$content" | wc -l) lines, ${#content} chars) ..."

  # Fresh chat: NO --resume.  Output to stream-json so we can record
  # token usage in the same .state/usage.jsonl (mode=polish_zh_$kind).
  local raw="$ZH_DIR/.${kind}.last_stream.jsonl"
  local rc=0
  cursor-agent -p --force \
    --output-format stream-json --stream-partial-output \
    "$prompt" > "$raw" 2>&1 || rc=$?

  # Extract the assistant's final text + usage from the stream.
  python3 - "$raw" "$dst" "$kind" "$WORK_DIR/.state/usage.jsonl" "$WORK_DIR/.state/usage_summary.txt" <<'PY'
import sys, json, pathlib, os
from datetime import datetime, timezone
raw_path, dst_path, kind, usage_jsonl, usage_summary = sys.argv[1:6]
_now = lambda: datetime.now(timezone.utc)
text_chunks, usage, final_text = [], {}, ""
for line in pathlib.Path(raw_path).read_text(errors="ignore").splitlines():
    line = line.strip()
    if not line.startswith("{"): continue
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        continue
    if ev.get("type") == "assistant":
        for c in ev.get("message", {}).get("content", []) or []:
            if c.get("type") == "text" and c.get("text"):
                text_chunks.append(c["text"])
    elif ev.get("type") == "result":
        usage = ev.get("usage", {}) or {}
        if isinstance(ev.get("result"), str):
            final_text = ev["result"]

text = final_text or (max(text_chunks, key=len) if text_chunks else "")
if not text:
    print(f"  ERROR: empty polish output for {kind}; check {raw_path}", file=sys.stderr)
    sys.exit(3)

pathlib.Path(dst_path).write_text(
    f"<!-- auto-generated by harp_polish.sh — do not edit -->\n"
    f"<!-- source: {kind}.md  polished: {_now().strftime('%Y-%m-%dT%H:%M:%SZ')} -->\n\n"
    + text + "\n"
)

if usage:
    rec = {
        "ts": _now().strftime("%Y%m%dT%H%M%SZ"),
        "cycle": 0, "mode": f"polish_zh_{kind}", "timed_out": False,
        "input_tokens": usage.get("inputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "cache_read_tokens": usage.get("cacheReadTokens", 0),
        "cache_write_tokens": usage.get("cacheWriteTokens", 0),
    }
    with open(usage_jsonl, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"  ✓ {kind}: in={rec['input_tokens']} out={rec['output_tokens']} "
          f"cache_r={rec['cache_read_tokens']}")
else:
    print(f"  ✓ {kind}: written (no usage info from agent)")
PY
  local py_rc=$?
  if [ $py_rc -eq 0 ]; then
    echo "$sha" > "$cache"
  fi
  return $py_rc
}

echo "==[ harp_polish ]=================================="
echo "  workspace : $WORK_DIR"
echo "  output    : $ZH_DIR/"
echo

case "$which_file" in
  all)
    polish_file log
    polish_file memory
    polish_file plan
    ;;
  log|memory|plan) polish_file "$which_file" ;;
  *) echo "ERROR: --file must be log|memory|plan|all" >&2; exit 2 ;;
esac

echo
echo "✓ done. View: ls -la $ZH_DIR/"
