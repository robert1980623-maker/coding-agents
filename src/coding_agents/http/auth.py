"""Bearer token authentication dependency for FastAPI routes.

Works alongside ``BearerTokenMiddleware`` (which handles the HTTP-layer
check). This dependency exists so routes that need the verified token
value can declare it via ``Depends(verify_token)``.

Dev-mode bypass: if no token file exists, authentication is disabled
and the dependency passes through with an empty token string.
"""

from __future__ import annotations

import secrets
from typing import Optional

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from coding_agents.auth import load_token

security = HTTPBearer(auto_error=False)


def _get_token_path(request: Request) -> Optional[str]:
    """Extract the token path from request.app.state, if set.

    The server sets ``app.state.token_path`` when a custom token path is
    configured. If not set, returns None to use the default path.
    """
    return getattr(request.app.state, "token_path", None)


async def verify_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> str:
    """Verify Bearer token against the stored token file.

    Dev-mode bypass: when the token file does not exist (``load_token()``
    returns ``None``), authentication is disabled and the dependency
    passes through unconditionally. The middleware performs the same
    check at the HTTP layer; this keeps the route-level dependency
    consistent with it.

    Args:
        request: The FastAPI request object (used to access app.state.token_path).
        credentials: HTTP Bearer credentials from the request.

    Returns:
        The validated token string, or ``""`` in dev mode (no token file).

    Raises:
        HTTPException: If credentials are missing or invalid (and a token
            file exists).
    """
    token_path = _get_token_path(request)

    # Load the stored token once. Re-reading inside validate_token()
    # would be a TOCTOU race and wastes an I/O per request.
    #
    # load_token returns:
    #   None  → file does not exist (dev mode: auth disabled).
    #   ""    → file exists but is empty / unreadable (auth BROKEN:
    #           reject rather than silently bypass).
    #   str   → valid token to compare against.
    stored = load_token(token_path)

    # Dev-mode bypass: no token file → auth disabled.
    # Return "" rather than the raw credentials — the caller must not
    # treat the returned value as verified when no token file exists.
    if stored is None:
        return ""

    if stored == "":
        # Token file exists but is empty/unreadable. This is NOT dev mode
        # — it means auth is broken. Refuse to validate rather than
        # silently passing all requests.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Auth token file is empty or unreadable. "
                "Regenerate it with the coding-agents CLI."
            ),
        )

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    if not secrets.compare_digest(token, stored):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token
