#!/usr/bin/env python3
"""Example: create a session via the coding-agents HTTP API.

⚠️  IMPORTANT — read this before running
========================================

The HTTP ``POST /sessions`` endpoint ONLY creates a session record in
``pending`` status. It does NOT trigger execution.

This example demonstrates the pure-wrapping semantics: we call the SDK,
inspect the returned session, and exit. To actually run the agent, an
external executor must consume ``pending`` sessions (see
``docs/INTEGRATION.md`` for the executor contract).

Run:
    CODING_AGENTS_URL=http://localhost:8765 \\
    CODING_AGENTS_TOKEN=my-secret-token \\
    python examples/create_session.py "refactor auth.py"
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from coding_agents_sdk import AsyncCodingAgentClient


async def main(prompt: str) -> int:
    base_url = os.environ.get("CODING_AGENTS_URL", "http://localhost:8765")
    token = os.environ.get("CODING_AGENTS_TOKEN")

    async with AsyncCodingAgentClient(base_url=base_url, token=token) as client:
        session = await client.create_session(
            agent="claude",
            prompt=prompt,
            workdir=os.getcwd(),
            metadata={"source": "openclaw-example"},
        )

        print(f"✅ Created session: {session.session_id}")
        print(f"   agent:    {session.agent}")
        print(f"   status:   {session.status}  ← pending — executor must consume this")
        print(f"   workdir:  {session.workdir}")
        print(f"   created:  {session.created_at}")
        print()
        print("Next steps:")
        print("  • wait for executor: poll status with get_session(...)")
        print("  • stream events:     async for e in client.stream_events(...)")
        print("  • attach metadata:   await client.create_tag(...)")

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: create_session.py "<prompt>"', file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))