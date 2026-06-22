"""``server`` command — HTTP API for the coding-agents runtime.

Placeholder: the HTTP server implementation lives in
:mod:`coding_agents.http`; wire its uvicorn launcher up as
``coding-agents server`` here when the HTTP surface is promoted from
experimental to stable.
"""

from __future__ import annotations

import typer


def register(app: typer.Typer) -> None:  # pragma: no cover - placeholder
    """Register the server command on the given Typer app.

    Currently a no-op placeholder so the module slot exists in the CLI
    package layout. The HTTP server can be started directly via
    ``uvicorn coding_agents.http:app`` until this command is wired up.
    """
    return None
