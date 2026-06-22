"""Async Python SDK for the coding-agents HTTP API.

This SDK is a thin, pure-HTTP wrapper around the coding-agents REST API.
It does **not** trigger or interpret execution semantics — see plan v2
``§约束 1`` for the contract.

Quick start:

    import asyncio
    from coding_agents_sdk import AsyncCodingAgentClient


    async def main() -> None:
        async with AsyncCodingAgentClient(base_url="http://localhost:8765") as client:
            session = await client.create_session(agent="claude", prompt="refactor me")
            print(session.session_id, session.status)  # status == "pending"


    asyncio.run(main())
"""

from __future__ import annotations

from coding_agents_sdk.client import AsyncCodingAgentClient
from coding_agents_sdk.exceptions import (
    APIError,
    AuthenticationError,
    CodingAgentsSDKError,
    ConnectionError_,
    NetworkError,
    NotFoundError,
    ServerError,
)
from coding_agents_sdk.models import (
    AgentType,
    Event,
    HealthStatus,
    KillResult,
    RecoverResult,
    Session,
    SessionStatus,
    Tag,
    TagsList,
)

__version__ = "0.2.0"

__all__ = [
    "__version__",
    "AgentType",
    "APIError",
    "AsyncCodingAgentClient",
    "AuthenticationError",
    "CodingAgentsSDKError",
    "ConnectionError_",
    "Event",
    "HealthStatus",
    "KillResult",
    "NetworkError",
    "NotFoundError",
    "RecoverResult",
    "ServerError",
    "Session",
    "SessionStatus",
    "Tag",
    "TagsList",
]