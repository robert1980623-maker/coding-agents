"""CLI for the coding agent runtime."""

from __future__ import annotations

import asyncio
import json
import signal
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
import os
_DEFAULT_DB = "~/.coding-agents/data.db"
DEFAULT_DB = os.environ.get("CODING_AGENTS_DB", _DEFAULT_DB)


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
    """Resolve DB path at call time so tests can override via env or attribute."""
    # Honor env override at runtime (tests can monkeypatch.setenv).
    return SQLiteStorage(os.environ.get("CODING_AGENTS_DB", DEFAULT_DB))


def _run_async(coro: Any) -> Any:
    """Run an async function in a sync CLI context.

    v0.2.12: asyncio.run() does not propagate BaseException like
    SystemExit to the outer code — it logs "Task exception was never
    retrieved" and returns normally. We catch SystemExit here and
    re-raise so the process exits with the correct POSIX code
    (128 + signal).
    """
    try:
        return asyncio.run(coro)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise SystemExit(130)


@app.command()
def run(
    agent: str = typer.Argument(..., help="Agent type: claude or codex"),
    prompt: str = typer.Argument(..., help="Prompt to send to the agent"),
    workdir: str = typer.Option(".", help="Working directory for the agent"),
    model: Optional[str] = typer.Option(None, help="Model override"),
    budget: Optional[float] = typer.Option(None, help="Max budget in USD"),
    output_mode: str = typer.Option("standard", help="Output mode: standard or passthrough"),
    verbose: bool = typer.Option(False, help="Verbose output"),
) -> None:
    """[DEPRECATED] Run a coding agent session. Use 'dispatch' instead."""
    import warnings
    warnings.warn(
        "'coding-agents run' is deprecated; use 'coding-agents dispatch' instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    _run_async(_run_session(agent, prompt, workdir, model, budget, output_mode, verbose))


@app.command(name="dispatch")
def dispatch(
    agent: str = typer.Argument(..., help="Agent type: claude or codex"),
    prompt: str = typer.Argument(..., help="Prompt to send to the agent"),
    workdir: Optional[str] = typer.Option(
        None,
        "--workdir", "-w",
        help="Working directory for the agent subprocess (default: current dir). "
             "This is where the agent reads AGENTS.md / CLAUDE.md / .claude/skills/ "
             "from, so always set this to your project root.",
    ),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model override"),
    budget: Optional[float] = typer.Option(None, "--budget", "-b", help="Max budget in USD"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
) -> None:
    """Dispatch a coding agent session in the current project.

    This is the recommended way to run an agent: the subprocess starts in
    ``--workdir`` (default: current directory), so it sees your project's
    AGENTS.md / CLAUDE.md / .claude/skills/ natively — no prompt injection
    needed.

    Output is bounded: dispatch only prints the final result event
    (one JSON line, < 1KB) plus the session id. All intermediate
    stdout/stderr goes to SQLite. Use ``coding-agents status <id>`` or
    ``coding-agents tail <id>`` to inspect the full stream — this
    keeps dispatch output safely below the OpenClaw exec 1MB buffer.

    Example:
        coding-agents dispatch claude "fix the auth bug" --workdir ~/project
    """
    # Default workdir to current dir (not '.') so it resolves to the absolute path
    # the agent subprocess actually sees.
    effective_workdir = workdir or "."
    _run_async(
        _run_session(agent, prompt, effective_workdir, model, budget, "standard", verbose)
    )


async def _run_session(
    agent_name: str,
    prompt: str,
    workdir: str,
    model: Optional[str],
    budget: Optional[float],
    output_mode: str,
    verbose: bool,
) -> None:
    """Run the agent subprocess and persist events to SQLite.

    Output contract: only the final result event (one line) plus the
    session id are written to stdout/stderr. Everything else is
    persisted to SQLite and retrieved with `status` / `tail`.
    This keeps dispatch output safely below the OpenClaw exec 1MB buffer.

    Signal safety: if the wrapper receives SIGTERM / SIGINT (e.g. OpenClaw
    1MB-buffer SIGKILL cascade, Ctrl-C, orchestrator timeout), the signal
    handler converts it into a ``SystemExit`` so the finally block runs.
    The finally block guarantees the session is moved out of ``running``
    and into ``failed`` (with the signal recorded in metadata) before the
    process actually exits — this is what prevents the "session stuck in
    running until gc/recover turns it into orphaned" bug.
    """
    try:
        agent_type = AgentType(agent_name)
    except ValueError:
        console.print(f"[red]Unknown agent type: {agent_name}[/red]")
        raise typer.Exit(code=1)

    adapter = get_agent(agent_type)
    config = ExecutionConfig(
        output_mode=output_mode,
        model=model,
        max_budget_usd=budget,  # None means "no cap"; agents honor or warn
    )

    command = adapter.build_command(prompt, config)
    if verbose:
        console.print(f"[dim]command: {' '.join(command)}[/dim]")

    storage = _get_storage()
    await storage.initialize()

    session = Session(agent=agent_type, prompt=prompt, workdir=workdir, model=model)
    await storage.create_session(session)
    # Always print the session id early so the caller can poll status
    # / tail even if dispatch is killed by the 1MB buffer.
    console.print(f"session_id={session.id}")

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
    exit_code: Optional[int] = None
    error_text: Optional[str] = None

    # --- Signal handling: convert SIGTERM/SIGINT into a controlled exit ---
    # We track which signal arrived (if any) so the finally block can
    # record it in metadata. Raising SystemExit lets the finally block
    # run and do the cleanup; SIGKILL bypasses everything, but there's
    # nothing any userspace code can do about that.
    _received_signal: dict[str, Any] = {"signal": None}

    def _on_signal(signum: int) -> None:
        _received_signal["signal"] = signum
        # SystemExit is a BaseException that triggers finally blocks.
        # The exit code follows the POSIX convention: 128 + signum.
        # Note: with start_new_session=True (v0.2.12), the wrapper's
        # SIGTERM is not propagated to the subprocess; the catch block
        # below calls executor._terminate_process() to kill the
        # process group and unblock the async for stdout pipe.
        raise SystemExit(128 + signum)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, OSError):
            # Windows doesn't support add_signal_handler; OSError if not main thread.
            pass

    async def _finalize_session(sig: Optional[int]) -> None:
        """Move session out of ``running`` if it is still stuck there.

        Called from the finally block. Idempotent: if the executor loop
        finished normally (status already COMPLETED / FAILED / KILLED)
        this is a no-op unless ``sig`` is set, in which case we enrich
        the metadata with signal info. If a signal or unrecoverable
        exception killed the loop mid-stream we mark it FAILED with
        signal/error metadata.
        """
        try:
            current = await storage.get_session(session.id)
        except Exception:  # pragma: no cover - best-effort; storage broken
            return
        if current is None:
            return
        if current.status != SessionStatus.RUNNING:
            # v0.2.12: enrich metadata with signal info even if executor
            # already finalized the session (it terminates the subprocess
            # and records exit_code=-15, but not the wrapper's signal).
            if sig is not None:
                merged = dict(current.metadata or {})
                merged["signal"] = sig
                try:
                    merged["signal_name"] = signal.Signals(sig).name
                except (ValueError, AttributeError):
                    pass
                merged["error"] = "wrapper terminated"
                try:
                    await storage.update_session(
                        session.id,
                        metadata=merged,
                        # Normalize: signal-terminated sessions are
                        # conventionally exit_code=-1 in our schema.
                        exit_code=-1,
                    )
                except Exception:
                    pass
            return

        metadata: dict[str, Any] = {"error": "wrapper terminated"}
        if sig is not None:
            metadata["signal"] = sig
            try:
                metadata["signal_name"] = signal.Signals(sig).name
            except (ValueError, AttributeError):
                pass
        elif exit_code is not None:
            # Partial result: executor loop ended without a RESULT event
            # being fully processed (shouldn't normally happen, but be safe).
            metadata["partial_exit_code"] = exit_code
        if error_text:
            metadata["partial_error"] = error_text

        try:
            await storage.update_session(
                session.id,
                status=SessionStatus.FAILED,
                finished_at=datetime.now(timezone.utc),
                exit_code=exit_code if exit_code is not None else -1,
                metadata=metadata,
            )
        except Exception:  # pragma: no cover - best-effort
            # Last-ditch: even if the rich update fails, mark terminal.
            try:
                await storage.update_session(
                    session.id,
                    status=SessionStatus.FAILED,
                    finished_at=datetime.now(timezone.utc),
                )
            except Exception:
                pass

    try:
        try:
            async for event in executor.execute(session.id, command, workdir):
                # All events flow to SQLite. We only act on the terminal ones.
                if event.type == EventType.RESULT:
                    data = json.loads(event.data)
                    exit_code = data.get("exit_code")
                elif event.type == EventType.ERROR:
                    error_text = event.data
        except asyncio.CancelledError:
            # v0.2.12: When asyncio's signal handler raises SystemExit,
            # the running coroutine receives CancelledError instead.
            # We translate it back to SystemExit so the finally block
            # can finalize the session with the signal metadata.
            if _received_signal["signal"] is not None:
                await executor._terminate_process()
                raise SystemExit(128 + _received_signal["signal"])
            raise
        # v0.2.12: If CancelledError was swallowed inside the executor's
        # async generator, the loop exits normally but the signal handler
        # already ran. Surface it as SystemExit so the outer finally
        # records the signal in session metadata.
        if _received_signal["signal"] is not None and exit_code is None:
            error_text = error_text or "wrapper terminated"
            raise SystemExit(128 + _received_signal["signal"])
    except Exception as e:
            console.print(f"[red]Execution error: {e}[/red]")
            error_text = str(e)
            # Finalize now (status -> FAILED). The finally block will see
            # it's no longer RUNNING and skip the duplicate update.
            await _finalize_session(None)
    finally:
        # Always: release registry + finalize + close storage.
        try:
            await registry.release(session.id)
        except Exception:  # pragma: no cover
            pass
        # If the except branch above already finalized, this is a no-op.
        await _finalize_session(_received_signal["signal"])
        # Restore default signal handlers (best-effort; loop may be closing).
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, OSError, RuntimeError):
                pass
        try:
            await storage.close()
        except Exception:  # pragma: no cover
            pass

    # Emit a single bounded result line so the caller knows how it ended.
    result_line = {
        "session_id": session.id,
        "exit_code": exit_code,
        "error": error_text,
    }
    sys.stdout.write(json.dumps(result_line, ensure_ascii=False) + "\n")
    sys.stdout.flush()

    if verbose:
        console.print(f"[dim]full event stream: coding-agents tail {session.id}[/dim]")


@app.command()
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
    _run_async(_show_status(session_id, limit, no_events))


async def _show_status(session_id: str, limit: int, no_events: bool) -> None:
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


def _print_event_summary(ev) -> None:
    """One-line summary for an event — bounded so 20 lines fit easily."""
    type_short = ev.type.value if hasattr(ev.type, "value") else str(ev.type)
    data_preview = ev.data if isinstance(ev.data, str) else str(ev.data)
    if len(data_preview) > 200:
        data_preview = data_preview[:200] + "..."
    console.print(f"  [dim]seq={ev.seq}[/dim] [{type_short}] {data_preview}")


@app.command(name="tail")
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
    _run_async(_tail_session(session_id, limit, follow))


async def _tail_session(session_id: str, limit: int, follow: bool) -> None:
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
            from coding_agents.models import SessionStatus
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
        table.add_column("ID", style="cyan", overflow="fold")
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
                s.id,
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
                f"[cyan]{ev.session_id}[/cyan] [{ev.channel}] {ev.data.strip()}"
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


@app.command(name="gc")
def gc(
    older_than_days: int = typer.Option(
        30, "--older-than", "-d",
        help="Drop completed sessions older than N days (default: 30).",
    ),
    failed_after_days: int = typer.Option(
        7, "--failed-after",
        help="Drop failed sessions older than N days (default: 7).",
    ),
    keep_result_only: bool = typer.Option(
        False, "--keep-result-only",
        help="For retained sessions, drop all stdout/stderr events but "
             "keep the result event. Frees disk; loses intermediate output.",
    ),
    vacuum: bool = typer.Option(
        True, "--vacuum/--no-vacuum",
        help="Run VACUUM after deletes to reclaim disk space (default: on).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n",
        help="Report what would be deleted without actually deleting.",
    ),
) -> None:
    """Garbage-collect old / completed sessions to keep SQLite bounded.

    Defaults are conservative: keeps 30 days of completed sessions and
    7 days of failed sessions. Use `--keep-result-only` to further prune
    intermediate events while preserving the final answer.

    For sessions in the running state older than 24h, they are marked
    as orphaned (not deleted) — recover those separately with
    `coding-agents recover`.
    """
    _run_async(
        _gc_sessions(
            older_than_days,
            failed_after_days,
            keep_result_only,
            vacuum,
            dry_run,
        )
    )


async def _gc_sessions(
    older_than_days: int,
    failed_after_days: int,
    keep_result_only: bool,
    vacuum: bool,
    dry_run: bool,
) -> None:
    """Implementation of `gc`."""
    from datetime import timedelta
    from coding_agents.models import EventType, SessionStatus

    storage = _get_storage()
    await storage.initialize()
    try:
        sessions = await storage.list_sessions()
        now = datetime.now(timezone.utc)
        completed_cutoff = now - timedelta(days=older_than_days)
        failed_cutoff = now - timedelta(days=failed_after_days)
        orphan_cutoff = now - timedelta(hours=24)

        to_drop_sids: list[str] = []
        to_prune_sids: list[str] = []
        to_orphan_sids: list[str] = []
        for s in sessions:
            if s.status in (SessionStatus.COMPLETED, SessionStatus.KILLED, SessionStatus.TIMEOUT):
                if s.finished_at and s.finished_at < completed_cutoff:
                    to_drop_sids.append(s.id)
            elif s.status == SessionStatus.FAILED:
                if s.finished_at and s.finished_at < failed_cutoff:
                    to_drop_sids.append(s.id)
            elif s.status == SessionStatus.RUNNING:
                # Orphan: still running but no activity for 24h.
                if s.started_at and s.started_at < orphan_cutoff:
                    to_orphan_sids.append(s.id)

        if keep_result_only:
            to_prune_sids = [s.id for s in sessions if s.id not in to_drop_sids]

        verb = "would drop" if dry_run else "dropping"
        if to_drop_sids:
            console.print(f"[bold]{verb} {len(to_drop_sids)} session(s)[/bold]")
            for sid in to_drop_sids:
                if not dry_run:
                    await storage.delete_session(sid)
                else:
                    console.print(f"  - {sid}")

        if to_orphan_sids:
            verb2 = "would mark orphaned" if dry_run else "marking orphaned"
            console.print(f"[bold]{verb2} {len(to_orphan_sids)} session(s)[/bold]")
            for sid in to_orphan_sids:
                if not dry_run:
                    await storage.update_session(
                        sid,
                        status=SessionStatus.ORPHANED,
                        finished_at=now,
                        metadata={"orphan_reason": "gc: 24h no activity"},
                    )

        if keep_result_only and to_prune_sids and not dry_run:
            pruned_total = 0
            for sid in to_prune_sids:
                pruned_total += await storage.prune_events_keep_result(sid)
            console.print(f"[bold]pruned {pruned_total} intermediate event(s)[/bold]")

        if not dry_run and vacuum and (to_drop_sids or to_prune_sids):
            console.print("[dim]running VACUUM...[/dim]")
            await storage.vacuum()

        if not (to_drop_sids or to_orphan_sids or to_prune_sids):
            console.print("[green]nothing to gc[/green]")
    finally:
        await storage.close()


# Register skill sub-commands
from coding_agents.cli_skill import app as skill_app  # noqa: E402

app.add_typer(skill_app, name="skill")


if __name__ == "__main__":
    app()
