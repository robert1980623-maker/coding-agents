# OpenClaw ↔ coding-agents Integration Guide

This document explains how to wire OpenClaw (or any async Python host) into the
coding-agents HTTP API.

> **Audience**: OpenClaw integrators, OpenClaw plugin authors, and anyone
> building a UI on top of coding-agents.

---

## 1. The contract you must understand

The HTTP API has one **non-obvious** property that every integrator must
internalise before writing code:

> **`POST /sessions` does NOT trigger execution.**
> It creates a session record in `pending` status and returns.
> A separate **executor** must consume pending sessions and drive them to
> completion.

This is documented in the project's design doc (`docs/DESIGN.md`, Phase 2
follow-up: "实际执行集成") and is the deliberate v2 design choice — it lets
the API stay synchronous + idempotent while leaving the execution policy to
the operator.

### What that means for your code

| Caller expectation | Reality |
| --- | --- |
| "I called `create_session()` and 5s later the agent finished" | ❌ Without an executor, the session stays `pending` forever. |
| "I called `create_session()` and got back a session id I can poll" | ✅ This works, as long as *something* is running executors. |
| "I want OpenClaw to drive the execution itself" | ✅ Run `coding-agents run` (CLI) or `StreamExecutor` (lib) as a separate process, then poll the HTTP API for state. |

---

## 2. Topology

```
┌────────────┐    POST /sessions       ┌──────────────────────┐
│  OpenClaw  │ ─────────────────────▶ │  coding-agents HTTP  │
│  (client)  │ ◀───────────────────── │       server          │
└────┬───────┘     201 Created         └──────────┬───────────┘
     │                                          │
     │ GET /sessions/{id}  ◀────────────────────┤
     │ GET /sessions/{id}/events/stream         │
     │                                          ▼
     │                              ┌──────────────────────┐
     │                              │     Executor         │
     │                              │ (StreamExecutor /    │
     │                              │  coding-agents run)  │
     │                              └──────────────────────┘
     │                                          │
     │                                          ▼
     │                              updates SQLite: pending→running→…
     │
     ▼
   SSE events streamed back to OpenClaw
```

The OpenClaw client and the executor can run on the same machine or different
machines — the HTTP server is the only thing they share.

---

## 3. Starting the HTTP server

```bash
# Local (default port 8765, bound to 127.0.0.1)
coding-agents serve

# Custom port / bind
coding-agents serve --host 0.0.0.0 --port 9000

# With a stable auth token (recommended for any non-localhost setup)
coding-agents token rotate  # prints a fresh token to ~/.coding-agents-token
```

The server persists sessions to `~/.coding-agents/data.db` (configurable).

## 4. Running an executor

You need *something* to drive pending sessions. Two options:

### Option A — CLI executor (simplest)

```bash
# Watch the DB and run pending sessions in the background
coding-agents run --watch
```

### Option B — Programmatic executor

```python
import asyncio
from coding_agents.executor import StreamExecutor
from coding_agents.storage.sqlite import SQLiteStorage


async def main() -> None:
    storage = SQLiteStorage("~/.coding-agents/data.db")
    await storage.initialize()
    executor = StreamExecutor(storage=storage)

    while True:
        sessions = await storage.list_sessions(status="pending", limit=1)
        if not sessions:
            await asyncio.sleep(1.0)
            continue
        await executor.run(sessions[0])
```

Either way, the executor flips the session through `running → completed /
failed / killed / timeout`. OpenClaw observes this via the HTTP API.

## 5. Calling the SDK from OpenClaw

```python
import asyncio
from coding_agents_sdk import AsyncCodingAgentClient


async def run_in_openclaw() -> None:
    async with AsyncCodingAgentClient(
        base_url="http://localhost:8765",
        token=openclaw_secret_token,
    ) as client:
        # 1. Create session
        session = await client.create_session(
            agent="claude",
            prompt=openclaw_user_prompt,
            workdir=openclaw_repo_path,
            metadata={"openclaw_user": openclaw_user_id},
        )

        # 2. Stream events (resumable via last_event_id)
        try:
            async for event in client.stream_events(session.session_id):
                await openclaw_emit(event)  # to UI / log / callback
                if event.type == "result":
                    break
        except asyncio.CancelledError:
            # User pressed stop — best-effort kill.
            await client.kill(session.session_id)
            raise
```

## 6. Failure modes & FAQ

### Q: My session is stuck in `pending` forever.

There is no executor running. Start one (see §4).

### Q: How do I know if a session is done?

Poll `GET /sessions/{id}` and check `status ∈ {completed, failed, killed,
timeout, orphaned}`.

### Q: Can I restart a streaming connection mid-session?

Yes — pass `last_event_id` to `stream_events()` and the server replays from
that sequence number. See `examples/stream_events.py` for an auto-resume
wrapper.

### Q: What does 401 mean?

Either the `Authorization: Bearer …` header is missing, or the token doesn't
match `~/.coding-agents-token`. Rotate the token and update your config.

### Q: Does the SDK cache anything?

No. Every call goes straight to the HTTP server. If you need caching, wrap
the SDK in your own layer.

### Q: Can multiple OpenClaw agents share one HTTP server?

Yes. Each agent uses its own bearer token (if you set up multiple tokens) or
shares one. There is no per-call authorization beyond the token.

## 7. Operational checklist

Before going to production:

- [ ] HTTP server bound to a non-loopback interface with TLS termination
- [ ] Auth token stored outside the codebase (env var, secret manager)
- [ ] At least one executor process running with `restart=on-failure`
- [ ] Persistent storage backup for `~/.coding-agents/data.db`
- [ ] OpenClaw client uses exponential backoff for 5xx and connection errors
      (see `examples/error_handling.py`)
- [ ] OpenClaw client cancels the stream and calls `kill()` on user-cancel

## 7.5. Skills (project-local knowledge)

coding-agents ships a set of project-local skills under
`../.coding-agents/skills/` (agentskill.io standard). These are
git-tracked, version-controlled, and discoverable by Claude Code
/ Codex when the agent's cwd is the coding-agents project root.

| Skill | When to use |
| --- | --- |
| `coding-agents-dispatch` | Dispatching a session for a project task |
| `coding-agents-recovery` | Recovering from a stuck / orphaned session |
| `coding-agents-cost` | Budgeting and monitoring session cost |
| `coding-agents-skills` | Managing the skill catalog itself |

**Important**: since v0.2.4 these skills are **not** auto-injected
into the agent's system prompt. Each agent discovers them natively
(Claude Code reads `~/.claude/skills/`, Codex reads `AGENTS.md`).
Forcing a skill list would compete with the agent's own discovery.

### For OpenClaw integrators

When OpenClaw dispatches a coding-agents session, pass `--workdir`
pointing to the coding-agents project root (or whichever project
you want the agent to see). The agent will then read the project's
SKILL.md catalog on its own:

```bash
coding-agents dispatch claude "fix the auth bug" \
  --workdir /path/to/coding-agents
```

You do **not** need to enumerate the skills in the prompt. The
agent will use them when relevant.

If you want a sub-agent to be aware of *how* to dispatch correctly,
point it at the `coding-agents-dispatch` skill in the project, or
include its `description` in the spawn prompt so the sub-agent
knows to read it.

## 8. Where to go next

- [SDK API reference](../../sdk/README.md)
- [Example scripts](../examples/)
- [Design doc §Phase 2](../../docs/DESIGN.md)

---

*Last updated: 2026-06-20.*