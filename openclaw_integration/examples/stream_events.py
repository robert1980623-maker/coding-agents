#!/usr/bin/env python3
"""Example: subscribe to a session's event stream over SSE.

Demonstrates how OpenClaw can subscribe to a coding-agent session and
process events as they arrive — including resumption via ``Last-Event-ID``.

Run:
    CODING_AGENTS_SESSION_ID=<id> \\
    CODING_AGENTS_URL=http://localhost:8765 \\
    python examples/stream_events.py
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import AsyncIterator

from coding_agents_sdk import AsyncCodingAgentClient, Event

# Stop streaming when we see this event type (None = until server closes).
STOP_ON: str | None = "result"

# How many events to fetch before exiting (None = unlimited).
MAX_EVENTS: int | None = None


async def iter_events(
    client: AsyncCodingAgentClient,
    session_id: str,
) -> AsyncIterator[Event]:
    """Yield events for a session, falling back to REST on connection loss."""
    last_seq = 0
    while True:
        try:
            async for event in client.stream_events(session_id, last_event_id=last_seq):
                if event.seq > last_seq:
                    last_seq = event.seq
                yield event
                if STOP_ON and event.type == STOP_ON:
                    return
                if MAX_EVENTS is not None and last_seq >= MAX_EVENTS:
                    return
            # Server closed the stream cleanly — assume session is done.
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Network blip — try to resume via REST.
            print(f"⚠️  stream interrupted: {exc!r}; resuming from seq={last_seq}…")
            await asyncio.sleep(1.0)
            events = await client.get_events(session_id, after_seq=last_seq)
            for event in events:
                last_seq = max(last_seq, event.seq)
                yield event
            continue


async def main() -> int:
    base_url = os.environ.get("CODING_AGENTS_URL", "http://localhost:8765")
    token = os.environ.get("CODING_AGENTS_TOKEN")
    session_id = (
        sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CODING_AGENTS_SESSION_ID")
    )
    if not session_id:
        print("Error: provide a session id via argv or CODING_AGENTS_SESSION_ID", file=sys.stderr)
        return 2

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(signame: str) -> None:
        print(f"\n🛑 received {signame}, stopping stream…", file=sys.stderr)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig.name)
        except NotImplementedError:  # pragma: no cover — Windows / restricted envs
            pass

    async with AsyncCodingAgentClient(base_url=base_url, token=token) as client:
        try:
            count = 0
            async for event in iter_events(client, session_id):
                count += 1
                # Compact, log-friendly rendering
                data_preview = repr(event.data)
                if len(data_preview) > 120:
                    data_preview = data_preview[:117] + "…"
                print(f"[seq={event.seq:>4}] {event.type:<20} {data_preview}")
                if stop_event.is_set():
                    break
        finally:
            print(f"\nStream ended after {count} events.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))