# Coding Agent Runtime

A unified, high-performance runtime for managing coding agents (Claude Code, Codex, etc.).

## Features

- **Unified Interface**: One API to call Claude Code, Codex, and more agents
- **Bounded Output**: Dispatch emits only `session_id` + JSON result — safe for the OpenClaw exec 1MB stdout buffer (v0.2.6+)
- **Session Management**: Track execution sessions with tags, search, and recovery
- **Full-Text Search**: FTS5-powered search across all agent output
- **Crash Recovery**: Automatic detection and recovery of orphaned sessions
- **Concurrency Control**: Semaphore-based limit on concurrent agent executions
- **Garbage Collection**: `gc` cleans up old sessions to bound SQLite size (v0.2.6+)
- **HTTP API**: REST endpoints + SSE event streaming
- **Python SDK**: Async-only client for OpenClaw / Hermes / any async Python host
- **OpenClaw Integration**: Example scripts + integration guide
- **Project-local skills**: SKILL.md catalog under `.coding-agents/skills/`
  (agentskill.io standard) discoverable by Claude Code / Codex natively

## Installation

```bash
# Global install (recommended for dispatch / OpenClaw use)
uv tool install /Users/rowang/projects/coding-agents

# Local dev install
uv sync
```

## Usage

### Dispatch an agent

`dispatch` is the canonical command. Output is bounded — safe for
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

### Garbage-collect old sessions

```bash
# Defaults: 30 days completed, 7 days failed, 24h running → orphaned
coding-agents gc

# Dry-run
coding-agents gc --dry-run

# Aggressive: drop stdout/stderr, keep only result events
coding-agents gc --keep-result-only
```

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
| `coding-agents-recovery` | A session is stuck / orphaned; you need to recover |
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
