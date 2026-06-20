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
    NotFoundError,
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

    assert captured["params"]["timeout"] == "120"
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