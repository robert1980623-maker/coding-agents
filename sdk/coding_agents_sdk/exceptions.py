"""SDK-specific exceptions.

The SDK is a pure HTTP wrapper: it does NOT interpret or trigger execution
semantics. Errors that bubble up are HTTP-layer errors only.
"""

from __future__ import annotations


class CodingAgentsSDKError(Exception):
    """Base exception for all SDK errors."""


class APIError(CodingAgentsSDKError):
    """An HTTP error response from the server.

    Attributes:
        status_code: HTTP status code returned by the server.
        detail: The error detail returned in the response body (if any).
        response_body: The raw response body (parsed as JSON when possible).
    """

    def __init__(
        self,
        status_code: int,
        detail: str | None = None,
        response_body: object | None = None,
    ) -> None:
        self.status_code = status_code
        self.detail = detail
        self.response_body = response_body

        message = f"HTTP {status_code}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class AuthenticationError(APIError):
    """Raised when the server returns 401 (missing/invalid token)."""

    def __init__(
        self,
        detail: str | None = None,
        response_body: object | None = None,
    ) -> None:
        super().__init__(401, detail=detail, response_body=response_body)


class NotFoundError(APIError):
    """Raised when the server returns 404 (resource does not exist)."""

    def __init__(
        self,
        detail: str | None = None,
        response_body: object | None = None,
    ) -> None:
        super().__init__(404, detail=detail, response_body=response_body)


class ServerError(APIError):
    """Raised when the server returns 5xx."""

    def __init__(
        self,
        status_code: int,
        detail: str | None = None,
        response_body: object | None = None,
    ) -> None:
        super().__init__(status_code, detail=detail, response_body=response_body)


class RateLimitError(APIError):
    """Raised when the server returns 429 (rate limit exceeded).

    Attributes:
        retry_after: Parsed ``Retry-After`` header value in seconds, if present.
    """

    def __init__(
        self,
        detail: str | None = None,
        response_body: object | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(429, detail=detail, response_body=response_body)
        self.retry_after = retry_after


class ConnectionError_(CodingAgentsSDKError):
    """Raised when the SDK cannot reach the server (network/timeout).

    The trailing underscore avoids clashing with the builtin ``ConnectionError``.

    .. deprecated::
        Use :class:`NetworkError` instead. ``ConnectionError_`` is kept as a
        backwards-compatible alias.
    """


# Preferred name — more descriptive and avoids the builtin clash without the
# trailing underscore.  ``ConnectionError_`` is kept as a deprecated alias.
NetworkError = ConnectionError_


class WaitTimeoutError(CodingAgentsSDKError, TimeoutError):
    """Raised when ``wait_for_completion`` / ``watch_session`` exceed their timeout.

    Dual-inherits from :class:`TimeoutError` so existing callers that catch the
    builtin ``TimeoutError`` continue to work unchanged.
    """


class CancelledError(CodingAgentsSDKError):
    """Raised when a wait/watch loop is cancelled via a :class:`CancelToken`."""


__all__ = [
    "APIError",
    "AuthenticationError",
    "CancelledError",
    "CodingAgentsSDKError",
    "ConnectionError_",
    "NetworkError",
    "NotFoundError",
    "RateLimitError",
    "ServerError",
    "WaitTimeoutError",
]