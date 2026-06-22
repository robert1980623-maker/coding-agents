"""Tests for HTTP authentication middleware.

These tests focus specifically on the BearerTokenMiddleware behavior:
- Public paths (/health) bypass auth
- All other paths require a valid Bearer token
- Missing-token dev-mode bypass (with warning)
- Invalid / missing Authorization headers return 401
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from coding_agents.auth import ensure_token, load_token
from coding_agents.http.middleware import BearerTokenMiddleware, reset_dev_mode_warning
from coding_agents.http.server import create_app
from coding_agents.storage.sqlite import SQLiteStorage


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[SQLiteStorage]:
    """Create a test storage instance."""
    db_path = tmp_path / "test.db"
    store = SQLiteStorage(str(db_path))
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
async def app(storage: SQLiteStorage):
    """Create a test FastAPI app with middleware."""
    test_app = create_app(db_path=str(storage._db_path))

    # Override the storage dependency
    async def get_test_storage() -> SQLiteStorage:
        return storage

    test_app.dependency_overrides[SQLiteStorage] = get_test_storage
    return test_app


@pytest.fixture(autouse=True)
def _reset_dev_mode_warning():
    """Reset the one-shot dev-mode warning before each test."""
    reset_dev_mode_warning()
    yield
    reset_dev_mode_warning()


def _client(app, headers: dict[str, str] | None = None) -> AsyncClient:
    """Create a test client with optional auth headers."""
    transport = ASGITransport(app=app)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=headers or {},
    )


class TestHealthBypass:
    """/health must bypass auth entirely."""

    async def test_health_no_auth_required(self, app):
        """GET /health should return 200 even without Authorization."""
        async with _client(app) as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    async def test_health_with_invalid_auth_still_works(self, app):
        """/health ignores even invalid tokens — it's fully public."""
        async with _client(app, {"Authorization": "Bearer garbage"}) as client:
            response = await client.get("/health")
        assert response.status_code == 200

    async def test_health_trailing_slash_bypasses_auth(self, app):
        """GET /health/ (trailing slash) must also bypass auth.

        Load balancers and health checkers frequently send trailing slashes;
        without normalization, these would fail with 401.
        """
        async with _client(app) as client:
            response = await client.get("/health/")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"


class TestNoTOCTOU:
    """verify_token must NOT re-read the token file after middleware validates it.

    If the file changes between middleware read and verify_token read,
    a request that passed middleware would get a false 401 at the route
    level. The middleware stores the validated token in request.state.auth_token;
    verify_token must read from there instead of re-reading the file.
    """

    async def test_verify_token_uses_middleware_result(
        self, app, monkeypatch, tmp_path
    ):
        """After middleware validates, verify_token should not re-read the file.

        We test this by changing the file content AFTER the middleware read
        but BEFORE the route handler runs. If verify_token re-reads the file,
        it would get a different token and return 401.
        """
        token_path = tmp_path / "token"
        original_token = ensure_token(str(token_path))
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        # Track load_token calls in the route handler
        load_count = {"count": 0}
        original_load_token = __import__(
            "coding_agents.http.auth", fromlist=["load_token"]
        ).load_token if False else None  # noqa

        async with _client(app, {"Authorization": f"Bearer {original_token}"}) as client:
            # Warmup: the middleware reads the token file on this first
            # non-public request and caches the result for the lifetime
            # of the middleware. This is the load that would have happened
            # in a real TOCTOU race (middleware read, then file change,
            # then handler read).
            warmup = await client.get("/sessions")
            assert warmup.status_code == 200, (
                f"warmup request failed with {warmup.status_code}: {warmup.text}"
            )

            # Now change the file. The middleware's cache still holds the
            # original token, so the next request's Bearer check will use
            # the cached value (not the new file content) and succeed.
            # verify_token reads from request.state.auth_token (set by the
            # middleware from the cache), not from the file, so the file
            # change is invisible to it.
            token_path.write_text("different-token\n")
            response = await client.get("/sessions")

        # Should still succeed because middleware already validated and
        # stored the result in request.state.
        assert response.status_code == 200


class TestTokenRequired:
    """Non-public endpoints must require a valid Bearer token."""

    async def test_missing_auth_returns_401(self, app):
        """Request without Authorization header → 401."""
        async with _client(app) as client:
            response = await client.get("/sessions")
        assert response.status_code == 401
        assert "Missing authorization" in response.json()["detail"]

    async def test_wrong_scheme_returns_401(self, app):
        """Non-Bearer auth schemes (e.g. Basic) → 401."""
        async with _client(app, {"Authorization": "Basic dXNlcjpwYXNz"}) as client:
            response = await client.get("/sessions")
        assert response.status_code == 401

    async def test_bearer_without_token_returns_401(self, app, monkeypatch, tmp_path):
        """``Authorization: Bearer `` with empty token → 401."""
        # Ensure a token file exists so dev-mode bypass doesn't kick in
        token_path = tmp_path / "token"
        ensure_token(str(token_path))
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        async with _client(app, {"Authorization": "Bearer "}) as client:
            response = await client.get("/sessions")
        assert response.status_code == 401

    async def test_invalid_token_returns_401(self, app, monkeypatch, tmp_path):
        """Wrong token → 401."""
        token_path = tmp_path / "token"
        ensure_token(str(token_path))
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        async with _client(app, {"Authorization": "Bearer wrong-token"}) as client:
            response = await client.get("/sessions")
        assert response.status_code == 401
        assert "Invalid token" in response.json()["detail"]

    async def test_valid_token_returns_200(self, app, monkeypatch, tmp_path):
        """Correct token → 200."""
        token_path = tmp_path / "token"
        token = ensure_token(str(token_path))
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        async with _client(app, {"Authorization": f"Bearer {token}"}) as client:
            response = await client.get("/sessions")
        assert response.status_code == 200

    @pytest.mark.parametrize("scheme", ["bearer", "BEARER", "BeArEr", "Bearer"])
    async def test_bearer_scheme_is_case_insensitive(
        self, app, monkeypatch, tmp_path, scheme: str
    ):
        """RFC 7235 §2.1: auth-scheme is case-insensitive."""
        token_path = tmp_path / "token"
        token = ensure_token(str(token_path))
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        async with _client(app, {"Authorization": f"{scheme} {token}"}) as client:
            response = await client.get("/sessions")
        assert response.status_code == 200, (
            f"{scheme!r} scheme should be accepted (RFC 7235 §2.1)"
        )

    @pytest.mark.parametrize(
        "header",
        [
            "Bearer {token}",      # Single space (standard)
            "Bearer  {token}",     # Double space
            "Bearer\t{token}",     # Tab separator
            "Bearer \t {token}",   # Mixed whitespace
            "bearer  {token}",     # Lowercase + double space
        ],
    )
    async def test_bearer_whitespace_handling(
        self, app, monkeypatch, tmp_path, header: str
    ):
        """RFC 7235 §2.1: auth-scheme followed by 1+ whitespace (SP/HTAB).

        The middleware must handle flexible whitespace between the scheme
        and credentials, as clients may send "Bearer  token" (double space)
        or "Bearer\\ttoken" (tab) instead of the standard single space.
        """
        token_path = tmp_path / "token"
        token = ensure_token(str(token_path))
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        formatted_header = header.format(token=token)
        async with _client(app, {"Authorization": formatted_header}) as client:
            response = await client.get("/sessions")
        assert response.status_code == 200, (
            f"Header {formatted_header!r} should be accepted (RFC 7235 §2.1)"
        )


class TestMetricsAuth:
    """/metrics is NOT public — it requires auth like any other endpoint.

    Prometheus scrapers should be configured with the Bearer token.
    """

    async def test_metrics_requires_auth(self, app, monkeypatch, tmp_path):
        """GET /metrics without token → 401."""
        token_path = tmp_path / "token"
        ensure_token(str(token_path))
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        async with _client(app) as client:
            response = await client.get("/metrics")
        assert response.status_code == 401

    async def test_metrics_with_valid_token(self, app, monkeypatch, tmp_path):
        """GET /metrics with valid token → 200."""
        token_path = tmp_path / "token"
        token = ensure_token(str(token_path))
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        async with _client(app, {"Authorization": f"Bearer {token}"}) as client:
            response = await client.get("/metrics")
        assert response.status_code == 200


class TestDevModeBypass:
    """If the token file does not exist, auth is skipped (dev mode)."""

    async def test_no_token_file_skips_auth(self, app, monkeypatch, tmp_path):
        """When no token file exists, requests pass through without auth."""
        # Point at a path that definitely doesn't exist
        nonexistent = tmp_path / "does-not-exist-token"
        assert not nonexistent.exists()
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(nonexistent))

        # Sanity: load_token returns None for missing file
        assert load_token(str(nonexistent)) is None

        async with _client(app) as client:
            response = await client.get("/sessions")

        # Auth bypassed → 200 (empty list)
        assert response.status_code == 200
        assert response.json() == []

    async def test_dev_mode_warning_logged_once(self, app, monkeypatch, tmp_path, caplog):
        """The dev-mode warning should fire at most once per process."""
        nonexistent = tmp_path / "no-token-here"
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(nonexistent))

        with caplog.at_level(logging.WARNING, logger="coding_agents.http.middleware"):
            async with _client(app) as client:
                await client.get("/sessions")
                await client.get("/sessions")
                await client.get("/sessions")

        # The warning should appear exactly once across 3 requests
        warn_count = sum(
            1 for r in caplog.records if "auth_disabled_no_token_file" in r.getMessage()
        )
        assert warn_count == 1, (
            f"expected 1 warning, got {warn_count}.\n"
            f"Records: {[r.getMessage() for r in caplog.records]}"
        )

    async def test_health_not_affected_by_dev_mode(self, app, monkeypatch, tmp_path):
        """/health works regardless of token file existence."""
        nonexistent = tmp_path / "no-token"
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(nonexistent))

        async with _client(app) as client:
            response = await client.get("/health")
        assert response.status_code == 200


class TestCorruptTokenFile:
    """A token file that exists but is empty/unreadable must NOT fall through
    to dev-mode bypass — that would silently disable auth (security bug).

    Instead, requests must be rejected with 500.
    """

    async def test_empty_token_file_rejects_requests(self, app, monkeypatch, tmp_path):
        """Token file exists but is empty → 500, NOT 200."""
        token_path = tmp_path / "token"
        token_path.write_text("")  # exists but empty
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        # Sanity: load_token returns "" for empty file (not None)
        assert load_token(str(token_path)) == ""

        async with _client(app) as client:
            response = await client.get("/sessions")
        # Must NOT be 200 (that would mean auth was silently bypassed).
        assert response.status_code == 500
        assert "empty" in response.json()["detail"].lower() or "unreadable" in response.json()["detail"].lower()

    async def test_empty_token_file_rejects_even_with_valid_looking_header(
        self, app, monkeypatch, tmp_path
    ):
        """Even with an Authorization header, empty token file → 500.

        The token can't be validated because there's nothing stored to
        compare against — rejecting with 500 is the safe default.
        """
        token_path = tmp_path / "token"
        token_path.write_text("")
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        async with _client(app, {"Authorization": "Bearer some-token"}) as client:
            response = await client.get("/sessions")
        assert response.status_code == 500

    async def test_empty_token_file_still_allows_health(self, app, monkeypatch, tmp_path):
        """/health is fully public — not affected by corrupt token file."""
        token_path = tmp_path / "token"
        token_path.write_text("")
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        async with _client(app) as client:
            response = await client.get("/health")
        assert response.status_code == 200

    async def test_empty_token_file_logs_error(self, app, monkeypatch, tmp_path, caplog):
        """An error should be logged when the token file is broken."""
        token_path = tmp_path / "token"
        token_path.write_text("")
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        with caplog.at_level(logging.ERROR, logger="coding_agents.http.middleware"):
            async with _client(app) as client:
                await client.get("/sessions")

        assert any(
            "auth_broken_token_file_empty" in r.getMessage()
            for r in caplog.records
        )


class TestAllEndpointsProtected:
    """Spot-check that every route category goes through auth."""

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/sessions"),
            ("POST", "/sessions"),
            ("GET", "/sessions/any-id"),
            ("GET", "/sessions/any-id/events"),
            ("GET", "/sessions/any-id/events/stream"),
            ("GET", "/sessions/any-id/tags"),
            ("POST", "/sessions/any-id/tags"),
            ("DELETE", "/sessions/any-id/tags/foo"),
            ("POST", "/sessions/any-id/kill"),
            ("POST", "/recover"),
            ("GET", "/metrics"),
        ],
    )
    async def test_endpoint_requires_auth(
        self, app, monkeypatch, tmp_path, method: str, path: str
    ):
        """Each endpoint should return 401 without a valid token."""
        token_path = tmp_path / "token"
        ensure_token(str(token_path))
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        async with _client(app) as client:
            response = await client.request(method, path)
        assert response.status_code == 401, (
            f"{method} {path} returned {response.status_code}, expected 401"
        )
