#!/usr/bin/env python3
"""Example: poll the status of a session until it reaches a terminal state.

Reads a session id from the command line (or the ``CODING_AGENTS_SESSION_ID``
env var) and polls ``GET /sessions/{id}`` until the status is terminal
(``completed`` / ``failed`` / ``killed`` / ``timeout`` / ``orphaned``).

Run:
    CODING_AGENTS_SESSION_ID=<id> \\
    CODING_AGENTS_URL=http://localhost:8765 \\
    python examples/query_status.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from coding_agents_sdk import AsyncCodingAgentClient, Session

TERMINAL_STATUSES = frozenset({"completed", "failed", "killed", "timeout", "orphaned"})


def render(session: Session) -> str:
    """Format a session for terminal output."""
    cost = f"${session.cost_usd:.4f}" if session.cost_usd is not None else "-"
    duration = f"{session.duration_ms} ms" if session.duration_ms is not None else "-"
    return (
        f"  status:    {session.status}\n"
        f"  agent:     {session.agent}\n"
        f"  duration:  {duration}\n"
        f"  cost:      {cost}\n"
        f"  tokens:    in={session.input_tokens or 0} out={session.output_tokens or 0}\n"
        f"  exit_code: {session.exit_code}\n"
        f"  updated:   {session.updated_at}"
    )


async def poll_until_terminal(
    client: AsyncCodingAgentClient,
    session_id: str,
    *,
    poll_interval: float = 2.0,
    max_wait: float = 600.0,
) -> Session:
    """Poll a session until it reaches a terminal status.

    Raises:
        TimeoutError: if ``max_wait`` is exceeded.
    """
    elapsed = 0.0
    while elapsed < max_wait:
        session = await client.get_session(session_id)
        print(f"[{elapsed:6.1f}s] status={session.status}")
        if session.status in TERMINAL_STATUSES:
            return session
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    raise TimeoutError(f"Session {session_id} did not finish within {max_wait}s")


async def main() -> int:
    base_url = os.environ.get("CODING_AGENTS_URL", "http://localhost:8765")
    token = os.environ.get("CODING_AGENTS_TOKEN")
    session_id = (
        sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CODING_AGENTS_SESSION_ID")
    )
    if not session_id:
        print("Error: provide a session id via argv or CODING_AGENTS_SESSION_ID", file=sys.stderr)
        return 2

    async with AsyncCodingAgentClient(base_url=base_url, token=token) as client:
        # Show tags, if any
        try:
            tags = await client.list_tags(session_id)
        except Exception as exc:  # noqa: BLE001 — example script
            tags = []
            print(f"(could not list tags: {exc})")

        print(f"Session {session_id}")
        if tags:
            print(f"  tags: {', '.join(tags)}")
        try:
            session = await poll_until_terminal(client, session_id)
        except TimeoutError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1

        print("\nFinal state:")
        print(render(session))

        if session.status == "completed":
            return 0
        if session.status in {"failed", "killed", "timeout", "orphaned"}:
            return 1
        return 0  # pragma: no cover — should be terminal


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))