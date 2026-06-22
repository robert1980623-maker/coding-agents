"""``resume`` command - restart a session from its last known sequence number.

Wires the core logic in :mod:`coding_agents.resume` to the CLI. The
core ``resume_session()`` function creates a *new* session (linked to
the original via metadata) and re-runs the agent with ``--resume``
arguments so the agent can continue from its last checkpoint.

The CLI:
- validates ``session_id`` format
- pre-checks ``can_resume()`` and prints a friendly error if the
  session is not resumable
- streams the new session's events to the console in real-time by
  polling SQLite for new events
- prints a final summary (new session id, event count, resumed from,
  final status) on completion
- handles ``KeyboardInterrupt`` gracefully: the new session is left
  in storage so the user can ``tail`` it later
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from typing import Any, Optional

import typer

from coding_agents.cli._utils import _get_storage, _run_async, console
from coding_agents.models import SessionStatus
from coding_agents.resume import (
    ResumeNotSupportedError,
    can_resume,
    get_resume_info,
    resume_session,
)

# session_id must be a short, URL-safe string. Allows UUIDs (default),
# custom slugs like "my-session", and test ids like "s1". The pattern
# matches what the rest of the CLI assumes (1-100 chars of
# alphanumerics, dashes, underscores).
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

# Polling interval (seconds) for streaming events from SQLite.
# Small enough for "real-time" feel, large enough to not hammer the DB.
_POLL_INTERVAL_S = 0.05

# Suppress these event types in non-verbose mode (heartbeat-style noise).
_QUIET_EVENT_TYPES = frozenset({"heartbeat", "watch"})


def register(app: typer.Typer) -> None:
    """Register the resume command on the given Typer app."""
    app.command()(resume)


# ---------------------------------------------------------------------------
# Typer command
# ---------------------------------------------------------------------------


def resume(
    session_id: str = typer.Argument(..., help="Session ID to resume."),
    workdir: str = typer.Option(
        ".",
        "--workdir", "-w",
        help="Working directory for the resumed session. Recorded on the "
             "new session's metadata (note: the agent subprocess itself "
             "currently runs in the executor's default workdir; this "
             "flag is reserved for forward compatibility).",
    ),
    new_session_id: Optional[str] = typer.Option(
        None,
        "--new-session-id",
        help="Explicit ID for the new (resumed) session. If not provided, "
             "a UUID4 is generated. Useful for scripting and tests.",
    ),
    db: Optional[str] = typer.Option(
        None,
        "--db",
        help="Path to the sessions SQLite database. "
             "When omitted, the CODING_AGENTS_DB env var is used, falling "
             "back to ~/.coding-agents/data.db (the project default).",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose/--no-verbose",
        help="Verbose output: print heartbeat/watch events and extra diagnostics.",
    ),
) -> None:
    """Resume a previously interrupted coding-agent session.

    A session is resumable when:
    - It exists in storage.
    - Its status is COMPLETED, KILLED, or TIMEOUT.
    - It has at least one event recorded.
    - Its exit_code is 0 (graceful completion) or None.

    Exit codes:
      0  Resume completed successfully.
      1  Session exists but is not resumable (e.g. FAILED with non-zero
         exit, or status RUNNING). See the printed error for details.
      2  Session not found in storage.
    130  Resume interrupted by user (SIGINT); the new session is left in
         storage so it can be inspected with ``tail`` later.
    """
    # 1. Validate session_id format
    if not _SESSION_ID_RE.match(session_id):
        console.print(
            f"[red]Invalid session_id format: {session_id!r}[/red]\n"
            "  Expected: 1-100 chars of [A-Za-z0-9_-] "
            "(UUIDs, slugs, or short test ids)."
        )
        raise typer.Exit(code=1)
    if new_session_id is not None and not _SESSION_ID_RE.match(new_session_id):
        console.print(
            f"[red]Invalid --new-session-id format: {new_session_id!r}[/red]\n"
            "  Expected: 1-100 chars of [A-Za-z0-9_-]."
        )
        raise typer.Exit(code=1)

    _run_async(_resume_impl(session_id, workdir, new_session_id, db, verbose))


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


async def _resume_impl(
    session_id: str,
    workdir: str,
    new_session_id: Optional[str],
    db: Optional[str],
    verbose: bool,
) -> None:
    # Honor --db by setting CODING_AGENTS_DB before _get_storage() reads it.
    # We expand ~ so the user can write --db ~/path/to.db in their shell.
    _set_db_env(db)

    storage = _get_storage()
    await storage.initialize()
    try:
        # --- Pre-check: session must exist -----------------------------
        original = await storage.get_session(session_id)
        if original is None:
            console.print(f"[red]Session {session_id} not found[/red]")
            raise typer.Exit(code=2)

        # --- Pre-check: session must be resumable ----------------------
        if not await can_resume(session_id, storage):
            await _print_resume_failure(session_id, original, storage)
            raise typer.Exit(code=1)

        # --- Pre-check: must have resume info -------------------------
        resume_info = await get_resume_info(session_id, storage)
        if resume_info is None:
            console.print(
                f"[red]No resume info for session {session_id} "
                "(session has no events).[/red]"
            )
            raise typer.Exit(code=1)

        # Pre-allocate the new session id so we can poll events on it.
        if new_session_id is None:
            new_session_id = str(uuid.uuid4())

        # Sanity check: if user gave an explicit id, it must not already
        # exist in storage (resume_session() would happily reuse it but
        # that's almost certainly a user mistake).
        if await storage.get_session(new_session_id) is not None:
            console.print(
                f"[red]--new-session-id {new_session_id!r} already exists in "
                "storage. Choose a different id or omit the flag.[/red]"
            )
            raise typer.Exit(code=1)

        console.print(f"[bold]Resuming session {session_id}[/bold]")
        console.print(f"  Last event seq: {resume_info.last_seq}")
        console.print(f"  New session_id: {new_session_id}")
        console.print(f"  Workdir:        {workdir}")
        console.print()

        # Run resume_session in a background task; poll SQLite for new
        # events on the new session id and print them as they arrive.
        events: list[Any] = []
        last_seq = 0
        resume_task = asyncio.create_task(
            resume_session(session_id, storage, new_session_id=new_session_id)
        )

        try:
            while not resume_task.done():
                events, last_seq = await _drain_new_events(
                    storage, new_session_id, last_seq, events, verbose
                )
                # No new events this tick: yield to the loop.
                await asyncio.sleep(_POLL_INTERVAL_S)
        except KeyboardInterrupt:
            console.print(
                f"\n[yellow]Resume interrupted, new session "
                f"{new_session_id} kept in storage[/yellow]"
            )
            resume_task.cancel()
            try:
                await resume_task
            except (asyncio.CancelledError, ResumeNotSupportedError, Exception):
                # Best-effort: cancellation may surface any of these;
                # we don't care about the inner exception, only about
                # leaving the new session in storage for inspection.
                pass
            raise typer.Exit(code=130)

        # Drain any events that landed in the final tick.
        events, last_seq = await _drain_new_events(
            storage, new_session_id, last_seq, events, verbose
        )

        # Surface ResumeNotSupportedError if the core raised it.
        try:
            await resume_task
        except ResumeNotSupportedError as e:
            console.print(f"[red]Resume failed: {e}[/red]")
            raise typer.Exit(code=1)

        # Best-effort: record workdir on the new session's metadata so
        # the user can see what was requested. We don't modify the
        # core resume_session() (per spec), so the agent subprocess
        # itself still runs in its default workdir.
        try:
            await storage.update_session(
                new_session_id, workdir=workdir
            )
        except Exception:  # pragma: no cover - best-effort
            pass

        # --- Print summary --------------------------------------------
        final_session = await storage.get_session(new_session_id)
        final_status = (
            final_session.status.value if final_session else "unknown"
        )
        console.print()
        console.print("[bold green]Resume complete[/bold green]")
        console.print(f"  New session_id:  {new_session_id}")
        console.print(
            f"  Resumed from:    {session_id} (last_seq={resume_info.last_seq})"
        )
        console.print(f"  Event count:     {len(events)}")
        console.print(f"  Final status:    {final_status}")
    finally:
        await storage.close()


async def _drain_new_events(
    storage: Any,
    new_session_id: str,
    last_seq: int,
    events: list[Any],
    verbose: bool,
) -> tuple[list[Any], int]:
    """Read events newer than ``last_seq`` from storage and print them."""
    new_events = await storage.get_events(
        new_session_id, after_seq=last_seq
    )
    for ev in new_events:
        _print_event_line(ev, verbose=verbose)
        events.append(ev)
        last_seq = ev.seq
    return events, last_seq


def _set_db_env(db: Optional[str]) -> None:
    """Honor the --db flag by exporting CODING_AGENTS_DB (expanded).

    If the user did not pass --db, leave the existing env var alone so
    tests (and other tools that set CODING_AGENTS_DB) keep working.
    """
    if db is None:
        return
    expanded = os.path.expanduser(db)
    os.environ["CODING_AGENTS_DB"] = expanded


async def _print_resume_failure(
    session_id: str,
    session: Any,
    storage: Any,
) -> None:
    """Print a friendly error explaining why a session can't be resumed.

    Shows the session's status, exit code, and event count (from
    :func:`get_resume_info`), plus a status-specific hint.
    """
    info = await get_resume_info(session_id, storage)
    event_count_desc = (
        f"up to seq {info.last_seq}" if info is not None else "none recorded"
    )

    console.print(f"[red]Cannot resume session {session_id}[/red]")
    console.print(f"  Status:    {session.status.value}")
    console.print(f"  Exit code: {session.exit_code}")
    console.print(f"  Events:    {event_count_desc}")

    status = session.status
    if status == SessionStatus.FAILED and (
        session.exit_code is None or session.exit_code != 0
    ):
        console.print(
            "[yellow]Hint: FAILED sessions (non-zero exit) cannot be resumed "
            "because the agent's internal state is unreliable. Retry with a "
            "new session instead.[/yellow]"
        )
    elif status == SessionStatus.FAILED and session.exit_code == 0:
        console.print(
            "[yellow]Hint: FAILED sessions cannot be resumed regardless of "
            "exit code. Retry with a new session instead.[/yellow]"
        )
    elif status == SessionStatus.RUNNING:
        console.print(
            "[yellow]Hint: A RUNNING session cannot be resumed. Wait for it "
            "to complete (or kill it) before resuming.[/yellow]"
        )
    elif status == SessionStatus.ORPHANED:
        console.print(
            "[yellow]Hint: An ORPHANED session's wrapper died but the "
            "subprocess is still running. Kill the subprocess first, "
            "then retry.[/yellow]"
        )
    elif status == SessionStatus.PENDING:
        console.print(
            "[yellow]Hint: A PENDING session hasn't started yet - there is "
            "nothing to resume from.[/yellow]"
        )


def _print_event_line(ev: Any, verbose: bool = False) -> None:
    """Print a one-line summary of an event."""
    type_short = ev.type.value if hasattr(ev.type, "value") else str(ev.type)
    if not verbose and type_short in _QUIET_EVENT_TYPES:
        return
    data = ev.data if isinstance(ev.data, str) else str(ev.data)
    if len(data) > 200:
        data = data[:200] + "..."
    console.print(f"  [dim]seq={ev.seq}[/dim] [{type_short}] {data}")
