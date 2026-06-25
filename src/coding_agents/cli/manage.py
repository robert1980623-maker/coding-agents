"""``kill``, ``recover`` and ``gc`` commands."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import typer

from coding_agents.models import SessionStatus


def register(app: typer.Typer) -> None:
    """Register the manage-family commands on the given Typer app."""
    app.command()(kill)
    app.command()(recover)
    app.command(name="gc")(gc)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def kill(
    session_id: str = typer.Argument(..., help="Session ID to kill"),
) -> None:
    """Terminate a running session."""
    from coding_agents.cli._utils import _run_async
    _run_async(_kill_session(session_id))


def recover(
    timeout: int = typer.Option(300, help="Heartbeat timeout in seconds"),
) -> None:
    """Recover orphaned sessions."""
    from coding_agents.cli._utils import _run_async
    _run_async(_recover_sessions(timeout))


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
    from coding_agents.cli._utils import _run_async
    _run_async(
        _gc_sessions(
            older_than_days,
            failed_after_days,
            keep_result_only,
            vacuum,
            dry_run,
        )
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _kill_session(session_id: str) -> None:
    from coding_agents.cli._utils import _get_storage, console
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


async def _recover_sessions(timeout: int) -> None:
    from coding_agents.cli._utils import _get_storage, console
    storage = _get_storage()
    await storage.initialize()
    try:
        count = await storage.recover_orphaned_sessions(timeout_seconds=timeout)
        console.print(f"[green]Marked {count} orphaned session(s)[/green]")
    finally:
        await storage.close()


async def _gc_sessions(
    older_than_days: int,
    failed_after_days: int,
    keep_result_only: bool,
    vacuum: bool,
    dry_run: bool,
) -> None:
    """Implementation of `gc`."""
    from coding_agents.cli._utils import _get_storage, console

    storage = _get_storage()
    await storage.initialize()
    try:
        sessions = await storage.list_sessions()
        now = datetime.now(timezone.utc)
        completed_cutoff = now - timedelta(days=older_than_days)
        failed_cutoff = now - timedelta(days=failed_after_days)
        orphan_cutoff = now - timedelta(hours=24)
        pending_cutoff = now - timedelta(seconds=120)  # 2 minutes

        to_drop_sids: list[str] = []
        to_prune_sids: list[str] = []
        to_orphan_sids: list[str] = []
        to_fail_pending_sids: list[str] = []
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
            elif s.status == SessionStatus.PENDING:
                # Fail: stuck in pending for > 2 minutes (runner likely crashed)
                if s.created_at and s.created_at < pending_cutoff:
                    to_fail_pending_sids.append(s.id)

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

        if to_fail_pending_sids:
            verb3 = "would fail pending" if dry_run else "failing pending"
            console.print(f"[bold]{verb3} {len(to_fail_pending_sids)} session(s)[/bold]")
            for sid in to_fail_pending_sids:
                if not dry_run:
                    await storage.update_session(
                        sid,
                        status=SessionStatus.FAILED,
                        finished_at=now,
                        metadata={"error": "pending timeout: runner likely crashed before starting"},
                    )
                else:
                    console.print(f"  - {sid}")

        if keep_result_only and to_prune_sids and not dry_run:
            pruned_total = 0
            for sid in to_prune_sids:
                pruned_total += await storage.prune_events_keep_result(sid)
            console.print(f"[bold]pruned {pruned_total} intermediate event(s)[/bold]")

        if not dry_run and vacuum and (to_drop_sids or to_prune_sids):
            console.print("[dim]running VACUUM...[/dim]")
            await storage.vacuum()

        if not (to_drop_sids or to_orphan_sids or to_prune_sids or to_fail_pending_sids):
            console.print("[green]nothing to gc[/green]")
    finally:
        await storage.close()
