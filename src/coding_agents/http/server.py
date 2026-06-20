"""FastAPI application for the coding-agents HTTP API."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from coding_agents.http.metrics_endpoint import router as metrics_router
from coding_agents.http.routes.actions import router as actions_router
from coding_agents.http.routes.events import router as events_router
from coding_agents.http.routes.sessions import router as sessions_router
from coding_agents.http.routes.tags import router as tags_router
from coding_agents.storage.sqlite import SQLiteStorage

logger = structlog.get_logger(__name__)


def create_app(db_path: str = "~/.coding-agents/data.db") -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Configured FastAPI application.
    """
    # Create storage instance
    storage = SQLiteStorage(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Manage application lifecycle (startup/shutdown)."""
        # Startup
        await storage.initialize()
        logger.info("storage_initialized", db_path=db_path)
        yield
        # Shutdown
        await storage.close()
        logger.info("storage_closed")

    app = FastAPI(
        title="Coding Agents API",
        description="HTTP API for managing coding agent sessions",
        version="0.2.0",
        lifespan=lifespan,
    )

    # Dependency override for storage injection
    async def get_storage() -> SQLiteStorage:
        """Dependency that provides the storage instance."""
        return storage

    app.dependency_overrides[SQLiteStorage] = get_storage

    # Include routers
    app.include_router(sessions_router)
    app.include_router(events_router)
    app.include_router(actions_router)
    app.include_router(tags_router)
    app.include_router(metrics_router)

    # Health check endpoint
    @app.get("/health")
    async def health() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "healthy"}

    # Global exception handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Handle uncaught exceptions."""
        logger.error("unhandled_exception", error=str(exc), exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    return app
