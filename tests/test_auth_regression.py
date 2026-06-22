"""Regression tests for auth TOCTOU vulnerability.

This test file documents the bug fixed in commit f8084de:
https://github.com/anthropic/coding-agents/commit/f8084de

** The Bug **
In `ensure_token()`, when a token file exists, the code called:
    token = load_token(token_path)

This passed the *original unresolved parameter* to `load_token()`, which
internally calls `get_token_path()` again. If the environment variable
`CODING_AGENTS_TOKEN_PATH` changed between the two `get_token_path()` calls,
the existence check and the load would operate on different files — a classic
TOCTOU (time-of-check-time-of-use) race condition.

** The Fix **
Pass the already-resolved absolute path to `load_token()`:
    token = load_token(str(path))

This ensures both the existence check and the load operate on the same file,
eliminating the race window.

** Why This Matters **
An attacker who can modify environment variables between the two calls could
trick `ensure_token()` into loading a token from a different file than the one
it checked for existence. This could lead to:
- Loading a token the attacker controls
- Bypassing authentication checks
- Silent auth failures in production

The fix ensures path resolution happens exactly once per operation.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from coding_agents.auth import ensure_token, get_token_path, load_token


class TestTOCTOURegression:
    """Tests for the TOCTOU vulnerability in ensure_token() (commit f8084de).

    The bug was a single-call issue: within one ensure_token() invocation,
    get_token_path() was called twice (once in ensure_token, once inside
    load_token), and the env var could change between them.
    """

    def test_ensure_token_passes_resolved_path_to_load_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Verify that ensure_token() passes the resolved path, not the
        original unresolved parameter, to load_token().

        Before the fix: load_token(token_path) was called with the original
        parameter (e.g. None or '~/token'), causing a second path resolution.
        After the fix: load_token(str(path)) is called with the already-resolved
        absolute path.
        """
        token_file = tmp_path / "token"
        token_file.write_text("real-token")

        # Set env var to point to the token file
        monkeypatch.setenv("CODING_AGENTS_TOKEN_PATH", str(token_file))

        # Patch load_token to spy on what argument ensure_token passes to it
        original_load_token = load_token
        calls_received = []

        def spy_load_token(token_path=None):
            calls_received.append(token_path)
            return original_load_token(token_path)

        with patch("coding_agents.auth.load_token", side_effect=spy_load_token):
            token = ensure_token()

        assert token == "real-token"
        assert len(calls_received) == 1
        # The critical assertion: ensure_token must pass the RESOLVED path,
        # not None or the original parameter. If it passed None, load_token
        # would re-resolve using the env var — re-introducing the TOCTOU.
        passed_path = calls_received[0]
        assert passed_path is not None, (
            "ensure_token passed None to load_token — this re-introduces the "
            "TOCTOU race. Must pass the resolved path: load_token(str(path))"
        )
        assert passed_path == str(token_file.resolve())

    def test_ensure_token_tilde_expansion_passed_resolved(
        self, tmp_path: Path
    ):
        """Verify that tilde paths are expanded before passing to load_token().

        Before the fix: ensure_token('~/token') would pass '~/token' (unexpanded)
        to load_token, which would expand it independently — two expansions that
        could theoretically diverge.
        After the fix: the tilde is expanded once in get_token_path(), and the
        resolved absolute path is passed to load_token.
        """
        home = Path.home()
        test_token = home / "test-token-regression-tilde"
        test_token.write_text("tilde-token-value")

        try:
            original_load_token = load_token
            calls_received = []

            def spy_load_token(token_path=None):
                calls_received.append(token_path)
                return original_load_token(token_path)

            with patch("coding_agents.auth.load_token", side_effect=spy_load_token):
                token = ensure_token("~/test-token-regression-tilde")

            assert token == "tilde-token-value"
            assert len(calls_received) == 1
            passed_path = calls_received[0]
            # Must be the expanded, absolute path — not '~/...'
            assert passed_path is not None
            assert "~" not in passed_path, (
                "ensure_token passed an unexpanded tilde path to load_token — "
                "must pass the resolved absolute path"
            )
            assert os.path.isabs(passed_path)
        finally:
            test_token.unlink(missing_ok=True)

    def test_load_token_resolves_path_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify that load_token() correctly uses the resolved path.

        Unlike the ensure_token bug, load_token resolves the path once and
        uses it for both the existence check and the read. This test documents
        the expected behavior.
        """
        token_file = tmp_path / "token"
        token_file.write_text("my-token")

        monkeypatch.setenv("CODING_AGENTS_TOKEN_PATH", str(token_file))
        assert load_token() == "my-token"

        # Changing the env var makes load_token resolve to the new file
        other_file = tmp_path / "other"
        other_file.write_text("other-token")
        monkeypatch.setenv("CODING_AGENTS_TOKEN_PATH", str(other_file))
        assert load_token() == "other-token"


class TestPathResolutionConsistency:
    """Tests to verify path resolution is consistent across all auth functions."""

    def test_all_functions_use_same_resolution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify that get_token_path, load_token, and ensure_token all
        resolve paths consistently.
        """
        token_file = tmp_path / "token"
        token_file.write_text("test-token")

        # Set env var
        monkeypatch.setenv("CODING_AGENTS_TOKEN_PATH", str(token_file))

        # All three should resolve to the same path
        path1 = get_token_path()
        path2 = get_token_path(None)
        path3 = get_token_path(str(token_file))

        assert path1 == path2 == path3 == token_file.resolve()

        # load_token should load from the same path
        token = load_token()
        assert token == "test-token"

        # ensure_token should load from the same path
        token = ensure_token()
        assert token == "test-token"

    def test_explicit_path_overrides_env_var_consistently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Verify that explicit path parameter takes precedence over env var
        in all auth functions.
        """
        env_file = tmp_path / "env-token"
        explicit_file = tmp_path / "explicit-token"
        env_file.write_text("env-token-value")
        explicit_file.write_text("explicit-token-value")

        # Set env var to env_file
        monkeypatch.setenv("CODING_AGENTS_TOKEN_PATH", str(env_file))

        # get_token_path with explicit path should ignore env var
        path = get_token_path(str(explicit_file))
        assert path == explicit_file.resolve()

        # load_token with explicit path should load from explicit file
        token = load_token(str(explicit_file))
        assert token == "explicit-token-value"

        # ensure_token with explicit path should load from explicit file
        token = ensure_token(str(explicit_file))
        assert token == "explicit-token-value"


class TestFDLeakInRaceHandlerRegression:
    """Regression test for FD leak in ensure_token() FileExistsError handler.

    Commit 16fef65 fixed the FD leak for the first os.open/os.fdopen pair:
    when os.open succeeded but os.fdopen failed, the fd was not closed.
    But the fix missed the *second* os.open/os.fdopen pair inside the
    FileExistsError handler — the same bug existed there.

    The outer ``except BaseException`` only catches exceptions from the
    ``try`` block, not from other ``except`` blocks. So if os.open succeeded
    inside FileExistsError but os.fdopen failed, the fd leaked.

    Fix: wrap the inner os.open/os.fdopen in its own try/except with fd
    cleanup, matching the pattern used for the outer pair.
    """

    def test_fd_not_leaked_when_fdopen_fails_in_race_handler(
        self, tmp_path: Path
    ):
        """When os.fdopen fails in the FileExistsError handler, the fd must
        be closed to avoid leaking.
        """
        from unittest.mock import patch

        token_file = tmp_path / "token"
        token_file.write_text("")  # empty so ensure_token tries to regenerate
        fake_fd = 999
        close_calls: list[int] = []

        def tracking_close(fd: int) -> None:
            close_calls.append(fd)
            # Don't actually close — it's a fake fd

        open_call_count = {"n": 0}

        def mock_open(path: str, flags: int, mode: int = 0o777) -> int:
            open_call_count["n"] += 1
            if open_call_count["n"] == 1:
                # First os.open (O_EXCL): simulate another process winning the race
                raise FileExistsError("simulated race")
            # Second os.open (O_TRUNC inside FileExistsError handler): return fake fd
            return fake_fd

        def mock_fdopen(fd: int, mode: str = "r"):
            raise OSError("simulated fdopen failure")

        with patch("os.open", side_effect=mock_open), \
             patch("os.fdopen", side_effect=mock_fdopen), \
             patch("os.close", side_effect=tracking_close), \
             patch("coding_agents.auth.load_token", return_value=""):
            with pytest.raises(OSError, match="simulated fdopen failure"):
                ensure_token(str(token_file))

        # The critical assertion: os.close must have been called with our fake fd
        assert fake_fd in close_calls, (
            "FD leak! os.close was not called for the fd opened in the "
            "FileExistsError handler when os.fdopen failed"
        )


class TestValidateTokenEmptyFileRegression:
    """Regression test for validate_token accepting empty tokens.

    The security hardening (commit 07bbddf) changed load_token to return ""
    for empty/unreadable files (instead of None) to prevent dev-mode bypass.
    But validate_token still checked `if stored is None`, so "" passed through
    to secrets.compare_digest(provided, ""). Since compare_digest("", "") == True,
    an empty provided token would be accepted when the file was empty/corrupt.
    """

    def test_validate_token_rejects_empty_stored_token(self, tmp_path: Path):
        """validate_token must return False when the stored token is empty,
        even if the provided token is also empty.
        """
        from coding_agents.auth import validate_token

        token_file = tmp_path / "token"
        token_file.write_text("")  # empty file

        # Empty provided token must NOT be accepted
        assert validate_token("", str(token_file)) is False
        # Non-empty provided token must also be rejected
        assert validate_token("any-token", str(token_file)) is False

    def test_validate_token_rejects_when_file_unreadable(self, tmp_path: Path):
        """validate_token must return False when the file can't be read.

        We simulate an unreadable file by using a directory path instead of
        a file path — this causes read_text() to raise OSError.
        """
        from coding_agents.auth import validate_token

        # Create a directory (not a file) — read_text() will fail
        token_dir = tmp_path / "token_dir"
        token_dir.mkdir()

        # load_token returns "" for unreadable files
        assert load_token(str(token_dir)) == ""
        # validate_token must reject
        assert validate_token("any-token", str(token_dir)) is False


class TestCLIEmptyTokenFileRegression:
    """Regression test for CLI not regenerating empty token files.

    The CLI's global callback (``_global_options``) previously only called
    ``ensure_token()`` when the token file did NOT exist (``if not
    token_path.exists()``). But ``ensure_token()`` also handles the case
    where the file exists but is empty — regenerating the token.

    When the file existed but was empty (e.g. crash during write, disk full,
    accidental truncation), the CLI skipped ``ensure_token()`` and the
    server would refuse all requests with a 500 error — auth was broken
    but the CLI didn't auto-fix it.

    Fix: always call ``ensure_token()`` from the CLI callback. It handles
    all cases (missing, empty, valid) internally.
    """

    def test_cli_regenerates_empty_token_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When the token file exists but is empty, the CLI callback must
        regenerate it so the server can start with valid auth.
        """
        from typer.testing import CliRunner
        from coding_agents.cli import app

        token_file = tmp_path / "token"
        token_file.write_text("")  # simulate crash-during-write
        assert token_file.exists()
        assert token_file.read_text() == ""

        runner = CliRunner()
        result = runner.invoke(app, ["--auth-token-file", str(token_file), "list"])

        # The token file should now contain a valid token (64 hex chars)
        content = token_file.read_text().strip()
        assert len(content) == 64, (
            f"CLI did not regenerate empty token file. Content: {content!r}"
        )
        int(content, 16)  # must be valid hex

    def test_cli_does_not_overwrite_valid_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When the token file exists and has a valid token, the CLI must
        NOT regenerate it (preserving the existing token).
        """
        from typer.testing import CliRunner
        from coding_agents.cli import app

        token_file = tmp_path / "token"
        token_file.write_text("existing-valid-token\n")

        runner = CliRunner()
        runner.invoke(app, ["--auth-token-file", str(token_file), "list"])

        # The existing token should be preserved
        content = token_file.read_text().strip()
        assert content == "existing-valid-token", (
            "CLI should not overwrite an existing valid token"
        )
