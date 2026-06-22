"""Bearer token authentication dependency for FastAPI routes.

Works alongside ``BearerTokenMiddleware`` (which handles the HTTP-layer
check). This dependency exists so routes that need the verified token
value can declare it via ``Depends(verify_token)``.

The middleware performs the actual token validation (file read + constant-
time comparison) and stores the result in ``request.state.auth_token``.
This dependency retrieves that pre-verified value — it does NOT re-read
the token file, which would be a TOCTOU race (the file could change
between the middleware read and this read, causing false 401s).

Dev-mode bypass: if no token file exists, the middleware sets
``auth_token=""`` and this dependency returns ``""`` without error.
"""

from __future__ import annotations

from fastapi import Request


async def verify_token(request: Request) -> str:
    """Return the token already validated by BearerTokenMiddleware.

    The middleware performs all auth logic (file read, dev-mode bypass,
    empty-file rejection, Bearer parsing, constant-time comparison) and
    stores the result in ``request.state.auth_token``:

    * ``str`` (non-empty) → validated token.
    * ``""`` → dev mode (no token file) or the token itself is empty.
      Either way, the middleware already decided this is acceptable.

    If ``auth_token`` is not set, the middleware didn't run on this
    request (e.g. route mounted without the middleware). In that case,
    we cannot verify the token — return ``""`` rather than raising, so
    callers get a consistent "unverified" sentinel.

    Returns:
        The validated token string, or ``""`` in dev mode / unverified.
    """
    # The middleware sets this after successful validation.
    # "" means dev mode (no token file) — auth is disabled.
    return getattr(request.state, "auth_token", "")
