"""CLI for the coding agent runtime."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
import typer
from rich.console import Console
from rich.table import Table

from coding_agents.agents.factory import get_agent
from coding_agents.auth import ensure_token, get_token_path
from coding_agents.executor import StreamExecutor
from coding_agents.logging_config import setup_logging
from coding_agents.models import (
    AgentType,
    Event,
    EventType,
    ExecutionConfig,
    Session,
    SessionStatus,
)
from coding_agents.registry import SessionRegistry
from coding_agents.storage.sqlite import SQLiteStorage

app = typer.Typer(
    name="coding-agents",
    help="A unified runtime for managing coding agents (Claude Code, Codex, etc.)",
    no_args_is_help=True,
)
console = Console()
logger = structlog.get_logger(__name__)

# Module-level default storage path
DEFAULT_DB = "~/.coding-agents/data.db"


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
) -> None:
    """Global options for all commands."""
    setup_logging(level=log_level, json_output=log_json)
    # Ensure auth token is available (generate if missing)
    # In Phase 1 we just materialize the token; Phase 2 HTTP server will validate it.
    token_path = get_token_path(auth_token_file)
    if not token_path.exists():
        token = ensure_token(auth_token_file)
        console.print(
            f"[dim]Generated auth token at {token_path}[/dim]"
        )
        _ = token  # phase 1: not consumed yet


def _get_storage() -> SQLiteStorage:
    return SQLiteStorage(DEFAULT_DB)


def _run_async(coro: Any) -> Any:
    """Run an async function in a sync CLI context."""
    return asyncio.run(coro)


@app.command()
def run(
    agent: str = typer.Argument(..., help="Agent type: claude or codex"),
    prompt: str = typer.Argument(..., help="Prompt to send to the agent"),
    workdir: str = typer.Option(".", help="Working directory for the agent"),
    model: Optional[str] = typer.Option(None, help="Model override"),
    budget: Optional[float] = typer.Option(None, help="Max budget in USD"),
    output_mode: str = typer.Option("standard", help="Output mode: standard or passthrough"),
    stream: bool = typer.Option(False, "--stream", help="Stream events in real-time with annotations"),
    verbose: bool = typer.Option(False, help="Verbose output"),
) -> None:
    """Run a coding agent session."""
    _run_async(_run_session(agent, prompt, workdir, model, budget, output_mode, stream, verbose))


async def _run_session(
    agent_name: str,
    prompt: str,
    workdir: str,
    model: Optional[str],
    budget: Optional[float],
    output_mode: str,
    stream: bool,
    verbose: bool,
) -> None:
    try:
        agent_type = AgentType(agent_name)
    except ValueError:
        console.print(f"[red]Unknown agent type: {agent_name}[/red]")
        raise typer.Exit(code=1)

    adapter = get_agent(agent_type)
    config = ExecutionConfig(output_mode=output_mode, model=model)
    if budget is not None:
        config.max_budget_usd = budget

    command = adapter.build_command(prompt, config)
    if verbose:
        console.print(f"[dim]command: {' '.join(command)}[/dim]")

    storage = _get_storage()
    await storage.initialize()

    session = Session(agent=agent_type, prompt=prompt, workdir=workdir, model=model)
    await storage.create_session(session)

    registry = SessionRegistry()
    acquired = await registry.acquire(session.id)
    if not acquired:
        console.print("[red]Timed out waiting for execution slot[/red]")
        await storage.update_session(
            session.id,
            status=SessionStatus.FAILED,
            finished_at=datetime.now(timezone.utc),
            metadata={"error": "Timed out waiting for execution slot"},
        )
        await storage.close()
        raise typer.Exit(code=1)

    executor = StreamExecutor(store=storage, config=config)

    last_stdout_data = ""
    last_stderr_data = ""
    exit_code: Optional[int] = None

    try:
        async for event in executor.execute(session.id, command, workdir):
            if event.type == EventType.STDOUT:
                if stream:
                    sys.stderr.write(f"[stdout seq={event.seq}] {event.data}")
                    if not event.data.endswith("\n"):
                        sys.stderr.write("\n")
                    sys.stderr.flush()
                else:
                    last_stdout_data = event.data
            elif event.type == EventType.STDERR:
                if stream:
                    sys.stderr.write(f"[stderr seq={event.seq}] {event.data}")
                    if not event.data.endswith("\n"):
                        sys.stderr.write("\n")
                    sys.stderr.flush()
                else:
                    last_stderr_data = event.data
            elif event.type == EventType.RESULT:
                data = json.loads(event.data)
                exit_code = data.get("exit_code")
                if not stream:
                    # In non-stream mode, print the final output
                    if last_stdout_data:
                        sys.stdout.write(last_stdout_data)
                        if not last_stdout_data.endswith("\n"):
                            sys.stdout.write("\n")
                        sys.stdout.flush()
                    if last_stderr_data:
                        sys.stderr.write(last_stderr_data)
                        if not last_stderr_data.endswith("\n"):
                            sys.stderr.write("\n")
                        sys.stderr.flush()
                if verbose or stream:
                    console.print(f"[dim]exit_code={exit_code}[/dim]")
            elif event.type == EventType.ERROR and stream:
                sys.stderr.write(f"[error seq={event.seq}] {event.data}\n")
                sys.stderr.flush()
            elif event.type == EventType.SESSION_START and stream:
                start_data = json.loads(event.data)
                sys.stderr.write(
                    f"[system seq={event.seq}] session started: "
                    f"{start_data.get('session_id', '')[:8]} "
                    f"agent={start_data.get('agent', '?')}\n"
                )
                sys.stderr.flush()
    except Exception as e:
        console.print(f"[red]Execution error: {e}[/red]")
        await storage.update_session(
            session.id,
            status=SessionStatus.FAILED,
            finished_at=datetime.now(timezone.utc),
            metadata={"error": str(e)},
        )
    finally:
        await registry.release(session.id)
        await storage.close()


@app.command()
def status(
    session_id: str = typer.Argument(..., help="Session ID"),
) -> None:
    """Show the status of a session."""
    _run_async(_show_status(session_id))


async def _show_status(session_id: str) -> None:
    storage = _get_storage()
    await storage.initialize()
    try:
        session = await storage.get_session(session_id)
        if session is None:
            console.print(f"[red]Session not found: {session_id}[/red]")
            raise typer.Exit(code=1)
        tags = await storage.list_tags(session_id)
        _print_session(session, tags)
    finally:
        await storage.close()


@app.command(name="list")
def list_sessions(
    agent: Optional[str] = typer.Option(None, help="Filter by agent type"),
    status: Optional[str] = typer.Option(None, help="Filter by status"),
    tag: Optional[str] = typer.Option(None, help="Filter by tag"),
    limit: int = typer.Option(100, help="Max results"),
) -> None:
    """List sessions."""
    _run_async(_list_sessions(agent, status, tag, limit))


async def _list_sessions(
    agent: Optional[str],
    status: Optional[str],
    tag: Optional[str],
    limit: int,
) -> None:
    storage = _get_storage()
    await storage.initialize()
    try:
        tags = [tag] if tag else None
        sessions = await storage.list_sessions(agent=agent, status=status, tags=tags, limit=limit)
        if not sessions:
            console.print("[dim]No sessions found.[/dim]")
            return
        table = Table(title="Sessions")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Agent", style="magenta")
        table.add_column("Status", style="green")
        table.add_column("Started")
        table.add_column("Duration (ms)")
        table.add_column("Prompt", overflow="fold")

        for s in sessions:
            started = s.started_at.strftime("%Y-%m-%d %H:%M:%S") if s.started_at else "-"
            dur = str(s.duration_ms) if s.duration_ms is not None else "-"
            prompt_short = s.prompt[:60] + "..." if len(s.prompt) > 60 else s.prompt
            table.add_row(
                s.id[:8],
                s.agent.value,
                s.status.value,
                started,
                dur,
                prompt_short,
            )
        console.print(table)
    finally:
        await storage.close()


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query (FTS5)"),
    agent: Optional[str] = typer.Option(None, help="Filter by agent type"),
    limit: int = typer.Option(20, help="Max results"),
) -> None:
    """Full-text search across agent events."""
    _run_async(_search_events(query, agent, limit))


async def _search_events(query: str, agent: Optional[str], limit: int) -> None:
    storage = _get_storage()
    await storage.initialize()
    try:
        events = await storage.search_events(query, agent=agent, limit=limit)
        if not events:
            console.print("[dim]No matching events found.[/dim]")
            return
        for ev in events:
            console.print(
                f"[cyan]{ev.session_id[:8]}[/cyan] [{ev.channel}] {ev.data.strip()}"
            )
    finally:
        await storage.close()


@app.command()
def kill(
    session_id: str = typer.Argument(..., help="Session ID to kill"),
) -> None:
    """Terminate a running session."""
    _run_async(_kill_session(session_id))


async def _kill_session(session_id: str) -> None:
    storage = _get_storage()
    await storage.initialize()
    try:
        session = await storage.get_session(session_id)
        if session is None:
            console.print(f"[red]Session not found: {session_id}[/red]")
            raise typer.Exit(code=1)

        if session.status not in {SessionStatus.RUNNING, SessionStatus.PENDING}:
            console.print(f"[yellow]Session is already {session.status.value}[/yellow]")
            return

        await storage.update_session(
            session_id,
            status=SessionStatus.KILLED,
            finished_at=datetime.now(timezone.utc),
        )
        console.print(f"[green]Killed session {session_id}[/green]")
    finally:
        await storage.close()


@app.command()
def recover(
    timeout: int = typer.Option(300, help="Heartbeat timeout in seconds"),
) -> None:
    """Recover orphaned sessions."""
    _run_async(_recover_sessions(timeout))


async def _recover_sessions(timeout: int) -> None:
    storage = _get_storage()
    await storage.initialize()
    try:
        count = await storage.recover_orphaned_sessions(timeout_seconds=timeout)
        console.print(f"[green]Marked {count} orphaned session(s)[/green]")
    finally:
        await storage.close()


@app.command()
def tag(
    session_id: str = typer.Argument(..., help="Session ID"),
    tag_name: str = typer.Argument(..., help="Tag to add/remove"),
    remove: bool = typer.Option(False, "-r", "--remove", help="Remove the tag instead of adding"),
) -> None:
    """Manage tags on a session."""
    _run_async(_manage_tag(session_id, tag_name, remove))


async def _manage_tag(session_id: str, tag_name: str, remove: bool) -> None:
    storage = _get_storage()
    await storage.initialize()
    try:
        session = await storage.get_session(session_id)
        if session is None:
            console.print(f"[red]Session not found: {session_id}[/red]")
            raise typer.Exit(code=1)
        if remove:
            await storage.remove_tag(session_id, tag_name)
            console.print(f"[green]Removed tag '{tag_name}' from {session_id}[/green]")
        else:
            await storage.add_tag(session_id, tag_name)
            console.print(f"[green]Added tag '{tag_name}' to {session_id}[/green]")
    finally:
        await storage.close()


def _print_session(session: Session, tags: list[str]) -> None:
    """Pretty-print a session."""
    console.print(f"[bold]Session[/bold]: {session.id}")
    console.print(f"  Agent: {session.agent.value}")
    console.print(f"  Status: {session.status.value}")
    console.print(f"  Workdir: {session.workdir}")
    console.print(f"  Prompt: {session.prompt}")
    if session.pid is not None:
        console.print(f"  PID: {session.pid}")
    if session.exit_code is not None:
        console.print(f"  Exit code: {session.exit_code}")
    if session.started_at:
        console.print(f"  Started: {session.started_at}")
    if session.finished_at:
        console.print(f"  Finished: {session.finished_at}")
    if session.duration_ms is not None:
        console.print(f"  Duration: {session.duration_ms}ms")
    if session.cost_usd is not None:
        console.print(f"  Cost: ${session.cost_usd:.4f}")
    if session.model:
        console.print(f"  Model: {session.model}")
    if tags:
        console.print(f"  Tags: {', '.join(tags)}")


# Register skill sub-commands
from coding_agents.cli_skill import app as skill_app  # noqa: E402

app.add_typer(skill_app, name="skill")


if __name__ == "__main__":
    app()
