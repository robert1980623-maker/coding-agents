"""Test that SDK client reads from default token file.

This test requires the main coding_agents package to be available.
Skip if it's not installed (SDK can work standalone).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

# Try to import coding_agents - skip tests if not available
try:
    from coding_agents.auth import ensure_token
    CODING_AGENTS_AVAILABLE = True
except ImportError:
    CODING_AGENTS_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not CODING_AGENTS_AVAILABLE,
    reason="coding_agents package not available (SDK standalone mode)"
)


class TestSDKClientTokenFileFallback:
    """SDK client should fall back to reading from ~/.coding-agents-token."""

    async def test_client_reads_from_default_token_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """When no token is provided, client should read from default token file."""
        # Create a token file
        token_file = tmp_path / "token"
        token = ensure_token(str(token_file))

        # Point the SDK client's default token path to our test file
        monkeypatch.setattr("coding_agents_sdk.client.DEFAULT_TOKEN_PATH", str(token_file))

        # Clear any env var that might interfere
        monkeypatch.delenv("CODING_AGENTS_TOKEN", raising=False)
        monkeypatch.delenv("CODING_AGENTS_TOKEN_PATH", raising=False)

        # Import after monkeypatching to ensure it picks up the changes
        from coding_agents_sdk.client import AsyncCodingAgentClient

        # Create client without explicit token
        async with AsyncCodingAgentClient() as client:
            # The client should have loaded the token from the file
            assert client._token == token, (
                f"Client should have loaded token from file. "
                f"Expected {token!r}, got {client._token!r}"
            )
            # Check that the Authorization header was set
            auth_header = client._client.headers.get("Authorization")
            assert auth_header == f"Bearer {token}", (
                f"Authorization header should be set. Got {auth_header!r}"
            )

    async def test_client_explicit_token_overrides_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """Explicit token parameter should override the file."""
        # Create a token file
        token_file = tmp_path / "token"
        file_token = ensure_token(str(token_file))
        monkeypatch.setattr("coding_agents_sdk.client.DEFAULT_TOKEN_PATH", str(token_file))
        monkeypatch.delenv("CODING_AGENTS_TOKEN", raising=False)
        monkeypatch.delenv("CODING_AGENTS_TOKEN_PATH", raising=False)

        from coding_agents_sdk.client import AsyncCodingAgentClient

        # Create client with explicit token
        explicit_token = "explicit-token-12345"
        async with AsyncCodingAgentClient(token=explicit_token) as client:
            assert client._token == explicit_token
            auth_header = client._client.headers.get("Authorization")
            assert auth_header == f"Bearer {explicit_token}"

    async def test_client_env_var_overrides_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """CODING_AGENTS_TOKEN env var should override the file."""
        # Create a token file
        token_file = tmp_path / "token"
        file_token = ensure_token(str(token_file))
        monkeypatch.setattr("coding_agents_sdk.client.DEFAULT_TOKEN_PATH", str(token_file))
        monkeypatch.delenv("CODING_AGENTS_TOKEN_PATH", raising=False)

        env_token = "env-var-token-67890"
        monkeypatch.setenv("CODING_AGENTS_TOKEN", env_token)

        from coding_agents_sdk.client import AsyncCodingAgentClient

        async with AsyncCodingAgentClient() as client:
            assert client._token == env_token
            auth_header = client._client.headers.get("Authorization")
            assert auth_header == f"Bearer {env_token}"

    async def test_client_no_token_file_no_env_var(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """When no token file exists and no env var, client should have no token."""
        # Point to a non-existent file
        nonexistent = tmp_path / "nonexistent-token"
        monkeypatch.setattr("coding_agents_sdk.client.DEFAULT_TOKEN_PATH", str(nonexistent))
        monkeypatch.delenv("CODING_AGENTS_TOKEN", raising=False)
        monkeypatch.delenv("CODING_AGENTS_TOKEN_PATH", raising=False)

        from coding_agents_sdk.client import AsyncCodingAgentClient

        async with AsyncCodingAgentClient() as client:
            assert client._token is None
            auth_header = client._client.headers.get("Authorization")
            assert auth_header is None
