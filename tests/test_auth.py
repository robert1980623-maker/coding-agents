"""Tests for auth token management."""

from __future__ import annotations

import os
from pathlib import Path

from coding_agents.auth import (
    ensure_token,
    generate_token,
    get_token_path,
    load_token,
    validate_token,
)


class TestGenerateToken:
    def test_generates_hex_string(self):
        token = generate_token()
        assert isinstance(token, str)
        assert len(token) == 64  # 32 bytes = 64 hex chars
        # Should be valid hex
        int(token, 16)

    def test_tokens_are_unique(self):
        tokens = {generate_token() for _ in range(10)}
        assert len(tokens) == 10


class TestGetTokenPath:
    def test_default_path(self):
        path = get_token_path()
        assert path.name == ".coding-agents-token"
        assert path.is_absolute()

    def test_explicit_path(self, tmp_path: Path):
        explicit = str(tmp_path / "my-token")
        path = get_token_path(explicit)
        assert str(path) == str(tmp_path / "my-token")

    def test_expands_tilde(self):
        path = get_token_path("~/test-token")
        assert "~" not in str(path)
        assert path.is_absolute()

    def test_env_var_fallback(self, tmp_path: Path, monkeypatch):
        env_path = str(tmp_path / "env-token")
        monkeypatch.setenv("CODING_AGENTS_TOKEN_PATH", env_path)
        path = get_token_path()
        assert str(path) == env_path

    def test_explicit_path_overrides_env_var(self, tmp_path: Path, monkeypatch):
        env_path = str(tmp_path / "env-token")
        explicit_path = str(tmp_path / "explicit-token")
        monkeypatch.setenv("CODING_AGENTS_TOKEN_PATH", env_path)
        path = get_token_path(explicit_path)
        assert str(path) == explicit_path


class TestEnsureToken:
    def test_creates_token_file(self, tmp_path: Path):
        token_file = str(tmp_path / "token")
        token = ensure_token(token_file)
        assert len(token) == 64
        assert Path(token_file).exists()
        # File permissions should be 0600
        mode = os.stat(token_file).st_mode & 0o777
        assert mode == 0o600

    def test_loads_existing_token(self, tmp_path: Path):
        token_file = tmp_path / "token"
        token_file.write_text("existing-token-value\n")
        loaded = ensure_token(str(token_file))
        assert loaded == "existing-token-value"

    def test_idempotent(self, tmp_path: Path):
        token_file = str(tmp_path / "token")
        t1 = ensure_token(token_file)
        t2 = ensure_token(token_file)
        assert t1 == t2

    def test_regenerates_empty_file(self, tmp_path: Path):
        token_file = tmp_path / "token"
        token_file.write_text("")  # empty
        token = ensure_token(str(token_file))
        assert len(token) == 64


class TestLoadToken:
    def test_returns_none_if_missing(self, tmp_path: Path):
        result = load_token(str(tmp_path / "nonexistent"))
        assert result is None

    def test_returns_token_content(self, tmp_path: Path):
        token_file = tmp_path / "token"
        token_file.write_text("my-secret\n")
        assert load_token(str(token_file)) == "my-secret"

    def test_returns_empty_string_for_empty_file(self, tmp_path: Path):
        """Empty token file returns ``""`` (not ``None``).

        ``None`` means "file missing" (dev-mode bypass OK).
        ``""`` means "file exists but is broken" (auth must reject).
        """
        token_file = tmp_path / "token"
        token_file.write_text("")
        assert load_token(str(token_file)) == ""


class TestValidateToken:
    def test_valid_token(self, tmp_path: Path):
        token_file = str(tmp_path / "token")
        stored = ensure_token(token_file)
        assert validate_token(stored, token_file) is True

    def test_invalid_token(self, tmp_path: Path):
        token_file = str(tmp_path / "token")
        ensure_token(token_file)
        assert validate_token("wrong-token", token_file) is False

    def test_missing_file(self, tmp_path: Path):
        assert validate_token("any", str(tmp_path / "missing")) is False
