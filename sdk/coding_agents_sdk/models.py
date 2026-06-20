"""Pydantic models for the SDK.

These models are defined independently from the server's internal models
(see plan v2 §1.3 — SHOULD #2). They mirror the HTTP wire format and may
include only the fields the SDK actually needs.

The SDK never triggers execution. ``Session.status`` for a session returned
by ``create_session()`` will be ``"pending"`` until an executor consumes it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Session status values (string form, matching the server's SessionStatus enum
# lowercased). Keep in sync with ``coding_agents.models.SessionStatus``.
SessionStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "killed",
    "timeout",
    "orphaned",
]

AgentType = Literal["claude", "codex"]


class Session(BaseModel):
    """A coding-agent session as returned by the HTTP API."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    agent: AgentType
    status: SessionStatus
    prompt: str = ""
    workdir: str = "."
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    # Optional execution/lifecycle fields (populated by server as execution progresses)
    pid: int | None = None
    exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    last_heartbeat_at: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    model: str | None = None
    provider: str | None = None


class Event(BaseModel):
    """A single event emitted during session execution.

    ``data`` is the raw event payload. The server stores it as a JSON string
    (string-encoded JSON), so the SDK keeps it as a raw value: if it parses as
    a JSON object/array we surface it as a dict/list, otherwise as the
    original string. Use :attr:`raw_json` for the exact string the server sent.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str
    seq: int
    type: str
    data: Any = None
    channel: str | None = None
    id: int | None = None
    raw_json: str | None = None
    created_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> "Event":
        """Build an Event from the server response, decoding ``data`` if it's JSON."""
        import json

        raw_data = payload.get("data")
        decoded: Any = raw_data
        if isinstance(raw_data, str):
            try:
                decoded = json.loads(raw_data)
            except (ValueError, TypeError):
                # Not JSON — keep the raw string.
                decoded = raw_data

        return cls(
            session_id=payload["session_id"],
            seq=payload["seq"],
            type=payload["type"],
            data=decoded,
            channel=payload.get("channel"),
            id=payload.get("id"),
            raw_json=payload.get("raw_json"),
            created_at=payload.get("created_at"),
            metadata=payload.get("metadata", {}) or {},
        )


class Tag(BaseModel):
    """A single tag attached to a session (response from POST /sessions/{id}/tags)."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    tag: str
    message: str


class TagsList(BaseModel):
    """Response from GET /sessions/{id}/tags."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    tags: list[str]


class KillResult(BaseModel):
    """Response from POST /sessions/{id}/kill."""

    model_config = ConfigDict(extra="allow")

    success: bool
    session_id: str
    message: str


class RecoverResult(BaseModel):
    """Response from POST /recover."""

    model_config = ConfigDict(extra="allow")

    recovered_count: int
    message: str


class HealthStatus(BaseModel):
    """Response from GET /health."""

    model_config = ConfigDict(extra="allow")

    status: str = "healthy"


__all__ = [
    "AgentType",
    "Event",
    "HealthStatus",
    "KillResult",
    "RecoverResult",
    "Session",
    "SessionStatus",
    "Tag",
    "TagsList",
]