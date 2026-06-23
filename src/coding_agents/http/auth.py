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

Safety net: if ``auth_token`` was never set (e.g. the middleware didn't
run because a route was mounted on a different app, or a future refactor
accidentally skips it), this dependency raises 401 rather than silently
returning ``""`` and allowing unauthenticated access.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status


async def verify_token(request: Request) -> str:
    """Return the token already validated by BearerTokenMiddleware.

    The middleware performs all auth logic (file read, dev-mode bypass,
    empty-file rejection, Bearer parsing, constant-time comparison) and
    stores the result in ``request.state.auth_token``:

    * ``str`` (non-empty) → validated token.
    * ``""`` → dev mode (no token file) — auth is disabled.
    * *not set* → middleware did not run → raise 401.

    Returns:
        The validated token string, or ``""`` in dev mode.

    Raises:
        HTTPException: 401 if the middleware did not set ``auth_token``
            (i.e. it didn't run on this request).
    """
    # Use None as the sentinel so we can distinguish "middleware didn't
    # run" (auth bypass — must reject) from "dev mode" (auth disabled
    # intentionally — token is "").
    token = getattr(request.state, "auth_token", None)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token
