"""``tag`` command (add / remove a tag on a session)."""

from __future__ import annotations

import typer


def register(app: typer.Typer) -> None:
    """Register the tag command on the given Typer app."""
    app.command()(tag)


def tag(
    session_id: str = typer.Argument(..., help="Session ID"),
    tag_name: str = typer.Argument(..., help="Tag to add/remove"),
    remove: bool = typer.Option(False, "-r", "--remove", help="Remove the tag instead of adding"),
) -> None:
    """Manage tags on a session."""
    from coding_agents.cli._utils import _run_async
    _run_async(_manage_tag(session_id, tag_name, remove))


async def _manage_tag(session_id: str, tag_name: str, remove: bool) -> None:
    from coding_agents.cli._utils import _get_storage, console
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
