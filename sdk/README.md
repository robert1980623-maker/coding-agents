# coding-agents-sdk

Async Python SDK for the [coding-agents](https://github.com/coding-agents/coding-agents) HTTP API.

> ⚠️ **This SDK is a pure HTTP wrapper. It does NOT trigger session execution.**
> `create_session()` returns a session in `pending` status; an external executor
> is required to actually run the agent. See
> [`docs/INTEGRATION.md`](../openclaw_integration/docs/INTEGRATION.md) for the
> full contract.

## Features

- **Async-only** — built on `httpx.AsyncClient`, no sync wrappers
- **Pure HTTP** — no subprocess management, no execution semantics
- **Pydantic models** — strongly typed `Session`, `Event`, `Tag`, …
- **SSE streaming** — `async for event in client.stream_events(...)`
- **Zero coupling** — independent Pydantic models; does not import the server's internals

## Installation

```bash
# From the repo root
pip install -e ./sdk

# Or once published:
pip install coding-agents-sdk
```

Requires Python ≥ 3.12.

## Quick start

```python
import asyncio
from coding_agents_sdk import AsyncCodingAgentClient


async def main() -> None:
    async with AsyncCodingAgentClient(
        base_url="http://localhost:8765",
        token="my-secret-token",
    ) as client:
        # 1. Create a session (PENDING — does NOT execute)
        session = await client.create_session(
            agent="claude",
            prompt="refactor auth.py",
            workdir="/repo",
            metadata={"trace_id": "abc"},
        )
        print(f"Created session {session.session_id} (status={session.status})")

        # 2. Poll status (or use stream_events)
        current = await client.get_session(session.session_id)
        print(f"Current status: {current.status}")

        # 3. Attach a tag
        await client.create_tag(session.session_id, "important")

        # 4. Kill if needed
        # await client.kill(session.session_id)


asyncio.run(main())
```

## Streaming events

```python
async with AsyncCodingAgentClient(base_url="...") as client:
    # last_event_id enables resumption
    async for event in client.stream_events(session_id, last_event_id=0):
        print(f"[seq={event.seq}] {event.type}: {event.data}")
        if event.type == "result":
            break
```

The server emits `text/event-stream` over `GET /sessions/{id}/events/stream`.
The SDK transparently parses `data: <json>` lines and decodes the JSON payload.

## API reference

### `AsyncCodingAgentClient`

| Method | HTTP | Notes |
| --- | --- | --- |
| `create_session(agent, prompt="", *, workdir=".", metadata=None)` | `POST /sessions` | Returns a `pending` session. Does NOT execute. |
| `get_session(session_id)` | `GET /sessions/{id}` | |
| `list_sessions(*, agent=None, status=None, tag=None, limit=100)` | `GET /sessions` | |
| `get_events(session_id, *, after_seq=0, limit=None)` | `GET /sessions/{id}/events` | JSON-decodes `data` when possible. |
| `stream_events(session_id, *, last_event_id=None)` | `GET /sessions/{id}/events/stream` | Async iterator over `Event`. |
| `kill(session_id)` | `POST /sessions/{id}/kill` | Only effective for `pending`/`running`. |
| `recover(*, timeout_seconds=300)` | `POST /recover` | Marks orphans. |
| `create_tag(session_id, tag)` | `POST /sessions/{id}/tags` | Body: `{"tag": "..."}` |
| `list_tags(session_id)` | `GET /sessions/{id}/tags` | Returns `list[str]`. |
| `delete_tag(session_id, tag)` | `DELETE /sessions/{id}/tags/{tag}` | |
| `health()` | `GET /health` | |
| `metrics()` | `GET /metrics` | Returns raw Prometheus text. |

### Context manager

The client is an async context manager and owns its internal `httpx.AsyncClient` by default. Pass an externally-managed client via `client=...` to share it (e.g. across tests):

```python
async with httpx.AsyncClient(timeout=10) as http:
    client = AsyncCodingAgentClient(client=http, token="...")
    # SDK will NOT close `http` on exit
```

### Errors

| Exception | HTTP |
| --- | --- |
| `AuthenticationError` | 401 |
| `NotFoundError` | 404 |
| `ServerError` | 5xx |
| `APIError` | other 4xx |
| `ConnectionError_` | transport failure (timeout, refused, …) |

All exceptions expose `.status_code`, `.detail`, and `.response_body` where applicable.

## Testing

```bash
cd sdk
pytest tests/ -v
```

Tests use `httpx.MockTransport` — no real HTTP server is required.

## Why a thin wrapper?

The HTTP API is the source of truth. Wrapping it thinly means:

- SDK breakage = server breakage (and vice versa)
- No dual maintenance
- Easy to swap implementations in tests
- OpenClaw and Hermes can each use their own SDK conventions on top

If you need higher-level helpers (e.g. "wait until completed"), build them in a
separate `coding_agents_sdk.highlevel` module — never inside this core client.

## License

MIT