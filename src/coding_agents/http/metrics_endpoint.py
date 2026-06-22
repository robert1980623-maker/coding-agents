"""Prometheus metrics endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from coding_agents.http.auth import verify_token

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics(token: str = Depends(verify_token)) -> Response:
    """Return Prometheus metrics in text format."""
    metrics_data = generate_latest()
    return Response(
        content=metrics_data,
        media_type=CONTENT_TYPE_LATEST,
    )
