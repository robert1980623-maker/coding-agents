"""Tests for the DAG-based multi-agent orchestrator."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coding_agents.models import (
    AgentType,
    Event,
    EventType,
    ExecutionConfig,
    Session,
    SessionStatus,
)
from coding_agents.orchestrator import (
    CyclicDependencyError,
    Task,
    TaskFlow,
    TaskResult,
    run_flow,
)
from coding_agents.storage.sqlite import SQLiteStorage


# ---------------------------------------------------------------------------
# Task dataclass
# ---------------------------------------------------------------------------

class TestTask:
    def test_defaults(self):
        t = Task(id="a", agent="claude", prompt="do something")
        assert t.id == "a"
        assert t.depends_on == []
        assert t.timeout is None
        assert t.config is None

    def test_resolve_agent_type_str(self):
        t = Task(id="a", agent="claude", prompt="x")
        assert t.resolve_agent_type() == AgentType.CLAUDE

    def test_resolve_agent_type_enum(self):
        t = Task(id="a", agent=AgentType.CODEX, prompt="x")
        assert t.resolve_agent_type() == AgentType.CODEX

    def test_resolve_agent_type_invalid(self):
        t = Task(id="a", agent="unknown_agent", prompt="x")
        with pytest.raises(ValueError):
            t.resolve_agent_type()


# ---------------------------------------------------------------------------
# TaskFlow — DAG construction
# ---------------------------------------------------------------------------

class TestTaskFlow:
    def test_add_single_task(self):
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="x"))
        assert "a" in flow.tasks
        assert flow.get_task("a").prompt == "x"

    def test_add_duplicate_raises(self):
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="x"))
        with pytest.raises(ValueError, match="Duplicate"):
            flow.add_task(Task(id="a", agent="claude", prompt="y"))

    def test_add_unknown_dep_validated_in_sort(self):
        flow = TaskFlow()
        # add_task no longer eagerly validates deps (allows cyclic test setup)
        flow.add_task(
            Task(id="b", agent="claude", prompt="y", depends_on=["a"])
        )
        # Validation happens in topological_sort
        with pytest.raises(ValueError, match="unknown task"):
            flow.topological_sort()

    def test_get_task_missing_raises(self):
        flow = TaskFlow()
        with pytest.raises(KeyError):
            flow.get_task("nonexistent")


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------

class TestTopologicalSort:
    def test_empty(self):
        flow = TaskFlow()
        assert flow.topological_sort() == []

    def test_single(self):
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="x"))
        assert flow.topological_sort() == ["a"]

    def test_linear_chain(self):
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="1"))
        flow.add_task(Task(id="b", agent="claude", prompt="2", depends_on=["a"]))
        flow.add_task(Task(id="c", agent="claude", prompt="3", depends_on=["b"]))
        assert flow.topological_sort() == ["a", "b", "c"]

    def test_diamond(self):
        """A → B, A → C, B → D, C → D"""
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="1"))
        flow.add_task(Task(id="b", agent="claude", prompt="2", depends_on=["a"]))
        flow.add_task(Task(id="c", agent="claude", prompt="3", depends_on=["a"]))
        flow.add_task(Task(id="d", agent="claude", prompt="4", depends_on=["b", "c"]))
        order = flow.topological_sort()
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_cycle_detection(self):
        """A → B → C → A (cycle)"""
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="1", depends_on=["c"]))
        flow.add_task(Task(id="b", agent="claude", prompt="2", depends_on=["a"]))
        flow.add_task(Task(id="c", agent="claude", prompt="3", depends_on=["b"]))
        with pytest.raises(CyclicDependencyError):
            flow.topological_sort()

    def test_independent_tasks(self):
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="1"))
        flow.add_task(Task(id="b", agent="claude", prompt="2"))
        flow.add_task(Task(id="c", agent="claude", prompt="3"))
        order = flow.topological_sort()
        assert set(order) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# Execution layers
# ---------------------------------------------------------------------------

class TestExecutionLayers:
    def test_empty(self):
        flow = TaskFlow()
        assert flow.execution_layers() == []

    def test_all_independent(self):
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="1"))
        flow.add_task(Task(id="b", agent="claude", prompt="2"))
        flow.add_task(Task(id="c", agent="claude", prompt="3"))
        layers = flow.execution_layers()
        assert len(layers) == 1
        assert set(layers[0]) == {"a", "b", "c"}

    def test_linear_layers(self):
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="1"))
        flow.add_task(Task(id="b", agent="claude", prompt="2", depends_on=["a"]))
        flow.add_task(Task(id="c", agent="claude", prompt="3", depends_on=["b"]))
        layers = flow.execution_layers()
        assert layers == [["a"], ["b"], ["c"]]

    def test_parallel_then_join(self):
        """A and B parallel, then C depends on both."""
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="1"))
        flow.add_task(Task(id="b", agent="claude", prompt="2"))
        flow.add_task(Task(id="c", agent="claude", prompt="3", depends_on=["a", "b"]))
        layers = flow.execution_layers()
        assert len(layers) == 2
        assert set(layers[0]) == {"a", "b"}
        assert layers[1] == ["c"]

    def test_cycle_raises(self):
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="1", depends_on=["b"]))
        flow.add_task(Task(id="b", agent="claude", prompt="2", depends_on=["a"]))
        with pytest.raises(CyclicDependencyError):
            flow.execution_layers()


# ---------------------------------------------------------------------------
# run_flow — mock helpers
# ---------------------------------------------------------------------------

class MockExecutor:
    """A fake StreamExecutor that yields predefined events.

    By default, yields a minimal start → stdout → result event sequence for
    every session.  Use ``set_events`` to override for a specific session_id,
    or ``set_block`` to block forever (for timeout tests).

    Does NOT call ``store.update_session`` — the runner determines task
    status from the RESULT event's exit_code when session status is still
    PENDING.  This avoids SQLite concurrent-write issues in parallel tests.
    """

    def __init__(
        self,
        store: Any,
        config: Any,
    ) -> None:
        self.store = store
        self.config = config
        # Populated by set_events()
        self._events_map: dict[str, list[Event]] = {}
        self._block_events: dict[str, asyncio.Event] = {}
        self._status_map: dict[str, SessionStatus] = {}
        self._execution_order: list[str] = []
        self._execution_times: dict[str, float] = {}

    def set_events(
        self,
        session_id: str,
        events: list[Event],
        status: SessionStatus = SessionStatus.COMPLETED,
    ) -> None:
        self._events_map[session_id] = events
        self._status_map[session_id] = status

    def set_block(self, session_id: str, event: asyncio.Event) -> None:
        self._block_events[session_id] = event

    async def execute(
        self,
        session_id: str,
        command: list[str],
        workdir: str,
        env: Optional[dict[str, str]] = None,
    ) -> AsyncIterator[Event]:
        self._execution_order.append(session_id)
        self._execution_times[session_id] = time.monotonic()

        # Block if configured (for timeout tests)
        if session_id in self._block_events:
            await self._block_events[session_id].wait()

        # Yield per-session events, or defaults
        events = self._events_map.get(session_id)
        if events is None:
            events = _make_events(session_id)

        for event in events:
            yield event


def _make_mock_agent() -> MagicMock:
    agent = MagicMock()
    agent.build_command.return_value = ["mock-agent", "-p"]
    return agent


def _make_events(
    session_id: str,
    text: str = "hello",
) -> list[Event]:
    """Create a minimal start → stdout → result event sequence."""
    return [
        Event(
            session_id=session_id,
            channel="system",
            seq=1,
            type=EventType.SESSION_START,
            data=json.dumps({"session_id": session_id}),
        ),
        Event(
            session_id=session_id,
            channel="stdout",
            seq=2,
            type=EventType.STDOUT,
            data=text,
        ),
        Event(
            session_id=session_id,
            channel="system",
            seq=3,
            type=EventType.RESULT,
            data=json.dumps({"exit_code": 0}),
        ),
    ]


# ---------------------------------------------------------------------------
# run_flow — integration tests
# ---------------------------------------------------------------------------

class TestRunFlow:
    async def test_simple_linear(
        self, storage: SQLiteStorage, tmp_path: Path
    ):
        """A → B → C, all succeed."""
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="step 1"))
        flow.add_task(
            Task(id="b", agent="claude", prompt="step 2", depends_on=["a"])
        )
        flow.add_task(
            Task(id="c", agent="claude", prompt="step 3", depends_on=["b"])
        )

        mock_agent = _make_mock_agent()

        with (
            patch(
                "coding_agents.orchestrator.runner.get_agent",
                return_value=mock_agent,
            ),
            patch(
                "coding_agents.orchestrator.runner.StreamExecutor",
                MockExecutor,
            ),
        ):
            results = await run_flow(flow, storage)

        assert set(results.keys()) == {"a", "b", "c"}
        for tid in ("a", "b", "c"):
            assert results[tid].status == "completed"
            assert results[tid].session_id is not None
            assert results[tid].duration_ms is not None
            assert results[tid].duration_ms >= 0

        # Verify execution order: a before b before c
        sessions = {}
        for tid in ("a", "b", "c"):
            s = await storage.get_session(results[tid].session_id)
            assert s is not None
            sessions[tid] = s

    async def test_parallel_execution(
        self, storage: SQLiteStorage, tmp_path: Path
    ):
        """A and B have no deps → should start at roughly the same time."""
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="parallel 1"))
        flow.add_task(Task(id="b", agent="claude", prompt="parallel 2"))

        mock_agent = _make_mock_agent()

        # Track start times
        start_times: dict[str, float] = {}

        class TimedMockExecutor(MockExecutor):
            async def execute(self, session_id, command, workdir, env=None):
                start_times[session_id] = time.monotonic()
                async for event in super().execute(
                    session_id, command, workdir, env
                ):
                    yield event

        with (
            patch(
                "coding_agents.orchestrator.runner.get_agent",
                return_value=mock_agent,
            ),
            patch(
                "coding_agents.orchestrator.runner.StreamExecutor",
                TimedMockExecutor,
            ),
        ):
            results = await run_flow(flow, storage)

        assert results["a"].status == "completed"
        assert results["b"].status == "completed"

        # Both should have started within 50ms of each other
        times = list(start_times.values())
        assert abs(times[0] - times[1]) < 0.05

    async def test_timeout_propagation(
        self, storage: SQLiteStorage, tmp_path: Path
    ):
        """Task A times out → dependent B should be skipped."""
        flow = TaskFlow()
        flow.add_task(
            Task(id="a", agent="claude", prompt="slow", timeout=0.2)
        )
        flow.add_task(
            Task(id="b", agent="claude", prompt="after a", depends_on=["a"])
        )

        mock_agent = _make_mock_agent()

        class BlockingExecutor(MockExecutor):
            """Blocks forever on every session to trigger timeout."""

            async def execute(
                self,
                session_id: str,
                command: list[str],
                workdir: str,
                env: Optional[dict[str, str]] = None,
            ) -> AsyncIterator[Event]:
                self._execution_order.append(session_id)
                self._execution_times[session_id] = time.monotonic()
                # Block forever → wait_for will cancel after timeout
                await asyncio.Event().wait()
                yield  # unreachable, but makes this an async generator

        with (
            patch(
                "coding_agents.orchestrator.runner.get_agent",
                return_value=mock_agent,
            ),
            patch(
                "coding_agents.orchestrator.runner.StreamExecutor",
                BlockingExecutor,
            ),
        ):
            results = await run_flow(flow, storage)

        assert results["a"].status == "timeout"
        assert results["b"].status == "skipped"
        assert "Dependency failed" in (results["b"].error or "")

    async def test_dependency_failure_skips_child(
        self, storage: SQLiteStorage, tmp_path: Path
    ):
        """A fails → B (depends on A) should be skipped."""
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="will fail"))
        flow.add_task(
            Task(id="b", agent="claude", prompt="after a", depends_on=["a"])
        )

        mock_agent = _make_mock_agent()

        class FailExecutor(MockExecutor):
            async def execute(self, session_id, command, workdir, env=None):
                self._execution_order.append(session_id)
                self._execution_times[session_id] = time.monotonic()
                yield Event(
                    session_id=session_id,
                    channel="system",
                    seq=1,
                    type=EventType.SESSION_START,
                    data=json.dumps({"session_id": session_id}),
                )
                # Yield a RESULT event with exit_code=1 to signal failure
                yield Event(
                    session_id=session_id,
                    channel="system",
                    seq=2,
                    type=EventType.RESULT,
                    data=json.dumps({"exit_code": 1}),
                )

        with (
            patch(
                "coding_agents.orchestrator.runner.get_agent",
                return_value=mock_agent,
            ),
            patch(
                "coding_agents.orchestrator.runner.StreamExecutor",
                FailExecutor,
            ),
        ):
            results = await run_flow(flow, storage)

        assert results["a"].status == "failed"
        assert results["b"].status == "skipped"

    async def test_complex_dag_topological_order(
        self, storage: SQLiteStorage, tmp_path: Path
    ):
        """Diamond DAG: A → {B, C} → D, verify all deps met."""
        flow = TaskFlow()
        flow.add_task(Task(id="a", agent="claude", prompt="1"))
        flow.add_task(
            Task(id="b", agent="claude", prompt="2", depends_on=["a"])
        )
        flow.add_task(
            Task(id="c", agent="claude", prompt="3", depends_on=["a"])
        )
        flow.add_task(
            Task(id="d", agent="claude", prompt="4", depends_on=["b", "c"])
        )

        mock_agent = _make_mock_agent()

        with (
            patch(
                "coding_agents.orchestrator.runner.get_agent",
                return_value=mock_agent,
            ),
            patch(
                "coding_agents.orchestrator.runner.StreamExecutor",
                MockExecutor,
            ),
        ):
            results = await run_flow(flow, storage)

        assert set(results.keys()) == {"a", "b", "c", "d"}
        for tid in ("a", "b", "c", "d"):
            assert results[tid].status == "completed", (
                f"Task {tid} has status {results[tid].status}: {results[tid].error}"
            )

        # Verify the flow created sessions for all tasks
        for tid in ("a", "b", "c", "d"):
            sid = results[tid].session_id
            assert sid is not None
            session = await storage.get_session(sid)
            assert session is not None

    async def test_result_aggregation(
        self, storage: SQLiteStorage, tmp_path: Path
    ):
        """Verify result dict has correct keys and structure."""
        flow = TaskFlow()
        flow.add_task(Task(id="x", agent="claude", prompt="test"))

        mock_agent = _make_mock_agent()

        with (
            patch(
                "coding_agents.orchestrator.runner.get_agent",
                return_value=mock_agent,
            ),
            patch(
                "coding_agents.orchestrator.runner.StreamExecutor",
                MockExecutor,
            ),
        ):
            results = await run_flow(flow, storage)

        assert "x" in results
        r = results["x"]
        assert isinstance(r, TaskResult)
        assert r.task_id == "x"
        assert r.status == "completed"
        assert r.session_id is not None
        assert len(r.events) == 3  # start + stdout + result
        assert r.duration_ms is not None
