"""Async HTTP client for the coding-agents HTTP API.

This is a pure HTTP wrapper. It does NOT trigger session execution — the
HTTP ``POST /sessions`` endpoint only creates a ``PENDING`` session record,
and the caller is responsible for ensuring an executor consumes it
(see plan v2 ``§约束 1``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from coding_agents_sdk.exceptions import (
    APIError,
    AuthenticationError,
    ConnectionError_,
    NotFoundError,
    ServerError,
)
from coding_agents_sdk.models import (
    Event,
    HealthStatus,
    KillResult,
    RecoverResult,
    Session,
    Tag,
    TagsList,
)

DEFAULT_BASE_URL = "http://localhost:8765"
DEFAULT_TIMEOUT = 30.0


class AsyncCodingAgentClient:
    """Async-only client for the coding-agents HTTP API.

    Parameters
    ----------
    base_url:
        Root URL of the coding-agents HTTP server, e.g.
        ``http://localhost:8765``. No trailing slash required.
    token:
        Bearer token for authentication. If ``None``, no ``Authorization``
        header is sent (the server will reject with 401 — see plan v2).
    timeout:
        Default per-request timeout in seconds. Defaults to 30s. Streaming
        endpoints override this with a longer timeout (see ``stream_events``).
    headers:
        Optional extra headers to send on every request.
    client:
        Optional pre-configured ``httpx.AsyncClient`` to use. When provided,
        ``base_url``/``token``/``headers`` are ignored and the caller is
        responsible for the client's lifecycle. ``__aenter__`` / ``__aexit__``
        become no-ops.
    transport:
        Optional ``httpx.MockTransport`` (or any ``httpx.BaseTransport``) to
        inject for testing. Equivalent to passing a pre-built ``client=`` with
        that transport, but a bit more ergonomic for tests.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if client is None:
            merged_headers: dict[str, str] = dict(headers or {})
            if token is not None:
                merged_headers["Authorization"] = f"Bearer {token}"
            client_kwargs: dict[str, Any] = {
                "base_url": base_url.rstrip("/"),
                "timeout": timeout,
            }
            if merged_headers:
                client_kwargs["headers"] = merged_headers
            if transport is not None:
                client_kwargs["transport"] = transport
            self._client = httpx.AsyncClient(**client_kwargs)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False

        self._base_url = base_url.rstrip("/")
        self._token = token

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "AsyncCodingAgentClient":
        if self._owns_client:
            await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_client:
            await self._client.__aexit__(exc_type, exc, tb)

    async def aclose(self) -> None:
        """Close the underlying HTTP client. No-op if the client is externally owned."""
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #

    async def create_session(
        self,
        agent: str,
        prompt: str = "",
        *,
        workdir: str = ".",
        metadata: dict[str, Any] | None = None,
        **_: Any,
    ) -> Session:
        """Create a new session (PENDING status).

        ⚠️ The HTTP ``POST /sessions`` endpoint does **not** trigger execution.
        The returned session will have ``status == "pending"`` until a separate
        executor consumes it. See plan v2 ``§约束 1``.
        """
        payload = {
            "agent": agent,
            "prompt": prompt,
            "workdir": workdir,
            "metadata": metadata or {},
        }
        body = await self._post("/sessions", json=payload)
        return Session.model_validate(_normalize_session(body))

    async def get_session(self, session_id: str) -> Session:
        """Fetch a session by id."""
        body = await self._get(f"/sessions/{session_id}")
        return Session.model_validate(_normalize_session(body))

    async def list_sessions(
        self,
        *,
        agent: str | None = None,
        status: str | None = None,
        tag: list[str] | None = None,
        limit: int = 100,
    ) -> list[Session]:
        """List sessions with optional filters."""
        params: dict[str, Any] = {"limit": limit}
        if agent is not None:
            params["agent"] = agent
        if status is not None:
            params["status"] = status
        if tag:
            # httpx supports repeated query params via list values
            params["tag"] = tag
        body = await self._get("/sessions", params=params)
        return [Session.model_validate(_normalize_session(item)) for item in body]

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    async def get_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[Event]:
        """Return events for a session via REST."""
        params: dict[str, Any] = {"after_seq": after_seq}
        if limit is not None:
            params["limit"] = limit
        body = await self._get(f"/sessions/{session_id}/events", params=params)
        return [Event.from_response(item) for item in body]

    async def stream_events(
        self,
        session_id: str,
        *,
        last_event_id: int | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[Event]:
        """Stream events for a session via Server-Sent Events.

        Uses ``GET /sessions/{session_id}/events/stream``. The iterator yields
        :class:`Event` instances until the server closes the stream or the
        caller breaks out of the loop.
        """
        headers: dict[str, str] = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)

        request = self._client.build_request(
            "GET",
            f"/sessions/{session_id}/events/stream",
            headers=headers,
            timeout=timeout,
        )

        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise ConnectionError_(f"Failed to open SSE stream: {exc}") from exc

        try:
            if response.status_code >= 400:
                await _raise_for_status(response)

            async for event in _iter_sse(response):
                if not event.get("data"):
                    # Heartbeat / comment / empty event — skip.
                    continue
                yield _sse_to_event(session_id, event)
        finally:
            await response.aclose()

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    async def kill(self, session_id: str) -> KillResult:
        """Mark a session as KILLED. Only effective for PENDING/RUNNING."""
        body = await self._post(f"/sessions/{session_id}/kill")
        return KillResult.model_validate(body)

    async def recover(self, *, timeout_seconds: int = 300) -> RecoverResult:
        """Recover orphaned sessions. Returns count recovered."""
        body = await self._post("/recover", params={"timeout": timeout_seconds})
        return RecoverResult.model_validate(body)

    # ------------------------------------------------------------------ #
    # Tags
    # ------------------------------------------------------------------ #

    async def create_tag(self, session_id: str, tag: str) -> Tag:
        """Add a tag to a session.

        Body is ``{"tag": "<name>"}`` per plan v2 ``§约束 3``.
        """
        body = await self._post(
            f"/sessions/{session_id}/tags",
            json={"tag": tag},
        )
        return Tag.model_validate(body)

    async def list_tags(self, session_id: str) -> list[str]:
        """List tags for a session."""
        body = await self._get(f"/sessions/{session_id}/tags")
        # Server may return either {"session_id": "...", "tags": [...]} or a bare list.
        if isinstance(body, list):
            return [str(t) for t in body]
        return TagsList.model_validate(body).tags

    async def delete_tag(self, session_id: str, tag: str) -> Tag:
        """Remove a tag from a session."""
        body = await self._delete(f"/sessions/{session_id}/tags/{tag}")
        return Tag.model_validate(body)

    # ------------------------------------------------------------------ #
    # Health
    # ------------------------------------------------------------------ #

    async def health(self) -> HealthStatus:
        """Server health check (no auth required)."""
        # Health endpoint is the one place we DON'T send the auth header —
        # the server treats /health as anonymous.
        request = self._client.build_request("GET", "/health")
        try:
            response = await self._client.send(request)
        except httpx.HTTPError as exc:
            raise ConnectionError_(f"Failed to reach server: {exc}") from exc

        try:
            if response.status_code >= 400:
                await _raise_for_status(response)
            return HealthStatus.model_validate(response.json())
        finally:
            await response.aclose()

    async def metrics(self) -> str:
        """Return Prometheus metrics text (passthrough)."""
        request = self._client.build_request("GET", "/metrics")
        try:
            response = await self._client.send(request)
        except httpx.HTTPError as exc:
            raise ConnectionError_(f"Failed to reach server: {exc}") from exc

        try:
            if response.status_code >= 400:
                await _raise_for_status(response)
            return response.text
        finally:
            await response.aclose()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, *, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> Any:
        return await self._request("POST", path, json=json, params=params)

    async def _delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                path,
                json=json,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise ConnectionError_(f"HTTP request failed: {exc}") from exc

        try:
            if response.status_code >= 400:
                await _raise_for_status(response)
            if response.status_code == 204 or not response.content:
                return None
            return response.json()
        finally:
            await response.aclose()


# ---------------------------------------------------------------------- #
# Module-level helpers
# ---------------------------------------------------------------------- #


def _normalize_session(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate server-side ``id`` field into SDK's ``session_id``."""
    if "session_id" not in payload and "id" in payload:
        payload = {**payload, "session_id": payload["id"]}
    return payload


async def _raise_for_status(response: httpx.Response) -> None:
    """Translate an error response into the appropriate SDK exception."""
    try:
        body = response.json()
    except Exception:
        body = response.text

    detail: str | None = None
    if isinstance(body, dict):
        raw_detail = body.get("detail")
        if isinstance(raw_detail, str):
            detail = raw_detail
        elif raw_detail is not None:
            detail = str(raw_detail)
    elif isinstance(body, str) and body:
        detail = body

    status = response.status_code
    if status == 401:
        raise AuthenticationError(detail=detail, response_body=body)
    if status == 404:
        raise NotFoundError(detail=detail, response_body=body)
    if status >= 500:
        raise ServerError(status, detail=detail, response_body=body)
    raise APIError(status, detail=detail, response_body=body)


async def _iter_sse(response: httpx.Response) -> AsyncIterator[dict[str, str]]:
    """Yield SSE events as ``{"event": "...", "data": "...", "id": "..."}``.

    This is intentionally minimal — just enough to read the
    ``data: <json>`` lines emitted by ``sse-starlette``.
    """
    event: dict[str, str] = {}
    async for raw_line in response.aiter_lines():
        if raw_line == "":
            if event:
                yield event
                event = {}
            continue
        if raw_line.startswith(":"):
            # SSE comment / heartbeat.
            continue
        # Strip SSE field prefix
        field, sep, value = raw_line.partition(":")
        if not sep:
            continue
        # Per spec, drop a single leading space after the colon.
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            # Multi-line data is joined with newlines per spec.
            if "data" in event:
                event["data"] = event["data"] + "\n" + value
            else:
                event["data"] = value
        elif field in {"event", "id", "retry"}:
            event[field] = value
    if event:
        yield event


def _sse_to_event(session_id: str, raw: dict[str, str]) -> Event:
    """Convert a parsed SSE record into an :class:`Event`.

    The server emits ``data: <json>`` where the JSON contains ``seq``, ``type``
    and other fields. We try to decode it and merge into the Event model.
    """
    data_raw = raw.get("data", "")
    payload: dict[str, Any]
    try:
        decoded = json.loads(data_raw)
        if isinstance(decoded, dict):
            payload = decoded
        else:
            payload = {"data": decoded}
    except (ValueError, TypeError):
        payload = {"data": data_raw}

    payload.setdefault("session_id", session_id)
    payload.setdefault("type", raw.get("event", "message"))
    if "seq" not in payload:
        # Try to use the SSE event id as the sequence number when present.
        if "id" in raw:
            try:
                payload["seq"] = int(raw["id"])
            except ValueError:
                pass
    return Event.from_response(payload)


__all__ = ["AsyncCodingAgentClient"]