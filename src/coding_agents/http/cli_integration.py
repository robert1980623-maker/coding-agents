"""CLI integration for the HTTP server.

Since we cannot modify cli.py directly (file isolation rules), this module
provides a standalone script to run the HTTP server.

Usage:
    uv run python -m coding_agents.http.cli_integration --port 8080 --host 127.0.0.1

Or create a wrapper script in your project:
    #!/usr/bin/env python
    from coding_agents.http.cli_integration import main
    if __name__ == "__main__":
        main()
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from coding_agents.auth import ensure_token, get_token_path
from coding_agents.logging_config import setup_logging


def main() -> None:
    """Run the HTTP server."""
    parser = argparse.ArgumentParser(
        description="Run the coding-agents HTTP server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1 for security)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to bind to",
    )
    parser.add_argument(
        "--db",
        default="~/.coding-agents/data.db",
        help="Path to the SQLite database",
    )
    parser.add_argument(
        "--auth-token-file",
        default="~/.coding-agents-token",
        help="Path to the auth token file",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level",
    )
    parser.add_argument(
        "--log-json",
        action="store_true",
        default=True,
        help="Enable JSON logging",
    )
    parser.add_argument(
        "--no-log-json",
        action="store_false",
        dest="log_json",
        help="Disable JSON logging (use plain text)",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(level=args.log_level, json_output=args.log_json)

    # Ensure auth token exists
    token = ensure_token(args.auth_token_file)
    token_path = get_token_path(args.auth_token_file)
    print(f"Auth token: {token_path}")
    print(f"Token (first 8 chars): {token[:8]}...")
    print()

    # Import server here to ensure logging is configured first
    from coding_agents.http.server import create_app

    app = create_app(db_path=args.db)

    print(f"Starting server on http://{args.host}:{args.port}")
    print(f"Database: {args.db}")
    print()
    print("Endpoints:")
    print("  POST   /sessions                    - Create session")
    print("  GET    /sessions                    - List sessions")
    print("  GET    /sessions/{id}               - Get session")
    print("  GET    /sessions/{id}/events        - Get events (REST)")
    print("  GET    /sessions/{id}/events/stream - Stream events (SSE)")
    print("  POST   /sessions/{id}/kill          - Kill session")
    print("  POST   /sessions/{id}/tags          - Add tag")
    print("  DELETE /sessions/{id}/tags/{tag}    - Remove tag")
    print("  GET    /sessions/{id}/tags          - List tags")
    print("  POST   /recover                     - Recover orphaned sessions")
    print("  GET    /metrics                     - Prometheus metrics")
    print("  GET    /health                      - Health check")
    print()

    # Run the server
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
    )


if __name__ == "__main__":
    main()
