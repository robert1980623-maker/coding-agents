"""Bearer token authentication middleware for the HTTP API.

This middleware enforces Bearer token auth on every route except the
public ones (``/health``). If the token file does not exist, auth is
skipped (development mode) and a warning is logged — this keeps the
server usable before the user has run the CLI for the first time.
"""

from __future__ import annotations

from typing import Optional

import logging
import secrets

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from coding_agents.auth import load_token

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
# True once a non-empty token has been successfully loaded. After this, a
# missing token file is treated as broken auth (500), NOT dev mode — this
# prevents a deleted token file from silently re-enabling dev-mode bypass.
_token_ever_loaded: bool = False


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
        global _dev_mode_warned, _token_ever_loaded  # noqa: PLW0603 — module-level flags

        # Public paths bypass auth entirely.
        # Strip trailing slash so /health/ (common from load balancers)
        # also matches — without this, health checks can fail with 401.
        if request.url.path.rstrip("/") in PUBLIC_PATHS:
            return await call_next(request)

        # Load the stored token from disk on every request.
        #
        # load_token returns:
        #   None  → file does not exist.
        #   ""    → file exists but is empty / unreadable (auth BROKEN:
        #           reject rather than silently disable auth — this is
        #           the security-critical distinction).
        #   str   → valid token to compare against.
        #
        # We re-read on every request so the server picks up token
        # regeneration (e.g. CLI re-creating an empty/corrupt file) and
        # initial token creation (server started before CLI ran once).
        # The TOCTOU window between this read and verify_token's use of
        # request.state is negligible (same request lifecycle).
        #
        # Dev-mode vs broken-file distinction for None (file missing):
        #   - If we have NEVER loaded a valid token, the file may simply
        #     not exist yet (server started before CLI ran once). Enter
        #     dev mode with a one-shot warning.
        #   - If we HAVE loaded a valid token before, the file was deleted
        #     after the server was running. This is NOT dev mode — it's
        #     broken auth (someone may have deleted the file to bypass
        #     auth). Reject with 500 rather than silently re-enabling
        #     dev-mode bypass.
        stored = load_token(self.token_path)
        if stored is None:
            if _token_ever_loaded:
                # File was deleted after we previously loaded a valid token.
                # This is NOT dev mode — it's broken auth. Reject rather
                # than silently re-enabling dev-mode bypass.
                logger.error(
                    "auth_broken_token_file_missing: Token file was deleted "
                    "after auth was active — refusing to silently disable "
                    "auth. Run the coding-agents CLI to regenerate the token."
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "detail": (
                            "Auth token file is missing. "
                            "Regenerate it with the coding-agents CLI."
                        ),
                    },
                )
            if not _dev_mode_warned:
                logger.warning(
                    "auth_disabled_no_token_file: No token file found — "
                    "authentication is disabled (development mode). "
                    "Run the coding-agents CLI once to generate a token, "
                    "or set CODING_AGENTS_TOKEN_PATH."
                )
                _dev_mode_warned = True
            # Signal to verify_token that we're in dev mode (auth disabled).
            # Empty string means "no auth performed" — verify_token will
            # return "" without re-checking the file.
            request.state.auth_token = ""
            return await call_next(request)
        if stored == "":
            # Token file exists but is empty or unreadable. This is NOT
            # dev mode — it means auth is broken (e.g. crash during write,
            # disk full, accidental truncation). Rejecting here prevents
            # silently serving all requests without auth.
            logger.error(
                "auth_broken_token_file_empty: Token file exists but is "
                "empty or unreadable — refusing to disable auth silently. "
                "Run the coding-agents CLI to regenerate the token."
            )
            return JSONResponse(
                status_code=500,
                content={
                    "detail": (
                        "Auth token file is empty or unreadable. "
                        "Regenerate it with the coding-agents CLI."
                    ),
                },
            )

        # Extract Bearer token from Authorization header.
        # RFC 7235 §2.1: the auth-scheme is case-insensitive and followed by
        # one or more whitespace characters (SP or HTAB) before the credentials.
        # We split on whitespace to handle "Bearer token", "Bearer  token",
        # "Bearer\ttoken", etc.
        auth_header = request.headers.get("Authorization", "")
        parts = auth_header.split(None, 1)  # Split on any whitespace, max 1 split
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing authorization header"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        provided = parts[1].strip()  # Strip any trailing whitespace
        # Record that we've loaded a valid token at least once. From now
        # on, a missing token file is treated as broken auth (500), not
        # dev mode — prevents silent auth bypass via file deletion.
        _token_ever_loaded = True
        # Constant-time comparison against the token we already loaded —
        # do NOT call validate_token() here, as that would re-read the
        # file from disk (TOCTOU + wasted I/O).
        if not secrets.compare_digest(provided, stored):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Store the validated token in request.state so that verify_token
        # can retrieve it without re-reading the file from disk. This
        # eliminates the TOCTOU race where the file could change between
        # the middleware read and the dependency read, causing false 401s.
        request.state.auth_token = provided

        return await call_next(request)


def reset_dev_mode_warning() -> None:
    """Reset the one-shot dev-mode warning flag and token-loaded tracker (for tests)."""
    global _dev_mode_warned, _token_ever_loaded  # noqa: PLW0603
    _dev_mode_warned = False
    _token_ever_loaded = False
