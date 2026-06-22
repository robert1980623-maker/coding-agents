"""Shared helpers for the coding-agents CLI sub-modules.

Values defined here are re-exported from :mod:`coding_agents.cli` so
``coding_agents.cli.DEFAULT_DB`` etc. remain valid import paths for
callers (and tests) that used them before the CLI was split into a
package.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import structlog
from rich.console import Console

from coding_agents.storage.sqlite import SQLiteStorage

console = Console()
logger = structlog.get_logger(__name__)

# Module-level default storage path. ``_get_storage`` reads this at call
# time so tests can override via env or by patching this attribute.
_DEFAULT_DB = "~/.coding-agents/data.db"
DEFAULT_DB = os.environ.get("CODING_AGENTS_DB", _DEFAULT_DB)


def _get_storage() -> SQLiteStorage:
    """Resolve DB path at call time so tests can override via env or attribute.

    Reads ``DEFAULT_DB`` through the ``coding_agents.cli`` package so tests
    that patch ``coding_agents.cli.DEFAULT_DB`` are honored.
    """
    import coding_agents.cli as _cli_pkg

    return SQLiteStorage(os.environ.get("CODING_AGENTS_DB", _cli_pkg.DEFAULT_DB))


def _run_async(coro: Any) -> Any:
    """Run an async function in a sync CLI context.

    v0.2.13: asyncio.run() does not propagate BaseException like
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


def _format_duration(seconds: float) -> str:
    """Format seconds as a human-readable duration: 5m, 4m21s, 2h30m, 45s."""
    if seconds < 0:
        return "-"
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s" if secs else f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h{mins:02d}m" if mins else f"{hours}h"


import re as _re_module  # noqa: E402  (kept at module scope for _parse_duration)


def _parse_duration(s: str) -> int:
    """Parse a duration string into seconds.

    Accepts: ``30m``, ``1h``, ``1h30m``, ``90s``, or raw integer seconds.
    """
    s = s.strip().lower()
    total = 0
    # Hours
    h_match = _re_module.search(r"(\d+)h", s)
    if h_match:
        total += int(h_match.group(1)) * 3600
    # Minutes
    m_match = _re_module.search(r"(\d+)m", s)
    if m_match:
        total += int(m_match.group(1)) * 60
    # Seconds (only if explicit 's' suffix)
    s_match = _re_module.search(r"(\d+)s", s)
    if s_match:
        total += int(s_match.group(1))
    # Bare number = seconds
    if not (h_match or m_match or s_match):
        try:
            total = int(s)
        except ValueError:
            total = 1800  # fallback: 30m
    return total
