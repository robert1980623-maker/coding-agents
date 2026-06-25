# Coding Agent Runtime

A unified, high-performance runtime for managing coding agents (Claude Code, Codex, etc.).

## Features

- **Unified Interface**: One API to call Claude Code, Codex, and more agents
- **Bounded Output**: Dispatch emits only `session_id` + JSON result — safe for the OpenClaw exec 1MB stdout buffer (v0.2.6+)
- **Session Management**: Track execution sessions with tags, search, and recovery
- **Native Session Resume**: Captures Claude Code / Codex native session IDs and uses them on `resume` for true conversation continuation (v0.2.29+)
- **Full-Text Search**: FTS5-powered search across all agent output
- **Auto-cleanup**: Automatic detection and cleanup of stuck sessions (pending > 2min → failed, running > 24h no heartbeat → orphaned) (v0.2.32+)
- **Fire-and-forget Mode**: `dispatch-bg` returns session_id in <1s for use with OpenClaw/exec wrappers (v0.2.17+)
- **Fleet Health Monitoring**: `poll` command shows one-line status overview for all active sessions (v0.2.31+)
- **Concurrency Control**: Semaphore-based limit on concurrent agent executions
- **Garbage Collection**: `gc` cleans up old sessions to bound SQLite size (v0.2.6+)
- **Idle Timeout Protection**: `--idle-timeout` flag prevents sessions from running indefinitely (v0.2.29+)
- **Ghost Session Prevention**: Auto-cleanup and health checks for pending/running sessions (v0.2.32+)
- **HTTP API**: REST endpoints + SSE event streaming
- **Python SDK**: Async-only client for OpenClaw / Hermes / any async Python host
- **OpenClaw Integration**: Example scripts + integration guide
- **Project-local skills**: SKILL.md catalog under `.coding-agents/skills/`
  (agentskill.io standard) discoverable by Claude Code / Codex natively
- **Version Flag**: `--version` / `-v` shows coding-agents X.Y.Z (v0.2.33+)

## Installation

```bash
# Global install (recommended for dispatch / OpenClaw use)
uv tool install /Users/rowang/projects/coding-agents

# Local dev install
uv sync
```

## Usage

### Dispatch an agent

**For OpenClaw/exec wrappers, use `dispatch-bg`** — it returns in ~1 second
instead of waiting for the agent to complete (which may exceed the 30s
wrapper timeout).

```bash
# Recommended for wrappers (returns instantly with session_id)
coding-agents dispatch-bg claude "fix the race condition" --workdir ~/projects/foo

# Or use dispatch for blocking execution from a terminal
coding-agents dispatch claude "fix the race condition" --workdir ~/projects/foo
```

The canonical command is `dispatch`. Output is bounded — safe for
OpenClaw exec, where stdout/stderr is capped at 1MB.

```bash
# Run Claude Code (default workdir: cwd)
coding-agents dispatch claude "refactor this function"

# Run with explicit workdir (RECOMMENDED — agent sees project AGENTS.md)
coding-agents dispatch claude "fix the race condition" --workdir ~/projects/foo

# Run Codex
coding-agents dispatch codex "add tests" --workdir ~/projects/foo

# Custom model
coding-agents dispatch claude "optimize this" --model claude-sonnet-4-20250514

# Budget cap (claude only — codex ignores it with a warning, v0.2.9+)
coding-agents dispatch claude "rewrite module" --budget 5.0

# Idle timeout (prevents sessions from running forever, v0.2.29+)
coding-agents dispatch claude "analyze large codebase" --idle-timeout 900
```

> v0.2.6+: dispatch never streams intermediate output. It prints
> `session_id=<id>` early and one JSON result line at the end.
> All stdout/stderr events are stored in SQLite — read them with
> `status` / `tail`.

### Inspect a session

```bash
# Session metadata + last 20 events (~4KB, OpenClaw-safe)
coding-agents status <session-id>

# More events
coding-agents status <session-id> --limit 100

# Metadata only (no events)
coding-agents status <session-id> --no-events

# Tail (default 100 events, oldest-first within the window)
coding-agents tail <session-id>
```

> v0.2.29+: `--idle-timeout` (default: 300s) kills sessions with no output
> for the configured duration. Use longer timeouts for long-running analysis.

> v0.2.32+: `status` and `poll` automatically clean stuck sessions by default
> (pending > 2min → failed, running > 24h no heartbeat → orphaned). Disable
> with `--no-auto-clean` if needed.

### Resume a session (v0.2.29+)

Resume continues a session from its native agent state. coding-agents captures
the agent's native session ID (`init.session_id` for Claude Code,
`thread.started.thread_id` for Codex) automatically and re-uses it on resume
instead of inventing a new ID. This makes resumes actually continue the original
agent conversation.

```bash
# Resume from the last event
coding-agents resume <session-id>

# Resume with a follow-up prompt
coding-agents resume <session-id> "and add tests for the edge cases"
```

Resume is only available for terminal sessions (`completed`, `killed`, `timeout`)
with exit code 0. Sessions with non-zero exit code (crashed agents) cannot be
resumed — their internal state is unreliable.

Behind the scenes:
- Claude Code: `claude code --resume <native_session_id>`
- Codex: `codex exec resume <native_thread_id>`
- Fallback: when no native ID is available (older session, or pre-v0.2.29
  data), coding-agents falls back to the session's own UUID for Claude, or
  `--resume-from <seq>` for Codex.

The captured native ID is stored in session metadata as `native_session_id`.
You can verify it via `coding-agents status <session-id> --no-events`.

### Garbage-collect old sessions

```bash
# Defaults: 30 days completed, 7 days failed, 24h running → orphaned
coding-agents gc

# Dry-run
coding-agents gc --dry-run

# Aggressive: drop stdout/stderr, keep only result events
coding-agents gc --keep-result-only
```

### Auto-cleanup vs garbage collection

- **Auto-cleanup** (v0.2.32+, `poll` and `status`): Immediate cleanup of stuck
  sessions that are blocking the system (pending > 2min, running > 24h no heartbeat)
- **Garbage collection** (`gc`): Cleanup of old completed/failed sessions to
  reclaim disk space (default: 30 days for completed, 7 days for failed)

Run `gc` periodically (e.g., weekly). Auto-cleanup runs automatically on
session inspection commands.

### List / filter / kill

```bash
# List sessions
coding-agents list
coding-agents list --agent claude --status completed
coding-agents list --tag important

# Kill a running session
coding-agents kill <session-id>
```

### Tags

```bash
coding-agents tag <session-id> important      # add
coding-agents tag -r <session-id> important   # remove
```

### Search

```bash
# Full-text search across all events
coding-agents search "refactor"
```

### Recovery

```bash
# Scan for orphaned sessions (heartbeat timeout)
coding-agents recover
```

> **Note**: `status` and `poll` automatically detect and clean stuck sessions
> by default. Use `coding-agents recover` for a full scanned fix-up of the
> entire database, or when `--auto-clean` has been disabled.

### Auto-cleanup (v0.2.32+)

`poll` and `status` automatically clean stuck sessions by default:

- **Pending sessions > 2min** are marked as `failed`
- **Running sessions with no heartbeat > 24h** are marked as `orphaned`

This prevents ghost sessions from clogging the system. You can disable
auto-cleanup with `--no-auto-clean`:

```bash
coding-agents poll --no-auto-clean      # see stuck sessions without cleanup
coding-agents status <id> --no-auto-clean
```

### Dispatch in fire-and-forget mode (v0.2.17+)

Use `dispatch-bg` when calling from inside OpenClaw/exec wrappers or orchestrators
with a 30s timeout limit. Unlike `dispatch`, it returns the `session_id` within
~1 second and the actual agent runs in a detached subprocess.

```bash
# Returns instantly with session_id
coding-agents dispatch-bg claude "refactor the auth module" --workdir ~/projects/foo

# Output (always < 1KB):
session_id=abc-123-def
{"session_id": "...", "status": "running"}

# Then poll progress later
coding-agents status <session-id>
```

**When to use which:**

| Scenario | Use |
| --- | --- |
| Human runs from terminal | `dispatch` (blocking, result inline) |
| Agent / cron / orchestrator calls | **`dispatch-bg`** (fire-and-forget) |
| Task < 30s and need result NOW | `dispatch` (with short prompt) |

### Fleet health monitoring (v0.2.31+)

Use `poll` to get a one-line status overview for all active sessions:

```bash
# Default: show running/pending sessions with auto-cleanup
coding-agents poll

# Include all sessions (completed, failed too)
coding-agents poll --all

# Filter by status
coding-agents poll --status running

# Custom stuck threshold (30m default)
coding-agents poll --stuck-after 1h

# Suppress cleanup summary report
coding-agents poll --quiet

# JSON output for programmatic use
coding-agents poll --format json
```

Example output:

```
SESSION ID                           STATUS  AGE      LAST EVENT
abc-123...                           running  5m 32s   stdout (12 events)
def-456...                           pending  1m 15s   started
xyz-789...                           running  24h 3m   heartbeat STUCK (no event in 24h)
```

### Version flag (v0.2.33+)

```bash
coding-agents --version
# coding-agents 0.2.33

coding-agents -v
# coding-agents 0.2.33
```

### HTTP API

```bash
# Start the HTTP server (default: http://127.0.0.1:8765)
coding-agents serve

# Health check
curl http://localhost:8765/health
```

See [`sdk/README.md`](sdk/README.md) for the full endpoint list.

### Python SDK

```bash
pip install -e ./sdk
```

```python
import asyncio
from coding_agents_sdk import AsyncCodingAgentClient


async def main() -> None:
    async with AsyncCodingAgentClient(
        base_url="http://localhost:8765",
        token="my-secret-token",
    ) as client:
        # ⚠️ POST /sessions does NOT trigger execution —
        # an external executor must consume pending sessions.
        session = await client.create_session(agent="claude", prompt="refactor me")
        async for event in client.stream_events(session.session_id):
            print(event.seq, event.type, event.data)
            if event.type == "result":
                break


asyncio.run(main())
```

### OpenClaw integration

Example scripts + integration guide:

- [`openclaw_integration/examples/create_session.py`](openclaw_integration/examples/create_session.py)
- [`openclaw_integration/examples/query_status.py`](openclaw_integration/examples/query_status.py)
- [`openclaw_integration/examples/stream_events.py`](openclaw_integration/examples/stream_events.py)
- [`openclaw_integration/examples/error_handling.py`](openclaw_integration/examples/error_handling.py)
- [`openclaw_integration/docs/INTEGRATION.md`](openclaw_integration/docs/INTEGRATION.md)

### Skills

This repo ships a starter set of skills under `.coding-agents/skills/`
(agentskill.io standard). They are project-local, git-tracked, and
discoverable by Claude Code / Codex when the agent's cwd is this repo
root (see `AGENTS.md` and `.claude/skills/` symlinks at the project root).
**They are not auto-injected** — each agent discovers them natively
(see v0.2.4 release notes).

| Skill | When to use |
| --- | --- |
| `coding-agents-dispatch` | You need to dispatch a coding-agents session for a project task |
| `coding-agents-lifecycle` | You need to inspect a session, read its events, or clean up old data |
| `coding-agents-recovery` | A session is stuck / orphaned; you need to recover (auto-cleanup handles most cases in v0.2.32+) |
| `coding-agents-cost` | You need to budget, monitor, or cap session cost |
| `coding-agents-skills` | You need to install, update, or share a skill |

Inspect with:

```bash
coding-agents skill list
coding-agents skill show <name>
```

To add a new skill, create `.coding-agents/skills/<name>/SKILL.md` and
add a symlink in `.claude/skills/<name>/SKILL.md` (see `.claude/skills/`
for the existing pattern).

## Architecture

```
CLI → SessionRegistry (concurrency control) → StreamExecutor → Agent Adapter
                                          → StorageBackend (SQLite)
```

- **SessionRegistry**: Semaphore-based concurrency control with 60s queue timeout
- **StreamExecutor**: Async subprocess management with bounded CLI output (v0.2.6+)
- **StorageBackend**: Protocol-based storage with SQLite implementation
- **Agent Adapters**: Claude Code (supports `--max-budget-usd`) and Codex CLI (no budget flag)

### Auto-cleanup (v0.2.32+)

When you query a session via `status` or `poll`, the runtime checks:
- If a `pending` session has no heartbeat for > 2 minutes → marks it `failed`
- If a `running` session has no events for > 24 hours → marks it `orphaned`

This prevents ghost sessions from blocking the queue without manual intervention.
To disable (for debugging), use `--no-auto-clean`. Run `gc` regularly to remove
completed/failed sessions older than the retention period (30/7 days).

## Development

```bash
# Install dev dependencies
uv sync --dev

# Run tests
uv run pytest tests/ -v

# Run SDK tests
uv run pytest sdk/tests/ -v

# Run tests with coverage
uv run pytest tests/ -v --cov=coding_agents --cov-report=term-missing
```

## License

MIT
