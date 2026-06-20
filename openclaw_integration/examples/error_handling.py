#!/usr/bin/env python3
"""Example: robust error handling for the coding-agents SDK.

Demonstrates how to react to the most common failure modes:

  * 401 — invalid token (re-auth or abort)
  * 404 — session/tag does not exist (skip / cleanup)
  * 5xx — server error (retry with backoff)
  * ConnectionError_ — server unreachable (backoff & retry)
  * Generic APIError — other 4xx (log & propagate)

Run:
    CODING_AGENTS_URL=http://localhost:8765 \\
    python examples/error_handling.py
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from collections.abc import Awaitable, Callable

from coding_agents_sdk import (
    APIError,
    AsyncCodingAgentClient,
    AuthenticationError,
    CodingAgentsSDKError,
    ConnectionError_,
    NotFoundError,
    ServerError,
)


# Default retry policy for transient errors.
MAX_RETRIES = 5
BASE_DELAY = 0.5
MAX_DELAY = 8.0


async def with_retry(
    op: Callable[[], Awaitable[T]],
    *,
    description: str,
    max_retries: int = MAX_RETRIES,
) -> T:
    """Run ``op()`` with exponential backoff on transient errors.

    Retries on:
      * ``ConnectionError_`` — transport-level failure
      * ``ServerError`` (5xx) — server bug / overload
    Raises immediately on:
      * ``AuthenticationError``, ``NotFoundError``, other ``APIError``
    """
    attempt = 0
    while True:
        try:
            return await op()
        except (ConnectionError_, ServerError) as exc:
            attempt += 1
            if attempt > max_retries:
                print(f"❌ {description}: gave up after {max_retries} retries ({exc})")
                raise
            delay = min(MAX_DELAY, BASE_DELAY * (2 ** (attempt - 1)))
            delay += random.uniform(0, 0.25)  # jitter
            print(
                f"⚠️  {description}: transient error ({exc!r}); "
                f"retry {attempt}/{max_retries} in {delay:.2f}s…"
            )
            await asyncio.sleep(delay)


async def safe_kill(client: AsyncCodingAgentClient, session_id: str) -> bool:
    """Best-effort kill that distinguishes 404 (already gone) from other errors."""
    try:
        result = await client.kill(session_id)
        return result.success
    except NotFoundError:
        # Already gone — that's fine.
        return False
    except AuthenticationError as exc:
        print(f"❌ auth failed — check CODING_AGENTS_TOKEN ({exc.detail})", file=sys.stderr)
        raise
    except APIError as exc:
        # Other 4xx — log and re-raise; let the caller decide.
        print(f"❌ kill failed: HTTP {exc.status_code} ({exc.detail})", file=sys.stderr)
        raise


async def safe_list_tags(client: AsyncCodingAgentClient, session_id: str) -> list[str]:
    """Return tags, treating 404 as 'no tags / no session'."""
    try:
        return await client.list_tags(session_id)
    except NotFoundError:
        return []
    except CodingAgentsSDKError as exc:
        print(f"❌ unexpected error while listing tags: {exc!r}", file=sys.stderr)
        raise


async def main() -> int:
    base_url = os.environ.get("CODING_AGENTS_URL", "http://localhost:8765")
    token = os.environ.get("CODING_AGENTS_TOKEN")

    async with AsyncCodingAgentClient(base_url=base_url, token=token) as client:
        # 1. Health check (with retry)
        try:
            health = await with_retry(client.health, description="health check")
            print(f"server: {health.status}")
        except AuthenticationError as exc:
            print(f"❌ token rejected: {exc.detail}", file=sys.stderr)
            return 1
        except CodingAgentsSDKError as exc:
            print(f"❌ cannot reach server: {exc!r}", file=sys.stderr)
            return 1

        # 2. Listing tags for an unknown session — should NOT raise.
        tags = await safe_list_tags(client, "00000000-0000-0000-0000-000000000000")
        print(f"unknown session tags: {tags}  (empty list is expected)")

        # 3. Killing a non-existent session — should NOT crash the script.
        killed = await safe_kill(client, "00000000-0000-0000-0000-000000000000")
        print(f"killed unknown session: {killed}  (False is expected)")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(130)