"""Async HTTP client for the coding-agents HTTP API.

This is a pure HTTP wrapper. It does NOT trigger session execution — the
HTTP ``POST /sessions`` endpoint only creates a ``PENDING`` session record,
and the caller is responsible for ensuring an executor consumes it
(see plan v2 ``§约束 1``).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from coding_agents_sdk.exceptions import (
    APIError,
    AuthenticationError,
    CancelledError,
    ConnectionError_,
    NetworkError,
    NotFoundError,
    RateLimitError,
    ServerError,
    WaitTimeoutError,
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
DEFAULT_STREAM_TIMEOUT = 3600.0  # 1 hour — matches server-side 30 min poll window
DEFAULT_POLL_INTERVAL = 2.0

# Retry configuration defaults
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY = 0.5  # seconds
DEFAULT_RETRY_MAX_DELAY = 30.0  # seconds

# Status codes that should trigger a retry with exponential backoff
RETRYABLE_STATUS_CODES = {429, 503, 504}


class CancelToken:
    """A token that can be used to cancel a ``wait_for_completion`` loop.

    Created automatically by :meth:`AsyncCodingAgentClient.create_session` and
    attached to the returned :class:`Session` as ``session.cancel_token``.
    Can also be constructed standalone and passed to ``wait_for_completion``
    / ``watch_session`` via the ``cancel_token`` parameter.

    Example::

        session = await client.create_session(agent="claude", prompt="hi")
        # later, from another task:
        session.cancel_token.cancel()
    """

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        """Signal cancellation. Any wait loop holding this token will exit."""
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        """Whether :meth:`cancel` has been called."""
        return self._cancelled

    def reset(self) -> None:
        """Clear the cancellation flag so the token can be reused."""
        self._cancelled = False

    def __bool__(self) -> bool:
        """Truthy when cancelled — convenient for ``if token:`` checks."""
        return self._cancelled


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
    max_retries:
        Default number of retry attempts for transient errors (connection
        errors, 429, 503, 504). Defaults to 3. Set to 0 to disable retries.
    retry_base_delay:
        Base delay in seconds for exponential backoff between retries.
        Defaults to 0.5.
    retry_max_delay:
        Maximum delay in seconds between retries. Defaults to 30.0.
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
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
        retry_max_delay: float = DEFAULT_RETRY_MAX_DELAY,
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
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay

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
    ) -> Session:
        """Create a new session (PENDING status).

        ⚠️ The HTTP ``POST /sessions`` endpoint does **not** trigger execution.
        The returned session will have ``status == "pending"`` until a separate
        executor consumes it. See plan v2 ``§约束 1``.

        The returned :class:`Session` has a ``cancel_token`` attribute — a
        :class:`CancelToken` instance that can be passed to
        :meth:`wait_for_completion` / :meth:`watch_session` to cancel the wait.
        """
        payload = {
            "agent": agent,
            "prompt": prompt,
            "workdir": workdir,
            "metadata": metadata or {},
        }
        body = await self._post("/sessions", json=payload)
        session = Session.model_validate(_normalize_session(body))
        # Attach a fresh CancelToken so callers can cancel a subsequent wait.
        # Session uses extra="allow", so arbitrary attributes are permitted.
        session.cancel_token = CancelToken()  # type: ignore[attr-defined]
        return session

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
        lightweight: bool = False,
    ) -> list[Session]:
        """List sessions with optional filters.

        Args:
            agent: Filter by agent type (e.g. ``"claude"``, ``"codex"``).
            status: Filter by status (e.g. ``"running"``, ``"completed"``).
            tag: Filter by tags.  Multiple values are sent as repeated
                query params (``tag=a&tag=b``).
            limit: Maximum number of sessions to return (default 100).
            lightweight: When ``True``, pass ``lightweight=true`` to the
                server so it returns only the essential fields
                (id, status, agent_type, created_at), reducing payload
                size for large session lists.
        """
        # Always use a list of tuples so repeated params (tag, …) are
        # preserved — httpx may collapse list values in a dict into a
        # single param.
        params_list: list[tuple[str, Any]] = [("limit", limit)]
        if agent is not None:
            params_list.append(("agent", agent))
        if status is not None:
            params_list.append(("status", status))
        if tag:
            for t in tag:
                params_list.append(("tag", t))
        if lightweight:
            params_list.append(("lightweight", "true"))
        body = await self._get("/sessions", params=params_list)
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
        type: str | None = None,
    ) -> list[Event]:
        """Return events for a session via REST.

        Args:
            session_id: The session to fetch events for.
            after_seq: Only return events with seq > this value.
            limit: Maximum number of events to return.
            type: Filter by event type (e.g. ``"stdout"``, ``"stderr"``,
                ``"result"``).  Passed as a query param so filtering
                happens server-side.
        """
        params: dict[str, Any] = {"after_seq": after_seq}
        if limit is not None:
            params["limit"] = limit
        if type is not None:
            params["type"] = type
        body = await self._get(f"/sessions/{session_id}/events", params=params)
        return [Event.from_response(item) for item in body]

    async def stream_events(
        self,
        session_id: str,
        *,
        last_event_id: int | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> AsyncIterator[Event]:
        """Stream events for a session via Server-Sent Events.

        Uses ``GET /sessions/{session_id}/events/stream``. The iterator yields
        :class:`Event` instances until the server closes the stream or the
        caller breaks out of the loop.

        The *timeout* defaults to 1 hour for long-running sessions (matching
        the server-side 30-minute polling window with comfortable headroom).

        The stream automatically reconnects on connection drops or transient
        errors (503, 504), resuming from the last received event ID. Set
        *max_retries* to 0 to disable reconnection.
        """
        if timeout is None:
            timeout = DEFAULT_STREAM_TIMEOUT
        if max_retries is None:
            max_retries = self._max_retries

        current_last_event_id = last_event_id
        reconnect_attempts = 0

        while True:
            headers: dict[str, str] = {"Accept": "text/event-stream"}
            if current_last_event_id is not None:
                headers["Last-Event-ID"] = str(current_last_event_id)

            request = self._client.build_request(
                "GET",
                f"/sessions/{session_id}/events/stream",
                headers=headers,
                timeout=timeout,
            )

            response: httpx.Response | None = None
            try:
                try:
                    response = await self._client.send(request, stream=True)
                except httpx.HTTPError as exc:
                    # Connection error — retry
                    if reconnect_attempts >= max_retries:
                        raise NetworkError(
                            f"Failed to open SSE stream: {exc}"
                        ) from exc
                    delay = _compute_retry_delay(
                        reconnect_attempts,
                        self._retry_base_delay,
                        self._retry_max_delay,
                    )
                    await asyncio.sleep(delay)
                    reconnect_attempts += 1
                    continue

                if response.status_code >= 400:
                    if response.status_code in RETRYABLE_STATUS_CODES and reconnect_attempts < max_retries:
                        retry_after = _parse_retry_after(
                            response.headers.get("retry-after")
                        )
                        await response.aclose()
                        response = None
                        if retry_after is not None:
                            await asyncio.sleep(retry_after)
                        else:
                            delay = _compute_retry_delay(
                                reconnect_attempts,
                                self._retry_base_delay,
                                self._retry_max_delay,
                            )
                            await asyncio.sleep(delay)
                        reconnect_attempts += 1
                        continue
                    await _raise_for_status(response)

                # Successful connection — reset reconnect counter
                reconnect_attempts = 0

                async for event in _iter_sse(response):
                    if not event.get("data"):
                        # Heartbeat / comment / empty event — skip.
                        continue
                    parsed = _sse_to_event(session_id, event)
                    # Track last event ID for reconnection
                    if parsed.seq is not None:
                        current_last_event_id = parsed.seq
                    elif "id" in event:
                        try:
                            current_last_event_id = int(event["id"])
                        except (ValueError, TypeError):
                            pass
                    yield parsed

                # Stream ended normally (server closed it) — exit.
                # The caller can reconnect manually by calling stream_events()
                # again with the last event ID.
                return

            except httpx.HTTPError as exc:
                # Error during stream iteration — retry
                if reconnect_attempts >= max_retries:
                    raise NetworkError(
                        f"SSE stream interrupted: {exc}"
                    ) from exc
                delay = _compute_retry_delay(
                    reconnect_attempts,
                    self._retry_base_delay,
                    self._retry_max_delay,
                )
                await asyncio.sleep(delay)
                reconnect_attempts += 1
                continue
            finally:
                if response is not None:
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
        body = await self._post("/recover", params={"timeout_seconds": timeout_seconds})
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
        """Server health check (no auth required).

        Delegates to :meth:`_request` for retry and error handling —
        previously this method duplicated the retry loop from
        :meth:`metrics`; both now share the same code path.
        """
        body = await self._get("/health")
        return HealthStatus.model_validate(body)

    async def metrics(self) -> str:
        """Return Prometheus metrics text (passthrough).

        Uses ``raw=True`` via :meth:`_request` because the ``/metrics``
        endpoint returns plain text, not JSON.
        """
        return await self._request("GET", "/metrics", raw=True)

    # ------------------------------------------------------------------ #
    # High-level: wait for completion
    # ------------------------------------------------------------------ #

    async def wait_for_completion(
        self,
        session_id: str,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        timeout: float = 3600.0,
        cancel_token: CancelToken | None = None,
    ) -> Session:
        """Block until session reaches a terminal state.

        Terminal states: completed, failed, killed, timeout.

        Args:
            session_id: The session to wait for.
            poll_interval: Seconds between status checks (default: 2.0).
            timeout: Maximum seconds to wait (default: 3600.0 = 1 hour).
            cancel_token: Optional :class:`CancelToken`. When cancelled, the
                wait loop raises :class:`CancelledError`.

        Returns:
            The final Session object with terminal status.

        Raises:
            WaitTimeoutError: If timeout exceeded before terminal state.
                Also catchable as builtin ``TimeoutError``.
            CancelledError: If *cancel_token* was cancelled.
            NotFoundError: If session does not exist.
        """
        terminal_states = {"completed", "failed", "killed", "timeout"}
        start_time = time.monotonic()

        while True:
            if cancel_token is not None and cancel_token.is_cancelled:
                raise CancelledError(
                    f"Wait for session {session_id} was cancelled"
                )

            session = await self.get_session(session_id)

            if session.status in terminal_states:
                return session

            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                raise WaitTimeoutError(
                    f"Session {session_id} did not complete within {timeout}s "
                    f"(current status: {session.status})"
                )

            # Use short sleeps so cancellation is responsive.
            remaining = poll_interval
            while remaining > 0:
                if cancel_token is not None and cancel_token.is_cancelled:
                    raise CancelledError(
                        f"Wait for session {session_id} was cancelled"
                    )
                step = min(0.1, remaining)
                await asyncio.sleep(step)
                remaining -= step

    async def watch_session(
        self,
        session_id: str,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        timeout: float = 3600.0,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[Session]:
        """Yield session on each status change until terminal state.

        Terminal states: completed, failed, killed, timeout.

        Args:
            session_id: The session to watch.
            poll_interval: Seconds between status checks (default: 2.0).
            timeout: Maximum seconds to watch (default: 3600.0 = 1 hour).
            cancel_token: Optional :class:`CancelToken`. When cancelled, the
                watch loop raises :class:`CancelledError`.

        Yields:
            Session object each time status changes.

        Raises:
            WaitTimeoutError: If timeout exceeded before terminal state.
                Also catchable as builtin ``TimeoutError``.
            CancelledError: If *cancel_token* was cancelled.
            NotFoundError: If session does not exist.

        Example:
            async for session in client.watch_session(session_id):
                print(f"Status: {session.status}")
        """
        terminal_states = {"completed", "failed", "killed", "timeout"}
        start_time = time.monotonic()
        last_status = None

        while True:
            if cancel_token is not None and cancel_token.is_cancelled:
                raise CancelledError(
                    f"Watch for session {session_id} was cancelled"
                )

            session = await self.get_session(session_id)

            if session.status != last_status:
                yield session
                last_status = session.status

            if session.status in terminal_states:
                break

            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                raise WaitTimeoutError(
                    f"Session {session_id} did not complete within {timeout}s "
                    f"(current status: {session.status})"
                )

            # Use short sleeps so cancellation is responsive.
            remaining = poll_interval
            while remaining > 0:
                if cancel_token is not None and cancel_token.is_cancelled:
                    raise CancelledError(
                        f"Watch for session {session_id} was cancelled"
                    )
                step = min(0.1, remaining)
                await asyncio.sleep(step)
                remaining -= step

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _get(self, path: str, *, params: dict[str, Any] | list[tuple[str, Any]] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, *, json: dict[str, Any] | None = None, params: dict[str, Any] | list[tuple[str, Any]] | None = None) -> Any:
        return await self._request("POST", path, json=json, params=params)

    async def _delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        raw: bool = False,
    ) -> Any:
        """Central HTTP request helper with retry and error handling.

        Parameters
        ----------
        method:
            HTTP method (GET, POST, DELETE, …).
        path:
            URL path relative to the client's ``base_url``.
        json:
            Optional JSON body for the request.
        params:
            Optional query parameters.  A ``list[tuple]`` is accepted so
            callers can send repeated params (e.g. ``tag=a&tag=b``).
        raw:
            When ``True``, return ``response.text`` instead of parsing
            JSON.  Used for endpoints like ``/metrics`` that return plain
            text.
        """
        max_retries = self._max_retries

        for attempt in range(max_retries + 1):
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                )
            except httpx.HTTPError as exc:
                error = NetworkError(f"HTTP request failed: {exc}")
                if attempt >= max_retries:
                    raise error from exc
                # All network errors are retryable
                delay = _compute_retry_delay(
                    attempt, self._retry_base_delay, self._retry_max_delay
                )
                await asyncio.sleep(delay)
                continue

            try:
                if response.status_code >= 400:
                    # Check if this is a retryable error
                    if (
                        attempt < max_retries
                        and response.status_code in RETRYABLE_STATUS_CODES
                    ):
                        retry_after = _parse_retry_after(
                            response.headers.get("retry-after")
                        )
                        await response.aclose()
                        if retry_after is not None:
                            await asyncio.sleep(retry_after)
                        else:
                            delay = _compute_retry_delay(
                                attempt,
                                self._retry_base_delay,
                                self._retry_max_delay,
                            )
                            await asyncio.sleep(delay)
                        continue

                    # Non-retryable error or last attempt — raise
                    await _raise_for_status(response)

                if response.status_code == 204 or not response.content:
                    return None
                if raw:
                    return response.text
                return response.json()
            finally:
                await response.aclose()

        # Should not be reached, but satisfies type checker
        raise NetworkError("Request failed after retries")


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
    if status == 429:
        retry_after = _parse_retry_after(response.headers.get("retry-after"))
        raise RateLimitError(
            detail=detail, response_body=body, retry_after=retry_after
        )
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


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value into seconds.

    Supports both integer seconds and HTTP-date formats per RFC 7231.
    Returns ``None`` if the header is absent or unparseable.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        pass
    try:
        retry_date = parsedate_to_datetime(value)
        if retry_date.tzinfo is None:
            retry_date = retry_date.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = (retry_date - now).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


def _compute_retry_delay(
    attempt: int, base_delay: float, max_delay: float
) -> float:
    """Compute delay with exponential backoff and jitter."""
    delay = min(max_delay, base_delay * (2**attempt))
    # Add jitter: 50-150% of computed delay to avoid thundering herd
    jitter = 0.5 + random.random()
    return delay * jitter


__all__ = ["AsyncCodingAgentClient", "CancelToken"]