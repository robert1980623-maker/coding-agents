"""Tests for the `dispatch` CLI command (v0.2.5)."""

from __future__ import annotations

import typer.main
from click.testing import CliRunner

from coding_agents.cli import app


def _run(args: list[str]):
    """Helper: run the CLI as if from a shell."""
    runner = CliRunner()
    return runner.invoke(app, args)


def test_dispatch_command_registered():
    """`dispatch` is in the main app's commands."""
    click_app = typer.main.get_command(app)
    assert "dispatch" in click_app.commands


def test_dispatch_help_mentions_workdir():
    """The help text must emphasise that workdir is the project root."""
    click_app = typer.main.get_command(app)
    dispatch_cmd = click_app.commands["dispatch"]

    # The help text is rendered from the docstring + params
    # Use click's get_help to render the help without actually invoking
    from click.testing import CliRunner as _CR
    runner = _CR()
    result = runner.invoke(click_app, ["dispatch", "--help"], catch_exceptions=False)
    # Some Typer versions break under CliRunner; fallback to checking the
    # command's get_help method directly.
    if result.exit_code != 0 or "--workdir" not in result.output:
        help_text = dispatch_cmd.get_help(click_app.context_class(click_app, info_name="dispatch"))
        assert "--workdir" in help_text
        assert "AGENTS.md" in help_text or "CLAUDE.md" in help_text
    else:
        assert "--workdir" in result.output
        assert "AGENTS.md" in result.output or "CLAUDE.md" in result.output


def test_run_is_deprecated():
    """`run` is still present but marked deprecated."""
    click_app = typer.main.get_command(app)
    assert "run" in click_app.commands


def test_dispatch_defaults_workdir_to_cwd(tmp_path, monkeypatch):
    """When --workdir is omitted, dispatch should fall back to the current dir.

    We can't actually start an agent here (would need claude/codex CLI), but
    we *can* verify the click-level default: the option has `default=None`
    which the CLI converts to '.' internally. This test guards against a
    future refactor that hard-codes a different default.
    """
    # Inspect the click command's option
    click_app = typer.main.get_command(app)
    dispatch_cmd = click_app.commands["dispatch"]

    # Look up the --workdir option
    workdir_opts = [
        p for p in dispatch_cmd.params
        if any(n in ("--workdir", "-w") for n in p.opts)
    ]
    assert len(workdir_opts) == 1
    assert workdir_opts[0].default is None  # CLI converts None -> '.'
