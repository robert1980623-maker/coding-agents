"""Core data models for the coding agent runtime."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional


class AgentType(str, Enum):
    """Supported agent types."""

    CLAUDE = "claude"
    CODEX = "codex"


class SessionStatus(str, Enum):
    """Session lifecycle status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    TIMEOUT = "timeout"
    ORPHANED = "orphaned"

    @property
    def is_terminal(self) -> bool:
        return self in {
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.KILLED,
            SessionStatus.TIMEOUT,
            SessionStatus.ORPHANED,
        }


class EventType(str, Enum):
    """Event types emitted during execution."""

    SESSION_START = "session.start"
    STDOUT = "stdout"
    STDERR = "stderr"
    SYSTEM = "system"
    RESULT = "result"
    WATCH = "watch"
    ERROR = "error"


TERMINAL_STATUSES: frozenset[SessionStatus] = frozenset(
    {
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
        SessionStatus.KILLED,
        SessionStatus.TIMEOUT,
        SessionStatus.ORPHANED,
    }
)


@dataclass
class WatchPattern:
    """Monitor pattern for watching stdout/stderr content."""

    pattern: str
    action: str = "notify"  # "notify" | "callback" | "stop"
    callback: Optional[str] = None  # webhook URL or function name


@dataclass
class Session:
    """An execution session."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent: AgentType = AgentType.CLAUDE
    prompt: str = ""
    workdir: str = "."
    status: SessionStatus = SessionStatus.PENDING

    # Process info
    pid: Optional[int] = None
    exit_code: Optional[int] = None

    # Timing
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    last_heartbeat_at: Optional[datetime] = None

    # Cost & usage
    cost_usd: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None

    # Metadata
    model: Optional[str] = None
    provider: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Event:
    """An event emitted during session execution."""

    id: Optional[int] = None
    session_id: str = ""
    channel: str = "stdout"  # "stdout" | "stderr" | "system"
    seq: int = 0
    type: EventType = EventType.STDOUT
    data: str = ""

    # Optional: raw JSON (passthrough mode)
    raw_json: Optional[str] = None

    # Timestamp
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Metadata
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionConfig:
    """Configuration for a single execution."""

    # Timeouts
    timeout_seconds: int = 3600  # Default 1 hour
    idle_timeout_seconds: int = 300  # No output timeout

    # Resource limits
    max_memory_mb: int = 4096
    # None means "no cap". Default is None so that agents which do not
    # support a budget flag (e.g. codex) do not get spurious warnings,
    # and so that codex/claude behave identically when the user does
    # not pass --budget.
    max_budget_usd: Optional[float] = None

    # Retry
    max_retries: int = 0
    retry_delay_seconds: int = 5

    # Watch patterns
    watch_patterns: list[WatchPattern] = field(default_factory=list)

    # Output mode
    output_mode: str = "standard"  # "passthrough" | "standard"

    # Model override
    model: Optional[str] = None

    # Line length limit (default 8MiB)
    line_limit: int = 8 * 1024 * 1024

    # Environment variables
    env: dict[str, str] = field(default_factory=dict)
