"""``poll`` command — one-line status overview per active session."""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table


def register(app: typer.Typer) -> None:
    """Register the poll command on the given Typer app."""
    app.command()(poll)


def poll(
    all: bool = typer.Option(
        False, "--all",
        help="Include all sessions, not just active (running/pending).",
    ),
    status: Optional[str] = typer.Option(
        None, "--status", "-s",
        help="Filter by status (running/completed/failed). "
             "Overrides the default active-only filter.",
    ),
    stuck_after: str = typer.Option(
        "30m", "--stuck-after",
        help="Mark sessions stuck if no event for this duration "
             "(default: 30m). Accepts: 30m, 1h, 1h30m, or raw seconds.",
    ),
    format: str = typer.Option(
        "table", "--format", "-f",
        help="Output format: table (default) or json.",
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Max sessions to show."),
) -> None:
    """Poll all active sessions — one-line status overview per session.

    Shows which sessions are running, what they last did (event type),
    how long they've been running, and whether they appear stuck
    (no heartbeat for --stuck-after). Designed for the PM agent to
    check fleet health with a single command instead of N list+tail calls.
    """
    from coding_agents.cli._utils import _run_async
    _run_async(_poll_sessions(all, status, stuck_after, format, limit))


async def _poll_sessions(
    include_all: bool,
    status_filter: Optional[str],
    stuck_after_str: str,
    format_str: str,
    limit: int,
) -> None:
    from coding_agents.cli._utils import (
        _format_duration,
        _get_storage,
        _parse_duration,
        console,
    )
    stuck_after_secs = _parse_duration(stuck_after_str)
    now = datetime.now(timezone.utc)

    storage = _get_storage()
    await storage.initialize()
    try:
        # Resolve status filter.
        # Explicit --status wins; then --all; default = active only.
        if status_filter:
            statuses = [status_filter]
        elif include_all:
            statuses = None  # no filter
        else:
            statuses = ["running", "pending"]

        raw = await storage.poll_summary(statuses=statuses, limit=limit)

        # Build result dicts.
        results: list[dict[str, Any]] = []
        for session, last_event in raw:
            started_ts = (
                session.started_at.timestamp() if session.started_at else None
            )
            running_for_ms: Optional[int] = None
            if started_ts is not None:
                running_for_ms = int(
                    (now.timestamp() - started_ts) * 1000
                )

            # Stuck: only for non-terminal sessions with a known reference time.
            stuck = False
            if not session.status.is_terminal:
                ref_ts = (
                    session.last_heartbeat_at.timestamp()
                    if session.last_heartbeat_at
                    else (
                        session.started_at.timestamp()
                        if session.started_at
                        else (
                            session.created_at.timestamp()
                            if session.created_at
                            else None
                        )
                    )
                )
                if ref_ts is not None:
                    stuck = (now.timestamp() - ref_ts) > stuck_after_secs

            entry: dict[str, Any] = {
                "id": session.id,
                "agent": session.agent.value,
                "status": session.status.value,
                "started_at": (
                    session.started_at.isoformat()
                    if session.started_at else None
                ),
                "running_for_ms": running_for_ms,
                "last_event": last_event,
                "stuck": stuck,
            }
            results.append(entry)

        if format_str == "json":
            summary = {
                "total": len(results),
                "running": sum(
                    1 for r in results if r["status"] == "running"
                ),
                "pending": sum(
                    1 for r in results if r["status"] == "pending"
                ),
                "completed": sum(
                    1 for r in results if r["status"] == "completed"
                ),
                "failed": sum(
                    1 for r in results if r["status"] == "failed"
                ),
                "stuck": sum(1 for r in results if r["stuck"]),
            }
            sys.stdout.write(
                json.dumps(
                    {"sessions": results, "summary": summary},
                    ensure_ascii=False,
                )
                + "\n"
            )
            sys.stdout.flush()
            return

        # Table output.
        if not results:
            console.print("[dim]No matching sessions.[/dim]")
            return

        _buf = io.StringIO()
        _wide = Console(file=_buf, width=160, force_terminal=False)
        table = Table(title="Session Poll")
        table.add_column("Session ID", style="cyan", no_wrap=True)
        table.add_column("Agent", style="magenta")
        table.add_column("Status", style="green")
        table.add_column("Running", justify="right")
        table.add_column("Last Event")
        table.add_column("Stuck?", justify="center")

        for r in results:
            running = (
                _format_duration(r["running_for_ms"] / 1000)
                if r["running_for_ms"] is not None
                else "-"
            )
            last_ev = (
                r["last_event"]["type"] if r["last_event"] else "-"
            )
            if r["status"] in ("running", "pending"):
                stuck_str = (
                    "[red]⚠️ STUCK[/red]" if r["stuck"] else "no"
                )
            else:
                stuck_str = "-"

            table.add_row(
                r["id"],
                r["agent"],
                r["status"],
                running,
                last_ev,
                stuck_str,
            )

        _wide.print(table)
        sys.stdout.write(_buf.getvalue())
        sys.stdout.flush()
    finally:
        await storage.close()
