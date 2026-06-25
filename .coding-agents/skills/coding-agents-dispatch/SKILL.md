---
name: coding-agents-dispatch
description: |
  How to correctly dispatch a coding-agents session for a project task.
  Use this skill when you need to run a Claude Code or Codex agent
  via the coding-agents runtime, with the right working directory
  and cost controls. v0.2.6+: dispatch output is bounded — use
  `tail` / `status` to read intermediate events.
  v0.2.17+: use `dispatch-bg` for OpenClaw/exec wrappers (fire-and-forget).
---

# Coding Agents — Dispatch

## When to use this skill

- You have a coding task that should be delegated to Claude Code or Codex
- You want it to run inside a specific project directory (so it sees the
  project's `AGENTS.md` / `CLAUDE.md` / `.claude/skills/`)
- You want cost control (budget) or resumable sessions via SQLite
- You need OpenClaw-safe output (no 1MB buffer overflow)
- You want idle timeout protection (v0.2.29+)

## dispatch vs dispatch-bg

| Scenario | Use |
| --- | --- |
| Human runs from terminal | `dispatch` (blocking, result inline) |
| Agent / cron / orchestrator calls | **`dispatch-bg`** (fire-and-forget, <1s reply) |
| Task < 30s and need result NOW | `dispatch` (with short prompt) |

**Use `dispatch-bg` when calling from inside OpenClaw/exec wrappers**.
OpenClaw's `exec` tool has a 30s timeout. `dispatch-bg` returns the
`session_id` within ~1 second, then the agent runs in a detached subprocess.
The wrapper exits immediately; query `status <id>` to inspect progress.

See `coding-agents dispatch-bg --help` for all available flags.

## Default: dispatch without model or budget

**The recommended form is the simplest one.** Do not pass
`--model`, `--budget`, or `--max-budget-usd` unless the human
explicitly asks for a specific value.

Why:
- The dispatched agent reads `AGENTS.md` / `CLAUDE.md` in the
  `--workdir` and uses the model + provider configured there. Those
  defaults reflect the human's actual subscription / API setup.
- The dispatcher (PM, CI, OpenClaw) usually does **not** know which
  model or budget the human wants. Picking one is guessing, and
  guessing wrong wastes the run.
- `--budget` only works for `claude` (passed through as
  `--max-budget-usd`). It is silently ignored for `codex` (v0.2.8+).

Passing model/budget without being asked is the most common
unnecessary-flags mistake. The agent will keep running either way —
you just have to re-dispatch when you picked wrong.

## How to dispatch

The canonical command is `coding-agents dispatch`. It is installed
globally as `/Users/rowang/.local/bin/coding-agents` (via `uv tool
install`); `which coding-agents` should return that path.

If `coding-agents` is not on your PATH, install it once:

```bash
cd /Users/rowang/projects/coding-agents && uv tool install .
```

Canonical commands:

```bash
# 1. Simplest: current directory
coding-agents dispatch claude "refactor auth.py"

# 2. Explicit workdir (RECOMMENDED for project tasks)
coding-agents dispatch claude "fix the race condition" --workdir ~/projects/foo

# 3. Advanced: budget cap (only when the human asks for one)
coding-agents dispatch codex "add unit tests" --workdir ~/projects/foo --budget 2.0

# 4. Advanced: model override (only when the human asks for a specific model)
coding-agents dispatch claude "optimize hot path" --model claude-sonnet-4-20250514
```

## Output contract (v0.2.6+)

`dispatch` **never streams intermediate stdout/stderr to the CLI**.
This is intentional — OpenClaw `exec` has a 1MB stdout buffer that
overflows on chatty agents (long tasks, big test suites).

What `dispatch` writes:
- **Early**: one line `session_id=<id>` (so you can poll even if the
  rest gets truncated)
- **End**: one JSON result line:
  ```json
  {"session_id": "...", "exit_code": 0, "error": null}
  ```

Everything else (every stdout/stderr chunk) goes into SQLite. Read it
later with `status` or `tail`:

```bash
SID=...  # from dispatch output
coding-agents status $SID          # metadata + last 20 events (~4KB)
coding-agents tail $SID            # last 100 events (~20KB)
coding-agents tail $SID --limit 500  # bigger window
```

## Hard rules

1. **Always pass `--workdir`** when the task belongs to a project.
   The agent subprocess is started in that directory; without it the
   agent can't see your project's conventions or local skills.
2. **Never inject skill lists** into the agent's prompt. Claude Code
   and Codex each discover skills natively (`~/.claude/skills/`,
   `AGENTS.md`, etc.). Forcing a list will compete with their native
   discovery and often mislead them.
3. **Do NOT pass `--model` / `--budget` / `--max-budget-usd`**
   unless the human explicitly asks for a specific value. The agent
   uses project defaults from `AGENTS.md` / `CLAUDE.md`. See the
   "Default: dispatch without model or budget" section above.
4. **Do NOT use `--stream`** — it was removed in v0.2.6 because
   it overflowed the OpenClaw exec buffer. Read events from SQLite
   instead (`status` / `tail`).

## Concurrency & timeout (CORE strategy, v0.2.29+)

Two non-negotiable rules, agreed with the model provider (DashScope
Alibaba proxy) to avoid 429 quota errors:

### 1. **Never dispatch in parallel** — run sequentially

```bash
# ❌ DO NOT do this
coding-agents dispatch-bg claude "task A" --workdir /path &
coding-agents dispatch-bg claude "task B" --workdir /path &

# ✅ DO this — chain with &&
coding-agents dispatch-bg claude "task A" --workdir /path
coding-agents status <A-id>   # wait for A to complete
coding-agents dispatch-bg claude "task B" --workdir /path
```

Why:
- The DashScope proxy has hard concurrency limits; parallel dispatches
  collide and return 429 / "Not logged in"
- Single-stream is also easier to debug and reason about
- For multi-task work, prefer breaking it into clear, ordered phases
  rather than racing them

### 2. **Keep tasks ≤ 10 minutes**

```bash
# ✅ Default timeout is 10 min — fine for most tasks
coding-agents dispatch-bg claude "fix the race condition" --workdir /path

# ⚠️ Long analysis? Add --idle-timeout (Codex may go silent on big files)
coding-agents dispatch-bg codex "analyze the whole repo" --workdir /path --idle-timeout 900

# ❌ DO NOT do this — a single 60-minute task
coding-agents dispatch-bg claude "rewrite the entire codebase"
```

If a task would exceed 10 minutes:
- **Split it** into smaller, scoped dispatches (preferred)
- Add `--idle-timeout` to defend against silent stalls
- Do NOT raise the cap; long tasks are usually a sign the prompt
  is too broad or the work is not yet understood

### Why these two rules together

The 10-minute cap + sequential execution means a typical
"refactor / test / commit" cycle is one or two dispatches, never
a firehose. This matches the provider's quota model and avoids
the cascading failures we saw in 2026-Q2.

### 3. **Polling interval must be > 5 minutes** (v0.2.30+)

Same provider-quota reason. If you poll `status <id>` or `watch <id>`
in a tight loop, you burn API quota the agent itself is using.

```bash
# ❌ DO NOT do this — 2s sleep hammers the provider
while true; do coding-agents status <id>; sleep 2; done

# ❌ DO NOT do this — even 1 minute is too short
while true; do coding-agents status <id>; sleep 60; done

# ✅ DO this — >= 5 min, default is 10 min
coding-agents watch <id>                    # default --interval 600
coding-agents watch <id> --interval 300     # minimum allowed

# ✅ Manual loop with proper spacing
while true; do coding-agents status <id>; sleep 600; done
```

The `watch` command enforces `--interval >= 300` (5 min) and
defaults to 600 (10 min). If you find yourself wanting shorter
intervals, the task is too long — split it.

## Common mistakes to avoid

| ❌ Don't | ✅ Do |
| --- | --- |
| `exec: claude -p "fix bug"` directly | `coding-agents dispatch claude "fix bug" --workdir ~/project` |
| Forget `--workdir`, agent runs in $HOME | Always pass the project root |
| Append "available skills: ..." to the prompt | Let the agent discover them natively |
| `coding-agents run ...` (deprecated) | `coding-agents dispatch ...` |
| Use `--stream` to see live output | Read from SQLite with `tail` / `status` |
| Poll `tail --follow` from inside OpenClaw exec | Use a one-shot `status` call |
| Add `--model <name>` because it "feels safer" | Use the agent's default from `AGENTS.md` |
| Add `--budget 5` "just in case" | Only set `--budget` when the human asks for a cap |

## What coding-agents does for you (for free)

- Persists the session in `~/.coding-agents/data.db` (resume / kill / search)
- **Bounded CLI output** — dispatch can run inside OpenClaw safely
- Tags + FTS5 search across all events
- Crash recovery: orphaned sessions can be picked up with `coding-agents recover`
- Cost tracking: each `result` event is parsed for tokens + USD
- `gc` for cleaning up old sessions

## Related skills

- `coding-agents-lifecycle` — `status` / `tail` / `gc` for inspecting & cleaning up
- `coding-agents-recovery` — how to recover from crashes / orphans
- `coding-agents-cost` — how to estimate and cap costs
- `coding-agents-skills` — how to manage skill directories (CLI)
## Keep prompts short — describe problems, not solutions

A good dispatch prompt is **a problem statement and acceptance criteria**,
not a full implementation blueprint. Concretely:

- ✅ Include: background / root cause, goal, constraints, workdir, agent type,
  acceptance criteria (what counts as "done").
- ❌ Exclude: complete code templates, function signatures, exact commit
  message text, step-by-step rewrites of files the agent hasn't read yet.

Why this matters:

- Long prompts eat the agent's context window. A 10 KB prompt can push
  the agent's first-turn context close to its limit, making later
  tool calls (which append to context) fail.
- Agents design better code than copy-pasted templates. They're trained
  on patterns, not the specific code you have in mind.
- The PM is the wrong layer to design the implementation. PM designs
  the goal and constraints; the agent picks the implementation.
- Commit messages, notification routes, and verification commands are
  conventions the agent already knows (from `AGENTS.md` / `CLAUDE.md` /
  skills) — they don't belong in the dispatch prompt.

### Bad prompt (do not do this)

```markdown
# VNPY Phase 2: 统一 DataDownloader + 并发下载

### Task 1: 创建统一 DataDownloader 类
新建 `examples/alpha_research/data_downloader.py`：
```python
[150 行完整代码，含 dataclass / ThreadPoolExecutor / 失败队列]
```

### Task 2: 重构 batch_download_enhanced.py
1. 在文件顶部 `from data_downloader import DataDownloader, ...`
2. 删除 `download_with_tushare()` 和 `download_with_akshare()` 中的 subprocess.run
3. 改为：
```python
def download_with_tushare(stock_code):
    """使用 Tushare Pro 下载（进程内调用）"""
    from download_data_akshare import get_stock_bars_tushare
    df = get_stock_bars_tushare(...)
    ...
```
```

(~11.5 KB, includes code, function signatures, verification commands,
commit message, notification routing — way too much.)

### Good prompt (do this)

```markdown
# VNPY Phase 2: 统一 DataDownloader + 并发下载

## 问题
batch_download_enhanced.py 用 subprocess.run 下载每只股票 (200 次进程启动浪费)

## 目标
1. 创建 data_downloader.py 提供 DataDownloader 类（直接 import download_data_akshare 的函数，不开子进程）
2. 重构 batch_download_enhanced.py 用 DataDownloader（保留命令行接口）
3. 验证 import 成功 + py_compile 通过
4. commit（不 push）

## 约束
- workdir: /Users/rowang/projects/vnpy
- 不修改 download_data_akshare.py

读完 AGENTS.md / CLAUDE.md 后再开始。
```

(~500 B, just problem + goal + constraints + acceptance criteria.)

### Size guideline

| Prompt length | Verdict |
| --- | --- |
| < 1 KB | Ideal |
| 1-3 KB | OK for complex multi-file work |
| 3-6 KB | Suspicious — re-check if you can cut |
| > 6 KB | Almost certainly too much — split into multiple dispatches |

---

## v0.2.17+: `dispatch-bg` for fire-and-forget dispatch

**Use `dispatch-bg` instead of `dispatch` when calling from inside an agent / cron / orchestrator.**

Why: OpenClaw's `exec` tool has a `tools.exec.timeoutSec: 30` default. A long-running agent task
dispatched via plain `dispatch` gets killed after 30s (exit_code=-1, metadata=`wrapper terminated`).
The agent subprocess may still complete in the background, but you can't reliably wait for it
from inside a 30s exec call.

`dispatch-bg` solves this:

- Returns session_id within ~1 second (way under the 30s timeout)
- Spawns a detached runner subprocess (`start_new_session=True`) that owns the agent subprocess
- The dispatch wrapper exits immediately; the runner runs independently
- All events still go to SQLite — query with `status <id>` / `tail <id>`

### When to use which

| Scenario | Use |
| --- | --- |
| Human runs from a long-lived terminal | `dispatch` (blocking, get result inline) |
| Agent / cron / orchestrator calls | **`dispatch-bg`** (fire-and-forget) |
| You need a result NOW and the task is < 30s | `dispatch` (with short prompt) |

### `dispatch-bg` usage

```bash
# Returns within ~1s with session_id
coding-agents dispatch-bg claude "<prompt>" --workdir /path/to/project

# With idle timeout (default: 300s, v0.2.29+)
coding-agents dispatch-bg claude "<prompt>" --workdir /path/to/project --idle-timeout 900

# Output (always 2 lines, < 1KB):
session_id=<uuid>
{"session_id": "...", "status": "running"}

# Then poll progress
coding-agents status <id>
coding-agents tail <id> --limit 20
```

> **Idle timeout** (v0.2.29+): Use `--idle-timeout N` to kill sessions that
> haven't produced output for N seconds. This prevents silent hangs. Default
> is 300 seconds (5 minutes).

### Why both commands still exist

- `dispatch` — back-compat with v0.2.6+ semantics; safe for human-driven terminal use
- `dispatch-bg` — new in v0.2.17; safe for agent/orchestrator use

The CLI wrapper that runs `dispatch-bg` exits before any agent work begins, so the OpenClaw 30s
timeout never sees the long-running agent process.

