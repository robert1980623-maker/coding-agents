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
        """verify_token reads from request.state, not from the file.

        The middleware reads the token file once per request and stores the
        validated token in ``request.state.auth_token``. verify_token reads
        from there — it does NOT re-read the file. This eliminates the
        TOCTOU window where the file could change between the middleware
        read and the dependency read within the same request.

        We verify this by directly testing verify_token with a mock request
        that has auth_token set, and confirming it doesn't call load_token
        even when the file has changed.
        """
        from starlette.requests import Request
        from coding_agents.http.auth import verify_token

        # Create a mock request with auth_token already set by middleware
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "headers": [],
        }
        request = Request(scope)
        request.state.auth_token = "middleware-validated-token"

        # Patch load_token to return a different value. If verify_token
        # re-reads the file, it would get this patched value and fail.
        # Instead, it should read from request.state and succeed.
        import coding_agents.auth as auth_mod
        original_load = auth_mod.load_token

        def fake_load_token(path=None):
            return "tampered-token"

        auth_mod.load_token = fake_load_token
        try:
            # verify_token should return the value from request.state,
            # not re-read the file
            result = await verify_token(request)
            assert result == "middleware-validated-token"
        finally:
            auth_mod.load_token = original_load

    async def test_token_file_change_picked_up_on_next_request(
        self, app, monkeypatch, tmp_path
    ):
        """When the token file is regenerated, subsequent requests use the
        new token. The middleware re-reads the file on every request so it
        picks up token changes (e.g. CLI regenerating a corrupt file).
        """
        token_path = tmp_path / "token"
        original_token = ensure_token(str(token_path))
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        async with _client(app, {"Authorization": f"Bearer {original_token}"}) as client:
            # First request with original token succeeds
            response1 = await client.get("/sessions")
            assert response1.status_code == 200

            # Regenerate the token file with a new value
            new_token = "newly-generated-token-value"
            token_path.write_text(new_token + "\n")

            # Old token now fails
            response2 = await client.get("/sessions")
            assert response2.status_code == 401

        # New token succeeds
        async with _client(app, {"Authorization": f"Bearer {new_token}"}) as client:
            response3 = await client.get("/sessions")
            assert response3.status_code == 200


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


class TestTokenFileDeletionAfterAuthActive:
    """If the token file is deleted after the server has loaded a valid token,
    the middleware must NOT fall through to dev-mode bypass — that would
    silently disable auth (security bug).

    Instead, requests must be rejected with 500. Dev mode is only valid
    when the token file has NEVER been seen — once auth has been active,
    a missing file is a broken-auth condition.
    """

    async def test_deleted_token_file_rejects_not_dev_mode(
        self, app, monkeypatch, tmp_path
    ):
        """After a valid token is loaded, deleting the file → 500, NOT 200.

        Without this fix, deleting the token file would cause load_token to
        return None, which the middleware would treat as dev mode — silently
        disabling auth and allowing all requests through.
        """
        token_path = tmp_path / "token"
        token = ensure_token(str(token_path))
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        # First request with valid token succeeds (and marks auth as active)
        async with _client(app, {"Authorization": f"Bearer {token}"}) as client:
            response = await client.get("/sessions")
        assert response.status_code == 200

        # Now delete the token file — simulates an attacker or accidental deletion
        token_path.unlink()
        assert not token_path.exists()

        # Without a valid token: should be 500 (broken auth), NOT 200 (dev mode)
        async with _client(app) as client:
            response = await client.get("/sessions")
        # Must NOT be 200 — that would mean auth was silently bypassed
        assert response.status_code == 500, (
            "Deleted token file after auth was active must return 500, "
            f"not {response.status_code} (which would mean dev-mode bypass)"
        )
        assert "missing" in response.json()["detail"].lower()

    async def test_deleted_token_file_still_allows_health(
        self, app, monkeypatch, tmp_path
    ):
        """/health is fully public — works even after token file is deleted."""
        token_path = tmp_path / "token"
        token = ensure_token(str(token_path))
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        # Activate auth with a valid request
        async with _client(app, {"Authorization": f"Bearer {token}"}) as client:
            await client.get("/sessions")

        # Delete the file
        token_path.unlink()

        # /health still works
        async with _client(app) as client:
            response = await client.get("/health")
        assert response.status_code == 200

    async def test_token_file_recreated_after_deletion_works(
        self, app, monkeypatch, tmp_path
    ):
        """After deletion + 500, recreating the file restores auth."""
        token_path = tmp_path / "token"
        token = ensure_token(str(token_path))
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        # Activate auth
        async with _client(app, {"Authorization": f"Bearer {token}"}) as client:
            response = await client.get("/sessions")
        assert response.status_code == 200

        # Delete → 500
        token_path.unlink()
        async with _client(app) as client:
            response = await client.get("/sessions")
        assert response.status_code == 500

        # Recreate the file with a new token → auth works again
        new_token = "newly-regenerated-token"
        token_path.write_text(new_token + "\n")
        async with _client(app, {"Authorization": f"Bearer {new_token}"}) as client:
            response = await client.get("/sessions")
        assert response.status_code == 200

    async def test_deleted_token_file_logs_error(
        self, app, monkeypatch, tmp_path, caplog
    ):
        """Deleting the token file after auth was active logs an error."""
        token_path = tmp_path / "token"
        token = ensure_token(str(token_path))
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        # Activate auth
        async with _client(app, {"Authorization": f"Bearer {token}"}) as client:
            await client.get("/sessions")

        # Delete the file → should log error
        token_path.unlink()
        with caplog.at_level(logging.ERROR, logger="coding_agents.http.middleware"):
            async with _client(app) as client:
                await client.get("/sessions")

        assert any(
            "auth_broken_token_file_missing" in r.getMessage()
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
            ("POST", "/sessions/any-id/resume"),
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


class TestVerifyTokenWithoutMiddleware:
    """verify_token must raise 401 if the middleware didn't set auth_token."""

    async def test_raises_when_auth_token_not_set(self):
        """If auth_token was never placed on request.state, verify_token
        must reject the request rather than silently returning ''."""
        from fastapi import APIRouter, Depends, FastAPI
        from fastapi.testclient import TestClient

        from coding_agents.http.auth import verify_token

        bare_app = FastAPI()
        router = APIRouter()

        @router.get("/test")
        async def test_route(token: str = Depends(verify_token)):
            return {"token": token}

        bare_app.include_router(router)
        # No BearerTokenMiddleware — simulates a route that bypasses
        # the middleware (e.g. mounted on a different app).
        client = TestClient(bare_app)
        response = client.get("/test")
        assert response.status_code == 401
        assert response.json()["detail"] == "Authentication required"

    async def test_dev_mode_still_passes(self, app, monkeypatch, tmp_path):
        """Dev mode (no token file) sets auth_token='' — must still pass."""
        token_path = tmp_path / "token"
        # Don't create the file → dev mode
        monkeypatch.setattr("coding_agents.auth.DEFAULT_TOKEN_PATH", str(token_path))

        async with _client(app) as client:
            response = await client.get("/sessions")
        # Should NOT be 401 — dev mode disables auth with a warning
        assert response.status_code != 401
