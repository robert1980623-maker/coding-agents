"""DAG-based multi-agent orchestration.

Defines :class:`Task` (a single node in the DAG), :class:`TaskFlow` (the DAG
container with topological-sort), and :class:`TaskResult` (outcome of one task).

Design notes (v0.2.0-S3 / P1-2):
- Kahn's algorithm for topological sort + cycle detection.
- ``execution_layers`` returns a list-of-lists where each inner list contains
  tasks that can run concurrently (all dependencies satisfied).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Union

from coding_agents.models import AgentType, Event, ExecutionConfig


class CyclicDependencyError(Exception):
    """Raised when the task graph contains a cycle."""


@dataclass
class Task:
    """A single task node in the orchestration DAG."""

    id: str
    agent: Union[AgentType, str]
    prompt: str
    depends_on: list[str] = field(default_factory=list)
    timeout: Optional[float] = None  # seconds; None = no timeout
    config: Optional[ExecutionConfig] = None

    def resolve_agent_type(self) -> AgentType:
        """Resolve agent to ``AgentType`` enum."""
        if isinstance(self.agent, str):
            return AgentType(self.agent)
        return self.agent


@dataclass
class TaskResult:
    """Outcome of a single task execution."""

    task_id: str
    status: str = "pending"  # pending | running | completed | failed | timeout | skipped
    session_id: Optional[str] = None
    error: Optional[str] = None
    events: list[Event] = field(default_factory=list)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    duration_ms: Optional[int] = None


class TaskFlow:
    """DAG of tasks with dependency management.

    >>> flow = TaskFlow()
    >>> flow.add_task(Task("a", "claude", "step 1"))
    >>> flow.add_task(Task("b", "claude", "step 2", depends_on=["a"]))
    >>> flow.topological_sort()
    ['a', 'b']
    """

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    @property
    def tasks(self) -> dict[str, Task]:
        return dict(self._tasks)

    def add_task(self, task: Task) -> None:
        """Add a task to the flow.

        Raises ``ValueError`` on duplicate IDs.  Dependency existence is
        validated lazily in :meth:`topological_sort` so that cyclic graphs
        can still be constructed for testing.
        """
        if task.id in self._tasks:
            raise ValueError(f"Duplicate task id: {task.id}")
        self._tasks[task.id] = task

    def get_task(self, task_id: str) -> Task:
        """Return task by ID or raise ``KeyError``."""
        return self._tasks[task_id]

    def topological_sort(self) -> list[str]:
        """Return task IDs in topological order (Kahn's algorithm).

        Raises ``CyclicDependencyError`` if the graph has a cycle.
        Raises ``ValueError`` if a task references an unknown dependency.
        """
        if not self._tasks:
            return []

        # Validate deps first
        for tid, task in self._tasks.items():
            for dep in task.depends_on:
                if dep not in self._tasks:
                    raise ValueError(
                        f"Task {tid} depends on unknown task: {dep}"
                    )

        # Build adjacency and in-degree
        in_degree: dict[str, int] = {tid: 0 for tid in self._tasks}
        adjacency: dict[str, list[str]] = {tid: [] for tid in self._tasks}

        for tid, task in self._tasks.items():
            for dep in task.depends_on:
                adjacency[dep].append(tid)
                in_degree[tid] += 1

        # Seed queue with zero-in-degree tasks
        queue: deque[str] = deque(
            tid for tid, deg in in_degree.items() if deg == 0
        )

        result: list[str] = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for neighbor in adjacency[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(result) != len(self._tasks):
            raise CyclicDependencyError("Task graph contains a cycle")

        return result

    def execution_layers(self) -> list[list[str]]:
        """Group tasks into layers for parallel execution.

        Tasks within the same layer have no intra-layer dependencies and
        can be launched concurrently.  The first layer contains tasks with
        no dependencies; each subsequent layer only contains tasks whose
        dependencies are all in earlier layers.
        """
        if not self._tasks:
            return []

        # Validate DAG (raises on cycle)
        self.topological_sort()

        remaining = set(self._tasks.keys())
        layers: list[list[str]] = []

        while remaining:
            # A task is ready when all its deps are in completed layers
            completed = set().union(*(set(layer) for layer in layers))
            layer = [
                tid
                for tid in remaining
                if all(dep in completed for dep in self._tasks[tid].depends_on)
            ]
            if not layer:
                raise CyclicDependencyError("Task graph contains a cycle")
            layers.append(layer)
            remaining -= set(layer)

        return layers
