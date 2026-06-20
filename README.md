# Coding Agent Runtime

A unified, high-performance runtime for managing coding agents (Claude Code, Codex, etc.).

## Features

- **Unified Interface**: One API to call Claude Code, Codex, and more agents
- **Streaming Output**: Real-time streaming of agent output via stdout/stderr
- **Session Management**: Track execution sessions with tags, search, and recovery
- **Full-Text Search**: FTS5-powered search across all agent output
- **Crash Recovery**: Automatic detection and recovery of orphaned sessions
- **Concurrency Control**: Semaphore-based limit on concurrent agent executions
- **HTTP API**: 12 REST endpoints + SSE event streaming
- **Python SDK**: Async-only client for OpenClaw / Hermes / any async Python host
- **OpenClaw Integration**: Example scripts + integration guide
- **Project-local skills**: SKILL.md catalog under `.coding-agents/skills/`
  (agentskill.io standard) discoverable by Claude Code / Codex natively

## Installation

```bash
# Using uv (recommended)
uv sync

# Using pip
pip install -e .
```

## Usage

### Run an agent

```bash
# Run Claude Code
coding-agents run claude "refactor this function" --workdir ~/project

# Run Codex
coding-agents run codex "add tests" --workdir ~/project

# Run with custom model
coding-agents run claude "optimize this" --model claude-sonnet-4-20250514

# Run with budget limit
coding-agents run claude "rewrite module" --budget 5.0
```

### Manage sessions

```bash
# List all sessions
coding-agents list

# Filter by agent and status
coding-agents list --agent claude --status completed

# Filter by tag
coding-agents list --tag important

# View session status
coding-agents status <session-id>

# Kill a running session
coding-agents kill <session-id>
```

### Tags

```bash
# Add a tag
coding-agents tag <session-id> important

# Remove a tag
coding-agents tag -r <session-id> important
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
root. **They are not auto-injected** — each agent discovers them
natively (see v0.2.4 release notes).

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
commit it.

## Architecture

```
CLI → SessionRegistry (concurrency control) → StreamExecutor → Agent Adapter
                                          → StorageBackend (SQLite)
```

- **SessionRegistry**: Semaphore-based concurrency control with 60s queue timeout
- **StreamExecutor**: Async subprocess management with streaming output
- **StorageBackend**: Protocol-based storage with SQLite implementation
- **Agent Adapters**: Claude Code and Codex CLI wrappers

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
