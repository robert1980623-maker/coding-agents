# OpenClaw Integration

Example scripts and integration guide for using the
[`coding-agents-sdk`](../sdk/) from OpenClaw (or any async Python host).

> ⚠️ **Important contract**: The HTTP `POST /sessions` endpoint does **not**
> trigger execution. It creates a session in `pending` status and returns.
> An external **executor** must consume pending sessions. See
> [`docs/INTEGRATION.md`](./docs/INTEGRATION.md) for details.

## Layout

```
openclaw_integration/
├── README.md                 ← you are here
├── examples/
│   ├── create_session.py     ← create a session (PENDING — no execution)
│   ├── query_status.py       ← poll until terminal status
│   ├── stream_events.py      ← SSE subscription with auto-resume
│   └── error_handling.py     ← 401/404/5xx/timeout handling
└── docs/
    └── INTEGRATION.md        ← executor contract + full guide
```

## Running the examples

Each script is self-contained and reads configuration from env vars:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CODING_AGENTS_URL` | `http://localhost:8765` | HTTP server root |
| `CODING_AGENTS_TOKEN` | (none) | Bearer token (required for protected endpoints) |
| `CODING_AGENTS_SESSION_ID` | (none) | Session id for `query_status.py` / `stream_events.py` |

### Create a session

```bash
CODING_AGENTS_URL=http://localhost:8765 \
CODING_AGENTS_TOKEN=my-token \
python examples/create_session.py "refactor auth.py"
```

Output:

```
✅ Created session: <uuid>
   agent:    claude
   status:   pending  ← pending — executor must consume this
   ...
```

### Poll until terminal status

```bash
CODING_AGENTS_SESSION_ID=<uuid> \
CODING_AGENTS_URL=http://localhost:8765 \
CODING_AGENTS_TOKEN=my-token \
python examples/query_status.py
```

### Stream events (SSE)

```bash
CODING_AGENTS_SESSION_ID=<uuid> \
CODING_AGENTS_URL=http://localhost:8765 \
CODING_AGENTS_TOKEN=my-token \
python examples/stream_events.py
```

### Handle errors

```bash
CODING_AGENTS_URL=http://localhost:8765 \
CODING_AGENTS_TOKEN=my-token \
python examples/error_handling.py
```

## Using the SDK from your own code

```python
from coding_agents_sdk import AsyncCodingAgentClient

async with AsyncCodingAgentClient(
    base_url="http://localhost:8765",
    token="…",
) as client:
    session = await client.create_session(agent="claude", prompt="…")
    async for event in client.stream_events(session.session_id):
        print(event.seq, event.type, event.data)
```

See [`../sdk/README.md`](../sdk/README.md) for the full API reference.

## License

MIT