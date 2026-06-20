# Coding Agent Runtime

A unified, high-performance runtime for managing coding agents (Claude Code, Codex, etc.).

## Features

- **Unified Interface**: One API to call Claude Code, Codex, and more agents
- **Streaming Output**: Real-time streaming of agent output via stdout/stderr
- **Session Management**: Track execution sessions with tags, search, and recovery
- **Full-Text Search**: FTS5-powered search across all agent output
- **Crash Recovery**: Automatic detection and recovery of orphaned sessions
- **Concurrency Control**: Semaphore-based limit on concurrent agent executions

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

# Run tests with coverage
uv run pytest tests/ -v --cov=coding_agents --cov-report=term-missing
```

## License

MIT
