"""CLI for the coding agent runtime.

The top-level :data:`app` is the Typer entry point referenced by
``pyproject.toml`` (``coding-agents = "coding_agents.cli:app"``).
Each command family lives in its own sub-module and exposes a
``register(app)`` function called below.
"""

from __future__ import annotations

import os
from typing import Optional

import typer
from rich.console import Console

from coding_agents.auth import ensure_token, get_token_path
from coding_agents.logging_config import setup_logging
from coding_agents._version import __version__

# Re-export factory functions for backward compatibility with tests that
# mock via ``patch("coding_agents.cli.get_agent", ...)``.
from coding_agents.agents.factory import get_agent  # noqa: F401  (re-export)

# --- Public package attributes ------------------------------------------------
# ``DEFAULT_DB``, ``_get_storage`` and ``_run_async`` are defined in
# ``_utils.py`` and re-exported here so existing callers / tests that
# imported them from ``coding_agents.cli`` keep working.
from coding_agents.cli._utils import (  # noqa: F401  (re-exports)
    DEFAULT_DB,
    _format_duration,
    _get_storage,
    _parse_duration,
    _run_async,
)

app = typer.Typer(
    name="coding-agents",
    help="A unified runtime for managing coding agents (Claude Code, Codex, etc.)",
    no_args_is_help=True,
    invoke_without_command=True,
)
console = Console()


@app.callback()
def _global_options(
    log_level: str = typer.Option("INFO", "--log-level", help="Log level: DEBUG, INFO, WARNING, ERROR"),
    log_json: bool = typer.Option(True, "--log-json/--no-log-json", help="Output logs as JSON lines"),
    auth_token_file: Optional[str] = typer.Option(
        None,
        "--auth-token-file",
        help="Path to auth token file (default: ~/.coding-agents-token). "
        "Auto-generated on first run if missing.",
    ),
    version: Optional[bool] = typer.Option(
        None, "--version", "-v",
        help="Show version and exit.",
        is_eager=True,
    ),
) -> None:
    """Global options for all commands."""
    # Handle --version flag
    if version is True:
        from rich import print as rich_print
        rich_print(f"coding-agents [bold]{__version__}[/bold]")
        raise typer.Exit()

    setup_logging(level=log_level, json_output=log_json)
    # Ensure auth token is available.
    # In Phase 1 we just materialize the token; Phase 2 HTTP server will validate it.
    #
    # We must call ensure_token() on every invocation — not just when the
    # file is missing — because it also handles the case where the file
    # exists but is empty/corrupt (crash during write, disk full, accidental
    # truncation). Without this, the server would refuse all requests with
    # a 500 error and the CLI would not auto-fix the broken auth state.
    token_path = get_token_path(auth_token_file)
    file_missing = not token_path.exists()
    ensure_token(auth_token_file)
    if file_missing:
        console.print(
            f"[dim]Generated auth token at {token_path}[/dim]"
        )


# --- Register sub-commands ----------------------------------------------------
# Each sub-module exposes ``register(app)`` that calls ``app.command(...)``
# for each of its commands. The local import defers the lookup of ``app``
# until after it has been defined above, avoiding a circular-import hazard
# (sub-modules import ``app`` from this package inside their ``register``).
from coding_agents.cli import inspect as _inspect_mod  # noqa: E402
from coding_agents.cli import manage as _manage_mod  # noqa: E402
from coding_agents.cli import poll as _poll_mod  # noqa: E402
from coding_agents.cli import resume as _resume_mod  # noqa: E402
from coding_agents.cli import run as _run_mod  # noqa: E402
from coding_agents.cli import server as _server_mod  # noqa: E402
from coding_agents.cli import tags as _tags_mod  # noqa: E402
from coding_agents.cli import watch as _watch_mod  # noqa: E402

_run_mod.register(app)
_inspect_mod.register(app)
_manage_mod.register(app)
_tags_mod.register(app)
_poll_mod.register(app)
_server_mod.register(app)
_resume_mod.register(app)
_watch_mod.register(app)


# Register skill sub-commands
from coding_agents.cli_skill import app as skill_app  # noqa: E402

app.add_typer(skill_app, name="skill")


if __name__ == "__main__":
    app()
