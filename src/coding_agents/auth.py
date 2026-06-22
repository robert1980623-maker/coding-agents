"""Auth token management for the coding-agents CLI.

Phase 1 implements local token generation, storage, and loading.
The HTTP server (Phase 2) will consume these tokens for Bearer auth.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_TOKEN_PATH = "~/.coding-agents-token"
TOKEN_LENGTH_BYTES = 32  # 256-bit token


def get_token_path(explicit_path: Optional[str] = None) -> Path:
    """Resolve the token file path.

    Resolution order:
    1. ``explicit_path`` argument (e.g. from ``--auth-token-file``).
    2. ``CODING_AGENTS_TOKEN_PATH`` environment variable.
    3. ``DEFAULT_TOKEN_PATH`` (``~/.coding-agents-token``).

    Args:
        explicit_path: User-provided path. Takes highest precedence.

    Returns:
        Resolved Path object.
    """
    raw = explicit_path or os.environ.get("CODING_AGENTS_TOKEN_PATH") or DEFAULT_TOKEN_PATH
    return Path(raw).expanduser().resolve()


def generate_token() -> str:
    """Generate a cryptographically-secure random token.

    Returns:
        A hex-encoded 256-bit token string.
    """
    return secrets.token_hex(TOKEN_LENGTH_BYTES)


def ensure_token(token_path: Optional[str] = None) -> str:
    """Load or create an auth token.

    If the token file exists, read and return its content.
    If it does not exist, generate a new token, write it to the file
    with mode 0600, and return it.

    Args:
        token_path: Optional explicit path to the token file.

    Returns:
        The token string.
    """
    path = get_token_path(token_path)

    if path.exists():
        token = load_token(str(path))
        if token:
            return token
        # File exists but is empty/invalid — regenerate.
        # Remove it first so the O_EXCL create below succeeds.
        logger.warning("token_file_empty_regenerating", path=str(path))
        try:
            path.unlink()
        except OSError:
            pass  # Race: another process may have removed it

    # Generate new token
    token = generate_token()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write with restrictive permissions (owner read/write only).
    # O_EXCL makes the create atomic: if another process created the file
    # between our exists() check and this open(), we get FileExistsError
    # instead of silently overwriting their token (which would break their
    # auth on next request).
    fd = -1  # Sentinel: -1 means "no fd to close" (os.open didn't run/succeed).
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        fd = -1  # fdopen consumed the fd; nothing to close on later error.
    except FileExistsError:
        # Another process won the race — load their token so both
        # processes converge on the same value.
        logger.info("token_generation_race_lost", path=str(path))
        existing = load_token(str(path))
        if existing:
            return existing
        # File exists but is empty/unreadable — regenerate ours.
        # This is rare; a second race here is acceptable.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        fd = -1  # fdopen consumed the fd.
    except BaseException:
        # If os.fdopen fails the fd is NOT consumed — close it to avoid
        # leaking. (Once fdopen succeeds, the with-block owns the fd and
        # we reset fd to -1 above.)
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        raise

    logger.info("token_generated", path=str(path))
    return token


def load_token(token_path: Optional[str] = None) -> Optional[str]:
    """Load an auth token from file.

    Args:
        token_path: Optional explicit path to the token file.

    Returns:
        The token string (stripped), or:
        - ``None`` if the file does not exist (caller may enable dev mode).
        - ``""`` if the file exists but is empty or unreadable (auth is
          broken — callers MUST reject requests rather than fall through
          to dev-mode bypass, which would silently disable auth).
    """
    path = get_token_path(token_path)
    if not path.exists():
        return None
    try:
        content = path.read_text().strip()
        if not content:
            return ""
        return content
    except OSError as e:
        logger.warning("token_load_failed", path=str(path), error=str(e))
        return ""


def validate_token(provided: str, token_path: Optional[str] = None) -> bool:
    """Validate a provided token against the stored token.

    Uses constant-time comparison to prevent timing attacks.

    Args:
        provided: The token to validate.
        token_path: Optional explicit path to the token file.

    Returns:
        True if the token matches, False otherwise.
    """
    path = get_token_path(token_path)
    stored = load_token(str(path))
    if not stored:
        # None → file missing; "" → file empty/corrupt.
        # Either way, the provided token cannot be validated.
        return False
    return secrets.compare_digest(provided, stored)
