"""Tests for the HTTP resume endpoint.

Covers:
- 404 for non-existent session
- 409 for RUNNING session
- 409 for FAILED session with non-zero exit
- 409 for FAILED session with zero exit
- 409 for ORPHANED session
- 200 for successful resume (core resume_session is mocked so no real
  agent subprocess is spawned)
- 200 with explicit new_session_id query param
- 409 raised by the core (ResumeNotSupportedError) is mapped correctly
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from coding_agents.auth import ensure_token
from coding_agents.resume import ResumeNotSupportedError
from coding_agents.http.server import create_app
from coding_agents.models import (
    AgentType,
    Event,
    EventType,
    Session,
    SessionStatus,
)
from coding_agents.storage.sqlite import SQLiteStorage


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_http.py so this file can run standalone)
# ---------------------------------------------------------------------------


@pytest.fixture
async def token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create a test token and patch the token path."""
    token_path = tmp_path / "test-token"
    token = ensure_token(str(token_path))

    monkeypatch.setattr(
        "coding_agents.auth.DEFAULT_TOKEN_PATH",
        str(token_path),
    )
    return token


@pytest.fixture
async def storage(tmp_path: Path) -> Any:
    """Create a test storage instance."""
    db_path = tmp_path / "test.db"
    store = SQLiteStorage(str(db_path))
    await store.initialize()
    return store


@pytest.fixture
async def app(storage: SQLiteStorage):
    """Create a test FastAPI app with the storage dependency overridden."""
    test_app = create_app(db_path=str(storage._db_path))

    async def get_test_storage() -> SQLiteStorage:
        return storage

    test_app.dependency_overrides[SQLiteStorage] = get_test_storage
    return test_app


@pytest.fixture
async def client(app, token: str) -> AsyncClient:
    """Create a test HTTP client with auth."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


def _make_event(session_id: str, seq: int = 1, data: str = "hello") -> Event:
    return Event(
        session_id=session_id,
        channel="stdout",
        seq=seq,
        type=EventType.STDOUT,
        data=data,
    )


async def _seed_resumable_session(
    storage: SQLiteStorage,
    session_id: str = "orig-1",
    status: SessionStatus = SessionStatus.COMPLETED,
    exit_code: int | None = 0,
    event_count: int = 3,
) -> Session:
    """Seed a session that can_resume() should accept by default."""
    session = Session(
        id=session_id,
        agent=AgentType.CLAUDE,
        prompt="resume me",
        status=status,
        exit_code=exit_code,
    )
    await storage.create_session(session)
    events = [
        _make_event(session_id, seq=i, data=f"line-{i}")
        for i in range(1, event_count + 1)
    ]
    await storage.append_events(events)
    return session


def _install_fake_resume_session(
    storage: SQLiteStorage,
    new_session_id: str = "new-resumed-1",
    status: SessionStatus = SessionStatus.PENDING,
) -> Any:
    """Patch actions.resume_session with an async fake.

    The fake mirrors what the real core function does *up to* execution:
    it creates the new session in PENDING, links it to the original via
    metadata, and returns ``(new_sid, [])`` without actually spawning an
    agent. Tests can then assert on the new session's existence and
    metadata.
    """
    captured: dict[str, Any] = {}
    # Capture the default new_session_id in a closure variable so the
    # inner kwarg with the same name does not shadow it.
    default_new_session_id = new_session_id

    async def fake_resume(
        session_id: str,
        storage_arg: Any,
        agent_factory: Any | None = None,
        new_session_id: str | None = None,
    ) -> tuple[str, list[Event]]:
        sid = new_session_id or default_new_session_id
        captured["session_id"] = session_id
        captured["new_session_id"] = sid
        captured["storage"] = storage_arg

        new_session = Session(
            id=sid,
            agent=AgentType.CLAUDE,
            prompt="resume me",
            status=status,
            metadata={
                "resumed_from": session_id,
                "resume_from_seq": 3,
            },
        )
        await storage.create_session(new_session)
        return sid, []

    patcher = patch(
        "coding_agents.http.routes.actions.resume_session",
        side_effect=fake_resume,
    )
    return patcher, captured


# ---------------------------------------------------------------------------
# 404 — session does not exist
# ---------------------------------------------------------------------------


class TestResumeSessionNotFound:
    async def test_resume_nonexistent_session_returns_404(
        self, client: AsyncClient
    ):
        """POST /sessions/:id/resume on a missing id → 404 with detail."""
        response = await client.post("/sessions/no-such-id/resume")
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert "no-such-id" in detail
        assert "not found" in detail.lower()


# ---------------------------------------------------------------------------
# 409 — session is not resumable
# ---------------------------------------------------------------------------


class TestResumeSessionConflict:
    async def test_resume_running_session_returns_409(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """RUNNING sessions cannot be resumed (not a terminal state)."""
        session = Session(
            id="running-1",
            agent=AgentType.CLAUDE,
            prompt="running",
            status=SessionStatus.RUNNING,
        )
        await storage.create_session(session)
        await storage.append_events([_make_event("running-1")])

        response = await client.post("/sessions/running-1/resume")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "running-1" in detail
        assert "cannot be resumed" in detail
        assert "status=running" in detail

    async def test_resume_failed_nonzero_exit_returns_409(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """FAILED with exit_code != 0 → 409 (agent state is unreliable)."""
        session = Session(
            id="failed-bad",
            agent=AgentType.CLAUDE,
            prompt="failed",
            status=SessionStatus.FAILED,
            exit_code=1,
        )
        await storage.create_session(session)
        await storage.append_events([_make_event("failed-bad")])

        response = await client.post("/sessions/failed-bad/resume")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "failed-bad" in detail
        assert "cannot be resumed" in detail
        assert "status=failed" in detail
        assert "exit_code=1" in detail

    async def test_resume_failed_zero_exit_returns_409(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """FAILED is not in RESUMABLE_STATUSES even with exit_code=0."""
        session = Session(
            id="failed-zero",
            agent=AgentType.CLAUDE,
            prompt="failed",
            status=SessionStatus.FAILED,
            exit_code=0,
        )
        await storage.create_session(session)
        await storage.append_events([_make_event("failed-zero")])

        response = await client.post("/sessions/failed-zero/resume")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "cannot be resumed" in detail
        assert "status=failed" in detail

    async def test_resume_orphaned_session_returns_409(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """ORPHANED sessions cannot be resumed (process is presumed dead)."""
        session = Session(
            id="orphan-1",
            agent=AgentType.CLAUDE,
            prompt="orphan",
            status=SessionStatus.ORPHANED,
        )
        await storage.create_session(session)
        await storage.append_events([_make_event("orphan-1")])

        response = await client.post("/sessions/orphan-1/resume")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "status=orphaned" in detail

    async def test_resume_completed_with_no_events_returns_409(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """COMPLETED with zero events → 409 (no checkpoint to resume from)."""
        session = Session(
            id="no-events",
            agent=AgentType.CLAUDE,
            prompt="no events",
            status=SessionStatus.COMPLETED,
            exit_code=0,
        )
        await storage.create_session(session)
        # No events appended.

        response = await client.post("/sessions/no-events/resume")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "no-events" in detail
        assert "cannot be resumed" in detail


# ---------------------------------------------------------------------------
# 200 — happy path
# ---------------------------------------------------------------------------


class TestResumeSessionSuccess:
    async def test_resume_completed_session_returns_200(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """Resuming a COMPLETED, exit_code=0 session with events → 200."""
        await _seed_resumable_session(
            storage, "orig-1", SessionStatus.COMPLETED, exit_code=0
        )

        patcher, captured = _install_fake_resume_session(
            storage, new_session_id="new-resumed-1"
        )
        with patcher:
            response = await client.post("/sessions/orig-1/resume")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["original_session_id"] == "orig-1"
        assert data["new_session_id"] == "new-resumed-1"
        # last_seq is the highest seq from the original session's events
        assert data["last_seq"] == 3
        # The fake creates the new session in PENDING; that is what the
        # endpoint should report.
        assert data["status"] == "pending"
        assert "Resumed" in data["message"]
        assert "orig-1" in data["message"]
        assert "new-resumed-1" in data["message"]

        # The fake was called with the right original session id.
        assert captured["session_id"] == "orig-1"
        assert captured["new_session_id"] == "new-resumed-1"

        # The new session is in storage, linked to the original.
        new_session = await storage.get_session("new-resumed-1")
        assert new_session is not None
        assert new_session.metadata.get("resumed_from") == "orig-1"
        assert new_session.metadata.get("resume_from_seq") == 3

    async def test_resume_with_explicit_new_session_id_returns_200(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """`?new_session_id=foo` is forwarded to the core and reflected."""
        await _seed_resumable_session(
            storage, "orig-2", SessionStatus.KILLED, exit_code=None
        )

        patcher, captured = _install_fake_resume_session(
            storage, new_session_id="my-custom-resume-id"
        )
        with patcher:
            response = await client.post(
                "/sessions/orig-2/resume",
                params={"new_session_id": "my-custom-resume-id"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["new_session_id"] == "my-custom-resume-id"
        # The endpoint must forward the explicit id to the core.
        assert captured["new_session_id"] == "my-custom-resume-id"

        new_session = await storage.get_session("my-custom-resume-id")
        assert new_session is not None
        assert new_session.metadata.get("resumed_from") == "orig-2"

    async def test_resume_killed_session_returns_200(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """KILLED is in RESUMABLE_STATUSES and should succeed."""
        await _seed_resumable_session(
            storage, "killed-1", SessionStatus.KILLED, exit_code=None
        )

        patcher, _captured = _install_fake_resume_session(
            storage, new_session_id="new-k"
        )
        with patcher:
            response = await client.post("/sessions/killed-1/resume")

        assert response.status_code == 200
        assert response.json()["original_session_id"] == "killed-1"
        assert response.json()["new_session_id"] == "new-k"

    async def test_resume_timeout_session_returns_200(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """TIMEOUT is in RESUMABLE_STATUSES and should succeed."""
        await _seed_resumable_session(
            storage, "timeout-1", SessionStatus.TIMEOUT, exit_code=None
        )

        patcher, _captured = _install_fake_resume_session(
            storage, new_session_id="new-to"
        )
        with patcher:
            response = await client.post("/sessions/timeout-1/resume")

        assert response.status_code == 200
        assert response.json()["original_session_id"] == "timeout-1"
        assert response.json()["new_session_id"] == "new-to"

    async def test_resume_response_model_has_all_fields(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """The response shape matches the documented ResumeResponse."""
        await _seed_resumable_session(
            storage, "shape-1", SessionStatus.COMPLETED, exit_code=0
        )

        patcher, _captured = _install_fake_resume_session(
            storage, new_session_id="shape-new"
        )
        with patcher:
            response = await client.post("/sessions/shape-1/resume")

        assert response.status_code == 200
        data = response.json()
        # Every documented field is present.
        for key in (
            "success",
            "original_session_id",
            "new_session_id",
            "last_seq",
            "status",
            "message",
        ):
            assert key in data, f"missing field: {key}"
        # Types match the model.
        assert isinstance(data["success"], bool)
        assert isinstance(data["original_session_id"], str)
        assert isinstance(data["new_session_id"], str)
        assert isinstance(data["last_seq"], int)
        assert isinstance(data["status"], str)
        assert isinstance(data["message"], str)


# ---------------------------------------------------------------------------
# 409 — core resume_session() raises ResumeNotSupportedError
# ---------------------------------------------------------------------------


class TestResumeSessionCoreError:
    async def test_resume_not_supported_error_maps_to_409(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """If can_resume() passes but the core raises ResumeNotSupportedError,
        the endpoint surfaces a 409 (not a 500) with the core's message."""

        await _seed_resumable_session(
            storage, "orig-race", SessionStatus.COMPLETED, exit_code=0
        )

        # can_resume() returns True (storage is set up correctly), but
        # resume_session() raises — simulates a TOCTOU race or some
        # other late failure inside the core.
        fake_resume = AsyncMock(
            side_effect=ResumeNotSupportedError("session state changed mid-call")
        )

        with patch(
            "coding_agents.http.routes.actions.resume_session", fake_resume
        ):
            response = await client.post("/sessions/orig-race/resume")

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "session state changed mid-call" in detail
