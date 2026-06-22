"""``watch`` command — monitor session status changes until terminal state."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import typer


def register(app: typer.Typer) -> None:
    """Register the watch command on the given Typer app."""
    app.command()(watch)


def watch(
    session_id: str = typer.Argument(..., help="Session ID to watch"),
    interval: int = typer.Option(
        300, "--interval", "-i",
        help="Polling interval in seconds (default: 300 = 5 minutes).",
    ),
    timeout: int = typer.Option(
        3600, "--timeout", "-t",
        help="Maximum time to watch in seconds (default: 3600 = 1 hour). "
             "Exits with code 1 if timeout exceeded.",
    ),
) -> None:
    """Watch a session and print status changes until it reaches a terminal state.

    Polls the session status at the specified interval and prints each change
    in the format: [YYYY-MM-DD HH:MM:SS] Status: old → new

    Exits with code 0 when the session reaches a terminal state (completed,
    failed, killed, timeout, orphaned). Exits with code 1 if the --timeout
    is exceeded before reaching a terminal state.
    """
    from coding_agents.cli._utils import _run_async, _get_storage, console

    async def _watch_impl() -> None:
        storage = _get_storage()
        await storage.initialize()
        try:
            # Check session exists
            session = await storage.get_session(session_id)
            if session is None:
                console.print(f"[red]Session not found: {session_id}[/red]")
                raise typer.Exit(code=1)

            start_time = time.monotonic()
            last_status = None

            while True:
                session = await storage.get_session(session_id)
                if session is None:
                    console.print(f"[red]Session disappeared: {session_id}[/red]")
                    raise typer.Exit(code=1)

                current_status = session.status.value

                # Print status change
                if last_status is None:
                    # First check — print initial status
                    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    console.print(f"[dim]{ts}[/dim] Status: {current_status}")
                    last_status = current_status
                elif current_status != last_status:
                    # Status changed — print transition
                    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    console.print(f"[dim]{ts}[/dim] Status: {last_status} → {current_status}")
                    last_status = current_status

                # Check terminal state
                if session.status.is_terminal:
                    return

                # Check timeout
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    console.print(
                        f"[red]Timeout after {int(elapsed)}s "
                        f"(current status: {current_status})[/red]"
                    )
                    raise typer.Exit(code=1)

                # Sleep before next poll
                await asyncio.sleep(interval)
        finally:
            await storage.close()

    _run_async(_watch_impl())
