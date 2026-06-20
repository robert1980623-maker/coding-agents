"""Pytest fixtures for mock CLI E2E tests.

Creates a temporary directory with mock claude/codex binaries,
injects them into PATH so the real CLI is not invoked.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def mock_cli_dir(tmp_path: Path) -> Path:
    """Create a temporary directory with mock claude/codex binaries.

    Returns the path to the directory (caller should inject into PATH).
    """
    mock_dir = tmp_path / "mock_cli"
    mock_dir.mkdir()

    # Copy mock scripts with executable permissions
    mock_claude = Path(__file__).parent / "mock_claude.py"
    mock_codex = Path(__file__).parent / "mock_codex.py"

    claude_bin = mock_dir / "claude"
    codex_bin = mock_dir / "codex"

    # Read and write with shebang
    claude_bin.write_text(mock_claude.read_text())
    codex_bin.write_text(mock_codex.read_text())

    # Make executable
    claude_bin.chmod(claude_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    codex_bin.chmod(codex_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return mock_dir


@pytest.fixture
def mock_cli_env(mock_cli_dir: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Inject mock CLI directory into PATH.

    Returns modified environment dict.
    """
    # Prepend mock_dir to PATH
    original_path = os.environ.get("PATH", "")
    new_path = f"{mock_cli_dir}:{original_path}"

    monkeypatch.setenv("PATH", new_path)

    return {"PATH": new_path}
