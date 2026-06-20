"""DAG-based multi-agent orchestration.

Public API
----------
- :class:`Task` — a single DAG node
- :class:`TaskFlow` — DAG container with topological sort
- :class:`TaskResult` — outcome of a task execution
- :class:`CyclicDependencyError` — raised on cyclic graphs
- :func:`run_flow` — execute a flow with dependency-aware parallelism
- :func:`enable_resume_support` — monkey-patch agent for resume
- :func:`build_resume_command` — construct ``--resume`` command
"""

from coding_agents.orchestrator.cli_integration import (
    build_resume_command,
    enable_resume_support,
)
from coding_agents.orchestrator.dag import (
    CyclicDependencyError,
    Task,
    TaskFlow,
    TaskResult,
)
from coding_agents.orchestrator.runner import run_flow

__all__ = [
    "CyclicDependencyError",
    "Task",
    "TaskFlow",
    "TaskResult",
    "build_resume_command",
    "enable_resume_support",
    "run_flow",
]
