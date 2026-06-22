"""Bearer token authentication middleware for the HTTP API.

This middleware enforces Bearer token auth on every route except the
public ones (``/health``). If the token file does not exist, auth is
skipped (development mode) and a warning is logged — this keeps the
server usable before the user has run the CLI for the first time.
"""

from __future__ import annotations

from typing import Optional

import logging

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from coding_agents.auth import load_token, validate_token

# Use stdlib logger so logs integrate with Python's logging system and can be
# captured by standard logging handlers (e.g. pytest's caplog). The default
# structlog logger writes to stdout via PrintLogger, which bypasses stdlib.
logger = logging.getLogger(__name__)

# Paths that are served without authentication.
# /health is the canonical liveness probe — load balancers and
# orchestrators hit it frequently; requiring a token would break them.
PUBLIC_PATHS: frozenset[str] = frozenset({"/health"})

# Warn at most once per process about the missing-token dev-mode bypass,
# so we don't spam the log on every request.
_dev_mode_warned: bool = False


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Global Bearer-token auth middleware.

    Behavior:
    * Public paths (``/health``) pass through without auth.
    * If the token file does not exist, all requests pass through and a
      warning is logged once (development mode).
    * Otherwise, the ``Authorization: Bearer <token>`` header is validated
      against the stored token using constant-time comparison. Missing or
      invalid tokens get a 401 response.

    Args:
        app: The ASGI application to wrap.
        token_path: Optional path to the token file. If None, uses the
            default path (``~/.coding-agents-token``).
    """

    def __init__(self, app, token_path: Optional[str] = None) -> None:
        """Initialize the middleware with an optional custom token path."""
        super().__init__(app)
        self.token_path = token_path

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        global _dev_mode_warned  # noqa: PLW0603 — module-level flag

        # Public paths bypass auth entirely.
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        # If the token file does not exist, skip auth (dev mode).
        # Warn once per process so the operator notices.
        stored = load_token(self.token_path)
        if stored is None:
            if not _dev_mode_warned:
                logger.warning(
                    "auth_disabled_no_token_file: No token file found — "
                    "authentication is disabled (development mode). "
                    "Run the coding-agents CLI once to generate a token, "
                    "or set CODING_AGENTS_TOKEN_PATH."
                )
                _dev_mode_warned = True
            return await call_next(request)

        # Extract Bearer token from Authorization header.
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing authorization header"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        provided = auth_header[len("Bearer "):]
        if not validate_token(provided, self.token_path):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)


def reset_dev_mode_warning() -> None:
    """Reset the one-shot dev-mode warning flag (for tests)."""
    global _dev_mode_warned  # noqa: PLW0603
    _dev_mode_warned = False
