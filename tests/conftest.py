"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest

from coding_agents.models import AgentType, ExecutionConfig, Session, SessionStatus
from coding_agents.storage.sqlite import SQLiteStorage


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
async def storage(tmp_db_path: Path) -> AsyncIterator[SQLiteStorage]:
    store = SQLiteStorage(tmp_db_path)
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def sample_session() -> Session:
    return Session(
        agent=AgentType.CLAUDE,
        prompt="refactor function",
        workdir="/tmp/project",
    )


@pytest.fixture
def default_config() -> ExecutionConfig:
    return ExecutionConfig()
