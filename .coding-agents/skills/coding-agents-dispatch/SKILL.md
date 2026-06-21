---
name: coding-agents-dispatch
description: |
  How to correctly dispatch a coding-agents session for a project task.
  Use this skill when you need to run a Claude Code or Codex agent
  via the coding-agents runtime, with the right working directory
  and cost controls. v0.2.6+: dispatch output is bounded — use
  `tail` / `status` to read intermediate events.
---

# Coding Agents — Dispatch

## When to use this skill

- You have a coding task that should be delegated to Claude Code or Codex
- You want it to run inside a specific project directory (so it sees the
  project's `AGENTS.md` / `CLAUDE.md` / `.claude/skills/`)
- You want cost control (budget) or resumable sessions via SQLite
- You need OpenClaw-safe output (no 1MB buffer overflow)

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
