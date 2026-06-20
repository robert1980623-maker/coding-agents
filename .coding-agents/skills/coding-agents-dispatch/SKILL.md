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

# 3. With budget cap
coding-agents dispatch codex "add unit tests" --workdir ~/projects/foo --budget 2.0

# 4. Model override
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
3. **Use `--budget`** for any non-trivial task. It caps runaway cost
   and is enforced at the agent CLI level (`--max-budget-usd`).
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