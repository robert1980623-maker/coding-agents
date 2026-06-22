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
- **High-level helpers** — `wait_for_completion` and `watch_session` for polling loops
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

        # 2. Wait for it to finish (polls every 5 min by default)
        final = await client.wait_for_completion(
            session.session_id,
            poll_interval=10.0,  # check more often for demo purposes
        )
        print(f"Final status: {final.status}")


asyncio.run(main())
```

## Configuration

`AsyncCodingAgentClient` accepts the following arguments at construction time:

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `base_url` | `str` | `http://localhost:8765` | Root URL of the coding-agents server |
| `token` | `str \| None` | env `CODING_AGENTS_TOKEN` | Bearer token for authentication |
| `timeout` | `float` | `30.0` | Per-request timeout in seconds |
| `client` | `httpx.AsyncClient \| None` | `None` | Externally-managed HTTP client (optional) |

The client is an async context manager and owns its internal `httpx.AsyncClient` by default. Pass an externally-managed client via `client=...` to share it (e.g. across tests):

```python
async with httpx.AsyncClient(timeout=10) as http:
    client = AsyncCodingAgentClient(client=http, token="...")
    # SDK will NOT close `http` on exit
```

## API reference

### Sessions

| Method | HTTP | Notes |
| --- | --- | --- |
| `create_session(agent, prompt="", *, workdir=".", metadata=None)` | `POST /sessions` | Returns a `pending` session. Does NOT execute. |
| `get_session(session_id)` | `GET /sessions/{id}` | |
| `list_sessions(*, agent=None, status=None, tag=None, limit=100)` | `GET /sessions` | |

### Events

| Method | HTTP | Notes |
| --- | --- | --- |
| `get_events(session_id, *, after_seq=0, limit=None)` | `GET /sessions/{id}/events` | JSON-decodes `data` when possible. |
| `stream_events(session_id, *, last_event_id=None, timeout=None)` | `GET /sessions/{id}/events/stream` | Async iterator over `Event`. |

### Actions

| Method | HTTP | Notes |
| --- | --- | --- |
| `kill(session_id)` | `POST /sessions/{id}/kill` | Only effective for `pending`/`running`. |
| `recover(*, timeout_seconds=300)` | `POST /recover` | Marks orphans. |

### Tags

| Method | HTTP | Notes |
| --- | --- | --- |
| `create_tag(session_id, tag)` | `POST /sessions/{id}/tags` | Body: `{"tag": "..."}` |
| `list_tags(session_id)` | `GET /sessions/{id}/tags` | Returns `list[str]`. |
| `delete_tag(session_id, tag)` | `DELETE /sessions/{id}/tags/{tag}` | |

### High-level helpers

| Method | Notes |
| --- | --- |
| `wait_for_completion(session_id, *, poll_interval=300.0, timeout=3600.0)` | Block until terminal state (`completed`/`failed`/`killed`/`timeout`). Returns final `Session`. Raises `TimeoutError` on deadline. |
| `watch_session(session_id, *, poll_interval=300.0, timeout=3600.0)` | Async iterator yielding the `Session` on every status change until terminal state. |

### Health

| Method | HTTP | Notes |
| --- | --- | --- |
| `health()` | `GET /health` | Returns `HealthStatus`. |
| `metrics()` | `GET /metrics` | Returns raw Prometheus text. |

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

## Error handling

All errors derive from `CodingAgentsSDKError`. Catch specific subclasses for finer-grained handling:

```python
from coding_agents_sdk import (
    AsyncCodingAgentClient,
    APIError,
    AuthenticationError,
    NotFoundError,
    ServerError,
    ConnectionError_,
)

async with AsyncCodingAgentClient(base_url="...") as client:
    try:
        session = await client.get_session("does-not-exist")
    except AuthenticationError as e:
        print(f"Bad token: {e.detail}")            # 401
    except NotFoundError as e:
        print(f"Not found: {e.detail}")            # 404
    except ServerError as e:
        print(f"Server error: {e.status_code}")    # 5xx
    except APIError as e:
        print(f"API error: {e.status_code} — {e.detail}")  # other 4xx
    except ConnectionError_ as e:
        print(f"Transport failure: {e}")           # timeout / refused
```

Every exception exposes `.status_code`, `.detail`, and `.response_body` where applicable.

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
