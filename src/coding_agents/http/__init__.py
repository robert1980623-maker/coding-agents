"""HTTP API for the coding-agents runtime.

This module provides a FastAPI-based HTTP server for managing coding agent sessions.

Main components:
- `server.create_app()`: Factory function to create the FastAPI app
- `auth.verify_token()`: Bearer token authentication dependency
- `sse.format_event_as_sse()`: SSE event formatting

Usage:
    # Run the server
    uv run python -m coding_agents.http.cli_integration --port 8080

    # Or import and customize
    from coding_agents.http.server import create_app
    app = create_app(db_path="my.db")
"""

from coding_agents.http.server import create_app

__all__ = ["create_app"]
