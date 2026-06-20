"""Tests for the HTTP API."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from coding_agents.auth import ensure_token, load_token
from coding_agents.http.server import create_app
from coding_agents.models import AgentType, Session, SessionStatus
from coding_agents.storage.sqlite import SQLiteStorage


@pytest.fixture
async def token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create a test token and patch the token path."""
    token_path = tmp_path / "test-token"
    token = ensure_token(str(token_path))

    # Patch the auth module to use our test token path
    monkeypatch.setattr(
        "coding_agents.auth.DEFAULT_TOKEN_PATH",
        str(token_path),
    )

    return token


@pytest.fixture
async def storage(tmp_path: Path) -> SQLiteStorage:
    """Create a test storage instance."""
    db_path = tmp_path / "test.db"
    store = SQLiteStorage(str(db_path))
    await store.initialize()
    return store


@pytest.fixture
async def app(storage: SQLiteStorage):
    """Create a test FastAPI app."""
    test_app = create_app(db_path=str(storage._db_path))

    # Override the storage dependency
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


@pytest.fixture
async def unauth_client(app) -> AsyncClient:
    """Create a test HTTP client without auth."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestAuthentication:
    """Test authentication middleware."""

    async def test_no_token_returns_401(self, unauth_client: AsyncClient):
        """Request without token should return 401."""
        response = await unauth_client.get("/sessions")
        assert response.status_code == 401
        assert "Missing authorization" in response.json()["detail"]

    async def test_invalid_token_returns_401(self, app, tmp_path: Path):
        """Request with invalid token should return 401."""
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer invalid-token"},
        ) as client:
            response = await client.get("/sessions")
            assert response.status_code == 401
            assert "Invalid token" in response.json()["detail"]

    async def test_valid_token_returns_200(self, client: AsyncClient):
        """Request with valid token should succeed."""
        response = await client.get("/sessions")
        assert response.status_code == 200


class TestSessions:
    """Test session endpoints."""

    async def test_create_session(self, client: AsyncClient):
        """POST /sessions should create a new session."""
        response = await client.post(
            "/sessions",
            json={
                "agent": "claude",
                "prompt": "test prompt",
                "workdir": "/tmp",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["agent"] == "claude"
        assert data["prompt"] == "test prompt"
        assert data["workdir"] == "/tmp"
        assert data["status"] == "pending"
        assert "id" in data

    async def test_list_sessions_empty(self, client: AsyncClient):
        """GET /sessions should return empty list initially."""
        response = await client.get("/sessions")
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_sessions_with_data(self, client: AsyncClient):
        """GET /sessions should return created sessions."""
        # Create a session
        await client.post("/sessions", json={"agent": "claude", "prompt": "test"})

        # List sessions
        response = await client.get("/sessions")
        assert response.status_code == 200
        sessions = response.json()
        assert len(sessions) == 1
        assert sessions[0]["prompt"] == "test"

    async def test_get_session(self, client: AsyncClient):
        """GET /sessions/:id should return the session."""
        # Create a session
        create_response = await client.post(
            "/sessions", json={"agent": "claude", "prompt": "test"}
        )
        session_id = create_response.json()["id"]

        # Get the session
        response = await client.get(f"/sessions/{session_id}")
        assert response.status_code == 200
        assert response.json()["id"] == session_id

    async def test_get_nonexistent_session_returns_404(self, client: AsyncClient):
        """GET /sessions/:id should return 404 for nonexistent session."""
        response = await client.get("/sessions/nonexistent-id")
        assert response.status_code == 404

    async def test_list_sessions_with_filters(self, client: AsyncClient):
        """GET /sessions should support filtering."""
        # Create sessions with different agents
        await client.post("/sessions", json={"agent": "claude", "prompt": "test1"})
        await client.post("/sessions", json={"agent": "codex", "prompt": "test2"})

        # Filter by agent
        response = await client.get("/sessions?agent=claude")
        assert response.status_code == 200
        sessions = response.json()
        assert len(sessions) == 1
        assert sessions[0]["agent"] == "claude"


class TestEvents:
    """Test event endpoints."""

    async def test_get_events_empty(self, client: AsyncClient, storage: SQLiteStorage):
        """GET /sessions/:id/events should return empty list initially."""
        # Create a session
        session = Session(agent=AgentType.CLAUDE, prompt="test")
        await storage.create_session(session)

        # Get events
        response = await client.get(f"/sessions/{session.id}/events")
        assert response.status_code == 200
        assert response.json() == []

    async def test_get_events_with_data(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """GET /sessions/:id/events should return events."""
        from coding_agents.models import Event, EventType

        # Create a session
        session = Session(agent=AgentType.CLAUDE, prompt="test")
        await storage.create_session(session)

        # Add events
        event = Event(
            session_id=session.id,
            channel="stdout",
            seq=1,
            type=EventType.STDOUT,
            data="test output",
        )
        await storage.append_events([event])

        # Get events
        response = await client.get(f"/sessions/{session.id}/events")
        assert response.status_code == 200
        events = response.json()
        assert len(events) == 1
        assert events[0]["data"] == "test output"
        assert events[0]["seq"] == 1

    async def test_get_events_after_seq(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """GET /sessions/:id/events should support after_seq filter."""
        from coding_agents.models import Event, EventType

        # Create a session
        session = Session(agent=AgentType.CLAUDE, prompt="test")
        await storage.create_session(session)

        # Add multiple events
        events = [
            Event(session_id=session.id, channel="stdout", seq=1, type=EventType.STDOUT, data="event1"),
            Event(session_id=session.id, channel="stdout", seq=2, type=EventType.STDOUT, data="event2"),
            Event(session_id=session.id, channel="stdout", seq=3, type=EventType.STDOUT, data="event3"),
        ]
        await storage.append_events(events)

        # Get events after seq=1
        response = await client.get(f"/sessions/{session.id}/events?after_seq=1")
        assert response.status_code == 200
        events_data = response.json()
        assert len(events_data) == 2
        assert events_data[0]["seq"] == 2
        assert events_data[1]["seq"] == 3

    async def test_get_events_nonexistent_session_returns_404(
        self, client: AsyncClient
    ):
        """GET /sessions/:id/events should return 404 for nonexistent session."""
        response = await client.get("/sessions/nonexistent-id/events")
        assert response.status_code == 404

    async def test_sse_stream_receives_realtime_events(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """SSE /stream should deliver new events as they are appended."""
        import asyncio

        from coding_agents.models import Event, EventType, SessionStatus

        # Create a RUNNING session
        session = Session(
            agent=AgentType.CLAUDE,
            prompt="sse test",
            status=SessionStatus.RUNNING,
        )
        await storage.create_session(session)

        # Pre-existing event (seq=1) — should be received immediately
        await storage.append_events(
            [
                Event(
                    session_id=session.id,
                    channel="stdout",
                    seq=1,
                    type=EventType.STDOUT,
                    data="pre-existing",
                )
            ]
        )

        received_data: list[str] = []

        async def consume_stream():
            """Read SSE lines from the streaming response."""
            async with client.stream(
                "GET",
                f"/sessions/{session.id}/events/stream",
                timeout=httpx.Timeout(30.0, connect=5.0),
            ) as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        received_data.append(line[len("data:") :].strip())
                    # Stop once we've seen events from all phases
                    if len(received_data) >= 3:
                        break

        async def produce_events():
            """Push new events after a short delay, then complete the session."""
            await asyncio.sleep(0.5)
            # seq=2 — should arrive while stream is running
            await storage.append_events(
                [
                    Event(
                        session_id=session.id,
                        channel="stdout",
                        seq=2,
                        type=EventType.STDOUT,
                        data="live-event-1",
                    )
                ]
            )
            await asyncio.sleep(1.5)
            # seq=3 + mark terminal
            await storage.append_events(
                [
                    Event(
                        session_id=session.id,
                        channel="stdout",
                        seq=3,
                        type=EventType.STDOUT,
                        data="live-event-2",
                    )
                ]
            )
            await storage.update_session(session.id, status=SessionStatus.COMPLETED)

        # Run producer and consumer concurrently
        await asyncio.gather(produce_events(), consume_stream())

        # Verify we got all 3 events (pre-existing + 2 live)
        assert len(received_data) >= 3
        # Parse the JSON data payloads to check content
        import json

        payloads = [json.loads(d) for d in received_data[:3]]
        assert payloads[0]["data"] == "pre-existing"
        assert payloads[0]["seq"] == 1
        assert payloads[1]["data"] == "live-event-1"
        assert payloads[1]["seq"] == 2
        assert payloads[2]["data"] == "live-event-2"
        assert payloads[2]["seq"] == 3


class TestActions:
    """Test action endpoints."""

    async def test_kill_session(self, client: AsyncClient, storage: SQLiteStorage):
        """POST /sessions/:id/kill should kill a running session."""
        # Create a running session
        session = Session(
            agent=AgentType.CLAUDE,
            prompt="test",
            status=SessionStatus.RUNNING,
        )
        await storage.create_session(session)

        # Kill the session
        response = await client.post(f"/sessions/{session.id}/kill")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["session_id"] == session.id

        # Verify session is killed
        updated_session = await storage.get_session(session.id)
        assert updated_session is not None
        assert updated_session.status == SessionStatus.KILLED

    async def test_kill_already_killed_session(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """POST /sessions/:id/kill should handle already killed sessions."""
        # Create a killed session
        session = Session(
            agent=AgentType.CLAUDE,
            prompt="test",
            status=SessionStatus.KILLED,
        )
        await storage.create_session(session)

        # Try to kill again
        response = await client.post(f"/sessions/{session.id}/kill")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "already killed" in data["message"]

    async def test_kill_nonexistent_session_returns_404(self, client: AsyncClient):
        """POST /sessions/:id/kill should return 404 for nonexistent session."""
        response = await client.post("/sessions/nonexistent-id/kill")
        assert response.status_code == 404

    async def test_recover_sessions(
        self, client: AsyncClient, storage: SQLiteStorage
    ):
        """POST /recover should recover orphaned sessions."""
        response = await client.post("/recover?timeout=300")
        assert response.status_code == 200
        data = response.json()
        assert "recovered_count" in data
        assert "message" in data


class TestTags:
    """Test tag endpoints."""

    async def test_add_tag(self, client: AsyncClient, storage: SQLiteStorage):
        """POST /sessions/:id/tags should add a tag."""
        # Create a session
        session = Session(agent=AgentType.CLAUDE, prompt="test")
        await storage.create_session(session)

        # Add a tag
        response = await client.post(
            f"/sessions/{session.id}/tags",
            json={"tag": "important"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["tag"] == "important"

        # Verify tag was added
        tags = await storage.list_tags(session.id)
        assert "important" in tags

    async def test_list_tags(self, client: AsyncClient, storage: SQLiteStorage):
        """GET /sessions/:id/tags should list tags."""
        # Create a session with tags
        session = Session(agent=AgentType.CLAUDE, prompt="test")
        await storage.create_session(session)
        await storage.add_tag(session.id, "tag1")
        await storage.add_tag(session.id, "tag2")

        # List tags
        response = await client.get(f"/sessions/{session.id}/tags")
        assert response.status_code == 200
        tags = response.json()["tags"]
        assert len(tags) == 2
        assert "tag1" in tags
        assert "tag2" in tags

    async def test_remove_tag(self, client: AsyncClient, storage: SQLiteStorage):
        """DELETE /sessions/:id/tags/:tag should remove a tag."""
        # Create a session with a tag
        session = Session(agent=AgentType.CLAUDE, prompt="test")
        await storage.create_session(session)
        await storage.add_tag(session.id, "to-remove")

        # Remove the tag
        response = await client.delete(f"/sessions/{session.id}/tags/to-remove")
        assert response.status_code == 200

        # Verify tag was removed
        tags = await storage.list_tags(session.id)
        assert "to-remove" not in tags

    async def test_tag_operations_nonexistent_session_returns_404(
        self, client: AsyncClient
    ):
        """Tag operations should return 404 for nonexistent session."""
        response = await client.get("/sessions/nonexistent-id/tags")
        assert response.status_code == 404

        response = await client.post(
            "/sessions/nonexistent-id/tags",
            json={"tag": "test"},
        )
        assert response.status_code == 404


class TestHealthAndMetrics:
    """Test health and metrics endpoints."""

    async def test_health_endpoint(self, client: AsyncClient):
        """GET /health should return healthy status."""
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    async def test_metrics_endpoint(self, client: AsyncClient):
        """GET /metrics should return Prometheus metrics."""
        response = await client.get("/metrics")
        assert response.status_code == 200
        # Check that it's Prometheus format
        assert "text/plain" in response.headers["content-type"]
        # Should contain at least some metric data
        content = response.text
        assert "# HELP" in content or "# TYPE" in content or len(content) > 0
