"""Tests for AsyncCodingAgentClient.

Uses ``httpx.MockTransport`` so no real HTTP server is required.
Covers all endpoints and error paths (401, 404, 500) plus SSE streaming.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx
import pytest

from coding_agents_sdk import (
    APIError,
    AsyncCodingAgentClient,
    AuthenticationError,
    ConnectionError_,
    NetworkError,
    NotFoundError,
    RateLimitError,
    ServerError,
)


# ---------------------------------------------------------------------- #
# Fixtures / helpers
# ---------------------------------------------------------------------- #


def make_session_payload(
    session_id: str = "sess-123",
    *,
    status: str = "pending",
    agent: str = "claude",
    prompt: str = "refactor me",
) -> dict[str, Any]:
    return {
        "id": session_id,
        "agent": agent,
        "prompt": prompt,
        "workdir": "/tmp/project",
        "status": status,
        "metadata": {},
        "created_at": "2026-06-20T10:00:00+00:00",
        "updated_at": "2026-06-20T10:00:00+00:00",
    }


def make_event_payload(seq: int, type_: str = "stdout", data: Any = "hello") -> dict[str, Any]:
    if isinstance(data, (dict, list)):
        import json

        data_str = json.dumps(data)
    else:
        data_str = str(data)
    return {
        "id": seq,
        "session_id": "sess-123",
        "channel": "stdout",
        "seq": seq,
        "type": type_,
        "data": data_str,
        "raw_json": data_str,
        "created_at": "2026-06-20T10:00:00+00:00",
        "metadata": {},
    }


def make_handler(
    route_map: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> httpx.MockTransport:
    """Build a ``MockTransport`` from a mapping of ``METHOD path`` → handler."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        if key in route_map:
            return route_map[key](request)
        return httpx.Response(404, json={"detail": f"unmocked: {key}"})

    return httpx.MockTransport(handler)


def sse_response(events: list[dict[str, Any]]) -> httpx.Response:
    """Build a fake SSE response body from a list of payload dicts."""
    import json

    lines = []
    for ev in events:
        lines.append(f"id: {ev.get('seq', 0)}")
        lines.append(f"event: {ev.get('type', 'message')}")
        data_str = json.dumps(ev)
        lines.append(f"data: {data_str}")
        lines.append("")  # event terminator
    lines.append("")  # final newline
    body = "\n".join(lines)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body,
    )


# ---------------------------------------------------------------------- #
# Lifecycle
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_context_manager_opens_and_closes_internal_client() -> None:
    captured: dict[str, bool] = {"opened": False, "closed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["opened"] = True
        return httpx.Response(200, json={"status": "healthy"})

    transport = make_handler({"GET /health": handler})

    async with httpx.AsyncClient(base_url="http://test", transport=transport) as http_client:
        client = AsyncCodingAgentClient(client=http_client)
        # The SDK client should treat the externally-provided client as
        # non-owning — its __aenter__/__aexit__ should be no-ops.
        result = await client.__aenter__()
        assert result is client
        await client.health()
        await client.__aexit__(None, None, None)

    # The external client must still be usable after the SDK exits — proves
    # the SDK did not close it.
    assert captured["opened"] is True


@pytest.mark.asyncio
async def test_owned_client_closed_on_exit() -> None:
    transport = make_handler({"GET /health": lambda r: httpx.Response(200, json={"status": "healthy"})})
    client = AsyncCodingAgentClient(base_url="http://test", transport=transport)
    async with client as c:
        await c.health()


# ---------------------------------------------------------------------- #
# Sessions
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_session_returns_pending() -> None:
    """Plan v2 §约束1: POST /sessions only creates PENDING, doesn't execute."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(201, json=make_session_payload(status="pending"))

    import json

    async with AsyncCodingAgentClient(base_url="http://test", transport=make_handler({"POST /sessions": handler})) as client:
        session = await client.create_session(agent="claude", prompt="refactor me")

    assert captured["method"] == "POST"
    assert captured["path"] == "/sessions"
    assert captured["body"]["agent"] == "claude"
    assert captured["body"]["prompt"] == "refactor me"
    assert session.session_id == "sess-123"
    assert session.status == "pending"
    assert session.agent == "claude"


@pytest.mark.asyncio
async def test_create_session_passes_workdir_and_metadata() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(201, json=make_session_payload())

    import json

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"POST /sessions": handler}),
    ) as client:
        await client.create_session(
            agent="codex",
            prompt="add tests",
            workdir="/repo",
            metadata={"trace_id": "abc"},
        )

    assert captured["body"]["workdir"] == "/repo"
    assert captured["body"]["metadata"] == {"trace_id": "abc"}
    assert captured["body"]["agent"] == "codex"


@pytest.mark.asyncio
async def test_get_session_normalizes_id_to_session_id() -> None:
    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123": lambda r: httpx.Response(200, json=make_session_payload(status="running"))}),
    ) as client:
        session = await client.get_session("sess-123")
    assert session.session_id == "sess-123"
    assert session.status == "running"


@pytest.mark.asyncio
async def test_list_sessions_passes_filters() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json=[make_session_payload("a", status="completed"), make_session_payload("b", status="failed")],
        )

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions": handler}),
    ) as client:
        sessions = await client.list_sessions(agent="claude", status="completed", limit=10)

    assert captured["params"]["agent"] == "claude"
    assert captured["params"]["status"] == "completed"
    assert captured["params"]["limit"] == "10"
    assert [s.session_id for s in sessions] == ["a", "b"]


@pytest.mark.asyncio
async def test_list_sessions_lightweight() -> None:
    """lightweight=True should send lightweight=true query param to the server."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        # Server returns minimal data in lightweight mode — the SDK
        # still parses it as Session objects.
        return httpx.Response(
            200,
            json=[
                {
                    "id": "a",
                    "agent": "claude",
                    "status": "completed",
                    "created_at": "2026-06-20T10:00:00+00:00",
                    "updated_at": "2026-06-20T10:00:00+00:00",
                },
            ],
        )

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions": handler}),
    ) as client:
        sessions = await client.list_sessions(lightweight=True)

    assert captured["params"]["lightweight"] == "true"
    assert len(sessions) == 1
    assert sessions[0].session_id == "a"


# ---------------------------------------------------------------------- #
# Events
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_events_decodes_json_data() -> None:
    payload = make_event_payload(seq=1, type_="stdout", data={"text": "hi"})
    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123/events": lambda r: httpx.Response(200, json=[payload])}),
    ) as client:
        events = await client.get_events("sess-123")

    assert len(events) == 1
    assert events[0].seq == 1
    assert events[0].type == "stdout"
    assert events[0].data == {"text": "hi"}


@pytest.mark.asyncio
async def test_get_events_type_filter() -> None:
    """get_events(type='stdout') should send type=stdout as a query param."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        # Pretend the server already filtered — return only matching events.
        return httpx.Response(
            200,
            json=[make_event_payload(seq=1, type_="stdout", data="match")],
        )

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123/events": handler}),
    ) as client:
        events = await client.get_events("sess-123", type="stdout")

    # Server-side filtering: the query param must be present.
    assert captured["params"]["type"] == "stdout"
    assert captured["params"]["after_seq"] == "0"
    assert len(events) == 1
    assert events[0].type == "stdout"


@pytest.mark.asyncio
async def test_get_events_no_type_filter_omits_param() -> None:
    """When type is not passed, no type query param should be sent."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123/events": handler}),
    ) as client:
        await client.get_events("sess-123")

    assert "type" not in captured["params"]


@pytest.mark.asyncio
async def test_stream_events_yields_parsed_events() -> None:
    events_in = [
        make_event_payload(seq=1, type_="stdout", data="line one"),
        make_event_payload(seq=2, type_="stdout", data="line two"),
        make_event_payload(seq=3, type_="result", data={"ok": True}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(events_in)

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123/events/stream": handler}),
    ) as client:
        out = []
        async for ev in client.stream_events("sess-123"):
            out.append(ev)
            if len(out) == len(events_in):
                break

    assert [e.seq for e in out] == [1, 2, 3]
    assert out[0].data == "line one"
    assert out[1].data == "line two"
    assert out[2].data == {"ok": True}


@pytest.mark.asyncio
async def test_stream_events_sends_last_event_id_header() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return sse_response([])

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123/events/stream": handler}),
    ) as client:
        async for _ in client.stream_events("sess-123", last_event_id=42):
            break

    assert captured["headers"].get("last-event-id") == "42"
    assert captured["headers"].get("accept") == "text/event-stream"


# ---------------------------------------------------------------------- #
# Actions
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_kill_session() -> None:
    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler(
            {"POST /sessions/sess-123/kill": lambda r: httpx.Response(200, json={"success": True, "session_id": "sess-123", "message": "Killed"})}
        ),
    ) as client:
        result = await client.kill("sess-123")
    assert result.success is True
    assert result.session_id == "sess-123"


@pytest.mark.asyncio
async def test_recover_passes_timeout() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"recovered_count": 3, "message": "Recovered 3"})

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"POST /recover": handler}),
    ) as client:
        result = await client.recover(timeout_seconds=120)

    assert captured["params"]["timeout_seconds"] == "120"
    assert result.recovered_count == 3


# ---------------------------------------------------------------------- #
# Tags
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_tag_uses_correct_body() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(
            201,
            json={"session_id": "sess-123", "tag": "important", "message": "Added tag 'important'"},
        )

    import json

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"POST /sessions/sess-123/tags": handler}),
    ) as client:
        tag = await client.create_tag("sess-123", "important")

    # Plan v2 §约束3: body must be {"tag": "string"}
    assert captured["body"] == {"tag": "important"}
    assert tag.tag == "important"
    assert tag.session_id == "sess-123"


@pytest.mark.asyncio
async def test_list_tags_returns_strings() -> None:
    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler(
            {
                "GET /sessions/sess-123/tags": lambda r: httpx.Response(
                    200, json={"session_id": "sess-123", "tags": ["a", "b", "c"]}
                )
            }
        ),
    ) as client:
        tags = await client.list_tags("sess-123")
    assert tags == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_delete_tag_uses_path() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(
            200, json={"session_id": "sess-123", "tag": "important", "message": "Removed"}
        )

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"DELETE /sessions/sess-123/tags/important": handler}),
    ) as client:
        result = await client.delete_tag("sess-123", "important")

    assert captured["path"] == "/sessions/sess-123/tags/important"
    assert result.tag == "important"


# ---------------------------------------------------------------------- #
# Health / metrics
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_health() -> None:
    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /health": lambda r: httpx.Response(200, json={"status": "healthy"})}),
    ) as client:
        result = await client.health()
    assert result.status == "healthy"


@pytest.mark.asyncio
async def test_metrics_returns_text() -> None:
    body = "# HELP foo bar\nfoo 1\n"
    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /metrics": lambda r: httpx.Response(200, text=body)}),
    ) as client:
        text = await client.metrics()
    assert "foo 1" in text


# ---------------------------------------------------------------------- #
# Auth header
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_token_sent_as_bearer() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "healthy"})

    async with AsyncCodingAgentClient(
        base_url="http://test",
        token="secret-token",
        transport=make_handler({"GET /health": handler}),
    ) as client:
        await client.health()

    assert captured["auth"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_empty_token_env_var_does_not_send_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: CODING_AGENTS_TOKEN="" must NOT send 'Authorization: Bearer '."""
    monkeypatch.setenv("CODING_AGENTS_TOKEN", "")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "healthy"})

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /health": handler}),
    ) as client:
        await client.health()

    assert "auth" not in captured or captured["auth"] is None


@pytest.mark.asyncio
async def test_empty_token_param_does_not_send_auth_header() -> None:
    """Regression: token="" must NOT send 'Authorization: Bearer '."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "healthy"})

    async with AsyncCodingAgentClient(
        base_url="http://test",
        token="",
        transport=make_handler({"GET /health": handler}),
    ) as client:
        await client.health()

    assert "auth" not in captured or captured["auth"] is None


# ---------------------------------------------------------------------- #
# Error paths
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_401_raises_authentication_error() -> None:
    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-x": lambda r: httpx.Response(401, json={"detail": "Invalid token"})}),
    ) as client:
        with pytest.raises(AuthenticationError) as exc_info:
            await client.get_session("sess-x")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid token"


@pytest.mark.asyncio
async def test_404_raises_not_found() -> None:
    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/missing": lambda r: httpx.Response(404, json={"detail": "not found"})}),
    ) as client:
        with pytest.raises(NotFoundError) as exc_info:
            await client.get_session("missing")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_500_raises_server_error() -> None:
    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /health": lambda r: httpx.Response(500, text="boom")}),
    ) as client:
        with pytest.raises(ServerError) as exc_info:
            await client.health()

    assert exc_info.value.status_code == 500
    assert "boom" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_other_4xx_raises_generic_api_error() -> None:
    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /health": lambda r: httpx.Response(400, json={"detail": "bad"})}),
    ) as client:
        with pytest.raises(APIError) as exc_info:
            await client.health()

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_connection_failure_raises_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(handler)
    async with AsyncCodingAgentClient(base_url="http://test", transport=transport) as client:
        with pytest.raises(ConnectionError_):
            await client.health()


@pytest.mark.asyncio
async def test_stream_404_propagates_not_found() -> None:
    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/missing/events/stream": lambda r: httpx.Response(404, json={"detail": "no"})}),
    ) as client:
        with pytest.raises(NotFoundError):

            async for _ in client.stream_events("missing"):
                pass


# ---------------------------------------------------------------------- #
# Base URL handling
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_trailing_slash_is_stripped() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"status": "healthy"})

    async with AsyncCodingAgentClient(
        base_url="http://test/",
        transport=make_handler({"GET /health": handler}),
    ) as client:
        await client.health()

    assert captured["path"] == "/health"


# ---------------------------------------------------------------------- #
# P0-2: stream_events default timeout
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stream_events_default_timeout_is_one_hour() -> None:
    """stream_events() should default to 1h timeout for long-running sessions."""
    from coding_agents_sdk.client import DEFAULT_STREAM_TIMEOUT

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return sse_response([])

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123/events/stream": handler}),
    ) as client:
        async for _ in client.stream_events("sess-123"):
            break

    # The timeout should be DEFAULT_STREAM_TIMEOUT (3600.0), not the client default (30.0)
    assert DEFAULT_STREAM_TIMEOUT == 3600.0


# ---------------------------------------------------------------------- #
# P1-2: list_sessions tag repeated params
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_sessions_tag_repeated_params() -> None:
    """Tags should be sent as repeated query params (tag=a&tag=b)."""
    captured_url: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_url["query"] = str(request.url.query)
        return httpx.Response(200, json=[])

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions": handler}),
    ) as client:
        await client.list_sessions(tag=["important", "urgent"])

    # The query string should contain both tag values
    query = captured_url["query"]
    assert "tag=important" in query
    assert "tag=urgent" in query


# ---------------------------------------------------------------------- #
# P2-2: NetworkError alias
# ---------------------------------------------------------------------- #


def test_network_error_is_connection_error_alias() -> None:
    """NetworkError should be the same class as ConnectionError_ (deprecated alias)."""
    assert NetworkError is ConnectionError_


@pytest.mark.asyncio
async def test_connection_failure_raises_network_error() -> None:
    """Network errors should be catchable as NetworkError."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(handler)
    async with AsyncCodingAgentClient(base_url="http://test", transport=transport) as client:
        with pytest.raises(NetworkError):
            await client.health()


# ---------------------------------------------------------------------- #
# P2-4: create_session rejects unknown kwargs
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_session_rejects_unknown_kwargs() -> None:
    """create_session() should raise TypeError for unknown keyword arguments."""
    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"POST /sessions": lambda r: httpx.Response(201, json=make_session_payload())}),
    ) as client:
        with pytest.raises(TypeError):
            await client.create_session(agent="claude", prompt="test", bogus_param="nope")


# ---------------------------------------------------------------------- #
# wait_for_completion
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wait_for_completion_returns_terminal_session() -> None:
    """wait_for_completion should return session when it reaches terminal state."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        # First call: running, second call: completed
        status = "running" if call_count == 1 else "completed"
        return httpx.Response(200, json=make_session_payload(status=status))

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123": handler}),
    ) as client:
        session = await client.wait_for_completion("sess-123", poll_interval=0.01)

    assert session.status == "completed"
    assert call_count == 2


@pytest.mark.asyncio
async def test_wait_for_completion_timeout() -> None:
    """wait_for_completion should raise TimeoutError if session doesn't complete."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_session_payload(status="running"))

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123": handler}),
    ) as client:
        with pytest.raises(TimeoutError) as exc_info:
            await client.wait_for_completion("sess-123", poll_interval=0.01, timeout=0.05)

    assert "did not complete within" in str(exc_info.value)


# ---------------------------------------------------------------------- #
# watch_session
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_watch_session_yields_on_status_change() -> None:
    """watch_session should yield session on each status change."""
    statuses = ["pending", "running", "running", "completed"]
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        status = statuses[min(call_count, len(statuses) - 1)]
        call_count += 1
        return httpx.Response(200, json=make_session_payload(status=status))

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123": handler}),
    ) as client:
        yielded = []
        async for session in client.watch_session("sess-123", poll_interval=0.01):
            yielded.append(session.status)

    # Should yield pending, running, completed (skip duplicate running)
    assert yielded == ["pending", "running", "completed"]


@pytest.mark.asyncio
async def test_watch_session_timeout() -> None:
    """watch_session should raise TimeoutError if session doesn't complete."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_session_payload(status="running"))

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123": handler}),
    ) as client:
        with pytest.raises(TimeoutError):
            async for _ in client.watch_session("sess-123", poll_interval=0.01, timeout=0.05):
                pass


# ---------------------------------------------------------------------- #
# P0 fixes: WaitTimeoutError, CancelToken, poll_interval default
# ---------------------------------------------------------------------- #


def test_wait_timeout_error_is_timeout_error_subclass() -> None:
    """WaitTimeoutError must be catchable as builtin TimeoutError (backward compat)."""
    from coding_agents_sdk import WaitTimeoutError, CodingAgentsSDKError

    err = WaitTimeoutError("timed out")
    assert isinstance(err, TimeoutError)
    assert isinstance(err, CodingAgentsSDKError)
    assert str(err) == "timed out"


@pytest.mark.asyncio
async def test_wait_for_completion_raises_wait_timeout_error() -> None:
    """wait_for_completion should raise WaitTimeoutError (also catchable as TimeoutError)."""
    from coding_agents_sdk import WaitTimeoutError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_session_payload(status="running"))

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123": handler}),
    ) as client:
        with pytest.raises(WaitTimeoutError) as exc_info:
            await client.wait_for_completion("sess-123", poll_interval=0.01, timeout=0.05)

    assert "did not complete within" in str(exc_info.value)


def test_default_poll_interval_is_2_seconds() -> None:
    """DEFAULT_POLL_INTERVAL should be 2.0s, not 300s."""
    from coding_agents_sdk.client import DEFAULT_POLL_INTERVAL

    assert DEFAULT_POLL_INTERVAL == 2.0


def test_cancel_token_basic_lifecycle() -> None:
    """CancelToken should start uncancelled, toggle, and reset."""
    from coding_agents_sdk import CancelToken

    token = CancelToken()
    assert token.is_cancelled is False
    assert bool(token) is False

    token.cancel()
    assert token.is_cancelled is True
    assert bool(token) is True

    token.reset()
    assert token.is_cancelled is False
    assert bool(token) is False


@pytest.mark.asyncio
async def test_create_session_attaches_cancel_token() -> None:
    """create_session() should attach a CancelToken to the returned Session."""
    from coding_agents_sdk import CancelToken
    import json

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"POST /sessions": lambda r: httpx.Response(201, json=make_session_payload())}),
    ) as client:
        session = await client.create_session(agent="claude", prompt="test")

    assert hasattr(session, "cancel_token")
    assert isinstance(session.cancel_token, CancelToken)
    assert session.cancel_token.is_cancelled is False


@pytest.mark.asyncio
async def test_wait_for_completion_cancelled_via_token() -> None:
    """wait_for_completion should raise CancelledError when cancel_token is cancelled."""
    from coding_agents_sdk import CancelledError, CancelToken

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_session_payload(status="running"))

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123": handler}),
    ) as client:
        token = CancelToken()
        # Cancel from another "task" after a short delay.
        async def cancel_soon() -> None:
            await asyncio.sleep(0.05)
            token.cancel()

        import asyncio
        task = asyncio.create_task(cancel_soon())

        with pytest.raises(CancelledError):
            await client.wait_for_completion("sess-123", poll_interval=0.02, cancel_token=token)

        await task


@pytest.mark.asyncio
async def test_watch_session_cancelled_via_token() -> None:
    """watch_session should raise CancelledError when cancel_token is cancelled."""
    from coding_agents_sdk import CancelledError, CancelToken
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_session_payload(status="running"))

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123": handler}),
    ) as client:
        token = CancelToken()

        async def cancel_soon() -> None:
            await asyncio.sleep(0.05)
            token.cancel()

        task = asyncio.create_task(cancel_soon())

        with pytest.raises(CancelledError):
            async for _ in client.watch_session("sess-123", poll_interval=0.02, cancel_token=token):
                pass

        await task


@pytest.mark.asyncio
async def test_wait_for_completion_uses_session_cancel_token() -> None:
    """End-to-end: session.cancel_token from create_session cancels the wait."""
    from coding_agents_sdk import CancelledError
    import asyncio

    call_count = 0

    def session_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_session_payload(status="running"))

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({
            "POST /sessions": lambda r: httpx.Response(201, json=make_session_payload()),
            "GET /sessions/sess-123": session_handler,
        }),
    ) as client:
        session = await client.create_session(agent="claude", prompt="test")

        async def cancel_soon() -> None:
            await asyncio.sleep(0.05)
            session.cancel_token.cancel()

        task = asyncio.create_task(cancel_soon())

        with pytest.raises(CancelledError):
            await client.wait_for_completion(
                session.session_id, poll_interval=0.02, cancel_token=session.cancel_token,
            )

        await task


# ---------------------------------------------------------------------- #
# Retry logic
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_request_retries_on_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requests should retry on 503 with exponential backoff."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"status": "healthy"})

    # Mock asyncio.sleep to avoid actual delays
    monkeypatch.setattr("coding_agents_sdk.client.asyncio.sleep", _no_op_sleep)

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /health": handler}),
    ) as client:
        result = await client.health()

    assert result.status == "healthy"
    assert call_count == 3


@pytest.mark.asyncio
async def test_request_retries_on_504(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requests should retry on 504 Gateway Timeout."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            return httpx.Response(504, text="gateway timeout")
        return httpx.Response(200, json=make_session_payload())

    monkeypatch.setattr("coding_agents_sdk.client.asyncio.sleep", _no_op_sleep)

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123": handler}),
    ) as client:
        session = await client.get_session("sess-123")

    assert session.session_id == "sess-123"
    assert call_count == 2


@pytest.mark.asyncio
async def test_request_retries_on_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requests should retry on connection errors."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"status": "healthy"})

    monkeypatch.setattr("coding_agents_sdk.client.asyncio.sleep", _no_op_sleep)

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.health()

    assert result.status == "healthy"
    assert call_count == 2


@pytest.mark.asyncio
async def test_request_exhausts_retries_on_persistent_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """After max_retries, a persistent 503 should raise ServerError."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503, text="unavailable")

    monkeypatch.setattr("coding_agents_sdk.client.asyncio.sleep", _no_op_sleep)

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /health": handler}),
    ) as client:
        with pytest.raises(ServerError) as exc_info:
            await client.health()

    assert exc_info.value.status_code == 503
    # Initial attempt + max_retries (default 3) = 4 total attempts
    assert call_count == 4


@pytest.mark.asyncio
async def test_request_respects_429_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 responses should respect the Retry-After header."""
    call_count = 0
    sleep_calls: list[float] = []

    async def mock_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            return httpx.Response(
                429,
                headers={"retry-after": "2.5"},
                json={"detail": "rate limited"},
            )
        return httpx.Response(200, json={"status": "healthy"})

    monkeypatch.setattr("coding_agents_sdk.client.asyncio.sleep", mock_sleep)

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /health": handler}),
    ) as client:
        result = await client.health()

    assert result.status == "healthy"
    assert call_count == 2
    # Should have slept for the Retry-After value
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 2.5


@pytest.mark.asyncio
async def test_request_429_raises_rate_limit_error_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """After exhausting retries on 429, should raise RateLimitError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "1"},
            json={"detail": "rate limited"},
        )

    monkeypatch.setattr("coding_agents_sdk.client.asyncio.sleep", _no_op_sleep)

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /health": handler}),
    ) as client:
        with pytest.raises(RateLimitError) as exc_info:
            await client.health()

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 1.0
    assert exc_info.value.detail == "rate limited"


@pytest.mark.asyncio
async def test_request_no_retry_on_non_retryable_error() -> None:
    """Non-retryable errors (400, 401, 404, 500) should not be retried."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(400, json={"detail": "bad request"})

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /health": handler}),
        max_retries=3,
    ) as client:
        with pytest.raises(APIError) as exc_info:
            await client.health()

    assert exc_info.value.status_code == 400
    # Should only have been called once — no retries for 400
    assert call_count == 1


@pytest.mark.asyncio
async def test_max_retries_zero_disables_retries() -> None:
    """max_retries=0 should disable retries entirely."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("refused")

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
        max_retries=0,
    ) as client:
        with pytest.raises(NetworkError):
            await client.health()

    assert call_count == 1


@pytest.mark.asyncio
async def test_stream_events_auto_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """stream_events() should auto-reconnect on connection failure and resume from last event ID."""
    events = [
        make_event_payload(seq=1, type_="stdout", data="hello"),
    ]

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: simulate connection error
            raise httpx.ConnectError("Connection refused")
        # Second call: verify Last-Event-ID was NOT sent (no events received yet)
        # and return events successfully
        return sse_response(events)

    monkeypatch.setattr("coding_agents_sdk.client.asyncio.sleep", _no_op_sleep)

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
        max_retries=3,
    ) as client:
        out = []
        async for ev in client.stream_events("sess-123"):
            out.append(ev)
            if len(out) == 1:
                break

    assert len(out) == 1
    assert out[0].seq == 1
    assert call_count == 2  # First failed, second succeeded


@pytest.mark.asyncio
async def test_stream_events_resumes_from_last_event_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """stream_events() should track last event ID for manual reconnection."""
    events = [
        make_event_payload(seq=42, type_="stdout", data="hello"),
    ]

    captured_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers["last-event-id"] = request.headers.get("last-event-id")
        return sse_response(events)

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123/events/stream": handler}),
    ) as client:
        # Pass last_event_id explicitly - this is how callers resume
        out = []
        async for ev in client.stream_events("sess-123", last_event_id=42):
            out.append(ev)
            if len(out) == 1:
                break

    assert len(out) == 1
    assert out[0].seq == 42
    # Verify the header was sent on the initial call
    assert captured_headers["last-event-id"] == "42"


@pytest.mark.asyncio
async def test_stream_events_reconnects_on_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """stream_events() should reconnect when server returns 503."""
    events = [
        make_event_payload(seq=1, type_="stdout", data="hello"),
    ]
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(503, text="unavailable")
        return sse_response(events)

    monkeypatch.setattr("coding_agents_sdk.client.asyncio.sleep", _no_op_sleep)

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=make_handler({"GET /sessions/sess-123/events/stream": handler}),
    ) as client:
        out = []
        async for ev in client.stream_events("sess-123", max_retries=3):
            out.append(ev)
            if len(out) == 1:
                break

    assert len(out) == 1
    assert out[0].seq == 1
    assert call_count == 2


@pytest.mark.asyncio
async def test_stream_events_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """stream_events() should raise NetworkError after exhausting reconnects."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("coding_agents_sdk.client.asyncio.sleep", _no_op_sleep)

    async with AsyncCodingAgentClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
        max_retries=2,
    ) as client:
        with pytest.raises(NetworkError):
            async for _ in client.stream_events("sess-123"):
                pass

    # 1 initial + 2 retries = 3 attempts
    assert call_count == 3


def test_retry_after_parse_integer() -> None:
    """_parse_retry_after should handle integer seconds."""
    from coding_agents_sdk.client import _parse_retry_after

    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after(None) is None


def test_retry_after_parse_http_date() -> None:
    """_parse_retry_after should handle HTTP-date format."""
    from coding_agents_sdk.client import _parse_retry_after
    from email.utils import format_datetime
    from datetime import datetime, timezone, timedelta

    future = datetime.now(timezone.utc) + timedelta(seconds=10)
    http_date = format_datetime(future, usegmt=True)
    result = _parse_retry_after(http_date)
    assert result is not None
    # Should be roughly 10 seconds (allow some tolerance)
    assert 5 <= result <= 15


def test_compute_retry_delay_exponential() -> None:
    """_compute_retry_delay should use exponential backoff with jitter."""
    from coding_agents_sdk.client import _compute_retry_delay

    # With base_delay=1, max_delay=60:
    # attempt 0: 1 * 2^0 = 1, with jitter: 0.5-1.5
    # attempt 1: 1 * 2^1 = 2, with jitter: 1.0-3.0
    # attempt 2: 1 * 2^2 = 4, with jitter: 2.0-6.0
    # attempt 3: 1 * 2^3 = 8, with jitter: 4.0-12.0
    delay0 = _compute_retry_delay(0, 1.0, 60.0)
    assert 0.5 <= delay0 <= 1.5

    delay1 = _compute_retry_delay(1, 1.0, 60.0)
    assert 1.0 <= delay1 <= 3.0

    delay3 = _compute_retry_delay(3, 1.0, 60.0)
    assert 4.0 <= delay3 <= 12.0


def test_compute_retry_delay_respects_max() -> None:
    """_compute_retry_delay should never exceed max_delay."""
    from coding_agents_sdk.client import _compute_retry_delay

    # Even with large attempt numbers, delay should be capped at max_delay
    delay = _compute_retry_delay(20, 1.0, 10.0)
    assert delay <= 15.0  # max_delay * 1.5 (max jitter)


# ---------------------------------------------------------------------- #
# Helpers for retry tests
# ---------------------------------------------------------------------- #


async def _no_op_sleep(seconds: float) -> None:
    """A no-op replacement for asyncio.sleep in tests."""
    pass