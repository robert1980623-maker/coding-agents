"""``status``, ``tail``, ``list`` and ``search`` commands."""

from __future__ import annotations

import asyncio
import io
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from coding_agents.models import Session, SessionStatus


def register(app: typer.Typer) -> None:
    """Register the inspect-family commands on the given Typer app."""
    app.command()(status)
    app.command(name="tail")(tail_cmd)
    app.command(name="list")(list_sessions)
    app.command()(search)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def status(
    session_id: str = typer.Argument(..., help="Session ID"),
    limit: int = typer.Option(
        20, "--limit", "-n",
        help="Number of recent events to show (default: 20). "
             "Keeps output below the OpenClaw exec 1MB buffer.",
    ),
    no_events: bool = typer.Option(
        False, "--no-events",
        help="Skip events, show only session metadata.",
    ),
) -> None:
    """Show session metadata + the most recent events.

    By default prints the last 20 events (oldest-first within the window).
    Use `tail` if you want a longer or full stream.
    """
    from coding_agents.cli._utils import _run_async
    _run_async(_show_status(session_id, limit, no_events))


def tail_cmd(
    session_id: str = typer.Argument(..., help="Session ID"),
    limit: int = typer.Option(
        100, "--limit", "-n",
        help="Number of events to show (default: 100, oldest-first).",
    ),
    follow: bool = typer.Option(
        False, "--follow", "-f",
        help="Poll for new events until the session reaches a terminal status. "
             "WARNING: do not use from inside OpenClaw exec — the polling will "
             "hit the 1MB buffer. Use `status` or read SQLite directly.",
    ),
) -> None:
    """Show the most recent events of a session (one-line summaries).

    Like `status` but with a larger default window. Output is bounded so
    it fits inside the OpenClaw exec 1MB buffer.
    """
    from coding_agents.cli._utils import _run_async
    _run_async(_tail_session(session_id, limit, follow))


def list_sessions(
    agent: Optional[str] = typer.Option(None, help="Filter by agent type"),
    status: Optional[str] = typer.Option(None, help="Filter by status"),
    tag: Optional[str] = typer.Option(None, help="Filter by tag"),
    limit: int = typer.Option(100, help="Max results"),
    short_id: bool = typer.Option(
        False, "--short-id/--full-id",
        help="Show only the first 8 characters of each session ID "
             "(--short-id) or the full 36-character UUID (--full-id, the "
             "default). The full UUID is the default so it can be copy-"
             "pasted directly into `status` / `tail` / `kill`.",
    ),
) -> None:
    """List sessions."""
    from coding_agents.cli._utils import _run_async
    _run_async(_list_sessions(agent, status, tag, limit, short_id))


def search(
    query: str = typer.Argument(..., help="Search query (FTS5)"),
    agent: Optional[str] = typer.Option(None, help="Filter by agent type"),
    limit: int = typer.Option(20, help="Max results"),
) -> None:
    """Full-text search across agent events."""
    from coding_agents.cli._utils import _run_async
    _run_async(_search_events(query, agent, limit))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_event_summary(ev) -> None:
    """One-line summary for an event — bounded so 20 lines fit easily."""
    from coding_agents.cli._utils import console
    type_short = ev.type.value if hasattr(ev.type, "value") else str(ev.type)
    data_preview = ev.data if isinstance(ev.data, str) else str(ev.data)
    if len(data_preview) > 200:
        data_preview = data_preview[:200] + "..."
    console.print(f"  [dim]seq={ev.seq}[/dim] [{type_short}] {data_preview}")


def _print_session(session: Session, tags: list[str]) -> None:
    """Pretty-print a session."""
    from coding_agents.cli._utils import console
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


async def _show_status(session_id: str, limit: int, no_events: bool) -> None:
    from coding_agents.cli._utils import _get_storage, console
    storage = _get_storage()
    await storage.initialize()
    try:
        session = await storage.get_session(session_id)
        if session is None:
            console.print(f"[red]Session not found: {session_id}[/red]")
            raise typer.Exit(code=1)
        tags = await storage.list_tags(session_id)
        _print_session(session, tags)
        if not no_events:
            events = await storage.get_latest_events(session_id, limit=limit)
            if events:
                console.print(f"\n[bold]recent {len(events)} event(s)[/bold]")
                for ev in events:
                    _print_event_summary(ev)
            else:
                console.print("\n[dim](no events recorded)[/dim]")
    finally:
        await storage.close()


async def _tail_session(session_id: str, limit: int, follow: bool) -> None:
    from coding_agents.cli._utils import _get_storage, console
    storage = _get_storage()
    await storage.initialize()
    try:
        session = await storage.get_session(session_id)
        if session is None:
            console.print(f"[red]Session not found: {session_id}[/red]")
            raise typer.Exit(code=1)
        events = await storage.get_latest_events(session_id, limit=limit)
        if events:
            console.print(f"[bold]{len(events)} most recent event(s)[/bold]")
            for ev in events:
                _print_event_summary(ev)
        else:
            console.print("[dim](no events recorded)[/dim]")
        if follow:
            last_seq = events[-1].seq if events else 0
            while not session.status.is_terminal:
                await asyncio.sleep(1.0)
                new_events = await storage.get_events(
                    session_id, after_seq=last_seq, limit=limit
                )
                for ev in new_events:
                    _print_event_summary(ev)
                    last_seq = ev.seq
                session = await storage.get_session(session_id)
                if session is None:
                    break
    finally:
        await storage.close()


async def _list_sessions(
    agent: Optional[str],
    status: Optional[str],
    tag: Optional[str],
    limit: int,
    short_id: bool = False,
) -> None:
    from coding_agents.cli._utils import _get_storage, console
    storage = _get_storage()
    await storage.initialize()
    try:
        tags = [tag] if tag else None
        sessions = await storage.list_sessions(agent=agent, status=status, tags=tags, limit=limit)
        if not sessions:
            console.print("[dim]No sessions found.[/dim]")
            return
        # v0.2.13: default to full 36-char UUID so users can copy-paste
        # into `status` / `tail` / `kill` without first running `tail`
        # to find the missing 28 characters. `--short-id` opts back
        # into the compact view for users who prefer it.
        def _format_id(sid: str) -> str:
            return sid[:8] if short_id else sid

        # v0.2.13: render the table on a 160-char-wide console so the full
        # 36-char UUID fits on a single line, copy-pasteable into status/tail/
        # kill. The default 80-char terminal wraps the UUID across multiple
        # lines which made the CEO's copy-paste workflow impossible. We use
        # a temporary Console (captures to a StringIO) and then echo its
        # output via the global `console` (which respects the user's tty
        # for color/style).
        _buf = io.StringIO()
        _wide = Console(file=_buf, width=160, force_terminal=False)
        table = Table(title="Sessions")
        table.add_column("ID", style="cyan", no_wrap=True)  # full UUID; column expands
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
                _format_id(s.id),
                s.agent.value,
                s.status.value,
                started,
                dur,
                prompt_short,
            )
        # v0.2.13: Render with no terminal width constraint so the full
        # UUID fits on one line. We capture to a string buffer, then
        # write the raw bytes to stdout so the wide table is preserved
        # (the global `console` would otherwise re-wrap at its detected
        # terminal width).
        _wide.print(table)
        sys.stdout.write(_buf.getvalue())
        sys.stdout.flush()
    finally:
        await storage.close()


async def _search_events(query: str, agent: Optional[str], limit: int) -> None:
    from coding_agents.cli._utils import _get_storage, console
    storage = _get_storage()
    await storage.initialize()
    try:
        events = await storage.search_events(query, agent=agent, limit=limit)
        if not events:
            console.print("[dim]No matching events found.[/dim]")
            return
        for ev in events:
            # v0.2.13: print each event line on its own line, prefixed
            # with the full 36-char UUID. We use ``console.print`` so
            # the search output respects the user's terminal width
            # (the [cyan] tag is a no-op for the UUID's 36 chars on
            # wide terminals and folds on narrow ones).
            console.print(
                f"[cyan]{ev.session_id}[/cyan] [{ev.channel}] {ev.data.strip()}"
            )
    finally:
        await storage.close()
