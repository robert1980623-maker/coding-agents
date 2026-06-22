"""``resume`` command — restart a session from its last known sequence number.

Placeholder: the underlying logic lives in :mod:`coding_agents.resume`.
Wire it up as ``coding-agents resume <session_id>`` once the resume UX
is approved by PM.
"""

from __future__ import annotations

import typer


def register(app: typer.Typer) -> None:  # pragma: no cover - placeholder
    """Register the resume command on the given Typer app.

    Currently a no-op placeholder so the module slot exists in the CLI
    package layout.
    """
    return None
