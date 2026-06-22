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
        token = load_token(token_path)
        if token is not None:
            return token
        # File exists but is empty/invalid — regenerate
        logger.warning("token_file_empty_regenerating", path=str(path))

    # Generate new token
    token = generate_token()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write with restrictive permissions (owner read/write only)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
    except Exception:
        # If fdopen fails, fd is already closed
        raise

    logger.info("token_generated", path=str(path))
    return token


def load_token(token_path: Optional[str] = None) -> Optional[str]:
    """Load an auth token from file.

    Args:
        token_path: Optional explicit path to the token file.

    Returns:
        The token string, or None if the file does not exist or is empty.
    """
    path = get_token_path(token_path)
    if not path.exists():
        return None
    try:
        content = path.read_text().strip()
        if not content:
            return None
        return content
    except OSError as e:
        logger.warning("token_load_failed", path=str(path), error=str(e))
        return None


def validate_token(provided: str, token_path: Optional[str] = None) -> bool:
    """Validate a provided token against the stored token.

    Uses constant-time comparison to prevent timing attacks.

    Args:
        provided: The token to validate.
        token_path: Optional explicit path to the token file.

    Returns:
        True if the token matches, False otherwise.
    """
    stored = load_token(token_path)
    if stored is None:
        return False
    return secrets.compare_digest(provided, stored)
