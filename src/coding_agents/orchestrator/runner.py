"""Flow runner — executes a :class:`TaskFlow` with dependency-aware parallelism.

Design notes (v0.2.0-S3 / P1-2):
- Each layer from :meth:`TaskFlow.execution_layers` is dispatched concurrently
  via ``asyncio.gather``.
- Per-task timeout is enforced via ``asyncio.wait_for``; on timeout the
  underlying :class:`StreamExecutor` ``finally`` block terminates the
  subprocess.
- Dependency failure propagation: if any dependency of a task did not
  complete, the task is marked ``skipped`` without spawning a subprocess.
"""

from __future__ import annotations

import asyncio
import json
import time

import structlog

from coding_agents.agents.factory import get_agent
from coding_agents.executor import StreamExecutor
from coding_agents.models import (
    AgentType,
    Event,
    EventType,
    ExecutionConfig,
    Session,
    SessionStatus,
)
from coding_agents.orchestrator.dag import Task, TaskFlow, TaskResult
from coding_agents.storage.base import StorageBackend

logger = structlog.get_logger(__name__)


async def run_flow(
    flow: TaskFlow,
    storage: StorageBackend,
    registry: object | None = None,
) -> dict[str, TaskResult]:
    """Execute a :class:`TaskFlow`, respecting DAG ordering.

    Parameters
    ----------
    flow:
        The task DAG to execute.
    storage:
        Storage backend for session/event persistence.
    registry:
        Unused — reserved for future :class:`SessionRegistry` integration.

    Returns
    -------
    dict mapping task IDs to their :class:`TaskResult`.
    """
    # Validate DAG (raises CyclicDependencyError on cycle)
    flow.topological_sort()
    layers = flow.execution_layers()

    results: dict[str, TaskResult] = {}
    flow_started_at = time.monotonic()

    for layer in layers:
        layer_tasks = [flow.get_task(tid) for tid in layer]

        # Separate runnable vs. skipped (dependency failed)
        runnable: list[Task] = []
        for task in layer_tasks:
            dep_failed = any(
                results.get(dep, TaskResult(task_id=dep)).status
                not in ("completed",)
                for dep in task.depends_on
            )
            if dep_failed:
                failed_deps = [
                    dep
                    for dep in task.depends_on
                    if results.get(dep, TaskResult(task_id=dep)).status
                    != "completed"
                ]
                results[task.id] = TaskResult(
                    task_id=task.id,
                    status="skipped",
                    error=f"Dependency failed: {failed_deps}",
                )
            else:
                runnable.append(task)

        # Launch runnable tasks concurrently
        task_coros = [_run_single_task(task, storage, results) for task in runnable]
        layer_results_list = await asyncio.gather(*task_coros, return_exceptions=True)

        for task, result in zip(runnable, layer_results_list):
            if isinstance(result, Exception):
                if isinstance(result, asyncio.TimeoutError):
                    results[task.id] = TaskResult(
                        task_id=task.id,
                        status="timeout",
                        error=f"Task timed out after {task.timeout}s",
                    )
                    # Ensure session status reflects the timeout
                    if results[task.id].session_id:
                        await storage.update_session(
                            results[task.id].session_id,  # type: ignore[arg-type]
                            status=SessionStatus.TIMEOUT,
                        )
                else:
                    results[task.id] = TaskResult(
                        task_id=task.id,
                        status="failed",
                        error=str(result),
                    )
            elif isinstance(result, TaskResult):
                results[task.id] = result

    flow_duration = time.monotonic() - flow_started_at
    logger.info(
        "flow_completed",
        total_tasks=len(flow.tasks),
        duration_s=round(flow_duration, 3),
    )

    return results


async def _run_single_task(
    task: Task,
    storage: StorageBackend,
    _prior_results: dict[str, TaskResult] | None = None,
) -> TaskResult:
    """Execute a single task and return its :class:`TaskResult`."""
    result = TaskResult(task_id=task.id, status="running")
    result.started_at = time.monotonic()

    # Build command via agent adapter
    agent_type_resolved: AgentType = task.resolve_agent_type()
    agent = get_agent(agent_type_resolved)
    config = task.config or ExecutionConfig()
    command = agent.build_command(task.prompt, config)

    # Create session
    session = Session(
        agent=agent_type_resolved,
        prompt=task.prompt,
        metadata={"flow_task_id": task.id},
    )
    await storage.create_session(session)
    result.session_id = session.id

    # Execute — wrap async generator iteration in a coroutine so
    # asyncio.wait_for can enforce the per-task timeout.
    executor = StreamExecutor(store=storage, config=config)

    async def _collect() -> list[Event]:
        collected: list[Event] = []
        async for ev in executor.execute(session.id, command, "."):
            collected.append(ev)
        return collected

    try:
        if task.timeout is not None:
            events = await asyncio.wait_for(_collect(), timeout=task.timeout)
        else:
            events = await _collect()
        result.events = events
    except asyncio.TimeoutError:
        result.status = "timeout"
        result.error = f"Task timed out after {task.timeout}s"
        result.finished_at = time.monotonic()
        if result.started_at is not None:
            result.duration_ms = int(
                (result.finished_at - result.started_at) * 1000
            )
        # Mark session as TIMEOUT since executor might have only set FAILED
        await storage.update_session(
            session.id,
            status=SessionStatus.TIMEOUT,
        )
        return result
    except Exception as e:
        result.status = "failed"
        result.error = str(e)
        result.finished_at = time.monotonic()
        if result.started_at is not None:
            result.duration_ms = int(
                (result.finished_at - result.started_at) * 1000
            )
        return result

    # Determine final status from session + events.
    # The session status is the primary signal, but we also check the
    # last RESULT event's exit_code as a fallback (useful when the
    # executor couldn't persist the session status, e.g. in tests).
    final_session = await storage.get_session(session.id)
    result_exit_code = _extract_result_exit_code(events)

    if final_session is not None:
        if final_session.status == SessionStatus.COMPLETED:
            result.status = "completed"
        elif final_session.status == SessionStatus.TIMEOUT:
            result.status = "timeout"
        elif (
            final_session.status == SessionStatus.PENDING
            and result_exit_code is not None
        ):
            # Executor completed but couldn't update session status
            result.status = (
                "completed" if result_exit_code == 0 else "failed"
            )
            if result_exit_code != 0:
                result.error = f"Exit code: {result_exit_code}"
        else:
            result.status = "failed"
            result.error = f"Session ended with status: {final_session.status.value}"
    elif result_exit_code is not None:
        result.status = "completed" if result_exit_code == 0 else "failed"
        if result_exit_code != 0:
            result.error = f"Exit code: {result_exit_code}"
    else:
        result.status = "failed"
        result.error = "Session not found after execution"

    result.finished_at = time.monotonic()
    if result.started_at is not None:
        result.duration_ms = int(
            (result.finished_at - result.started_at) * 1000
        )

    return result


def _extract_result_exit_code(events: list[Event]) -> int | None:
    """Extract exit_code from the last RESULT event, or None if absent."""
    for event in reversed(events):
        if event.type == EventType.RESULT:
            try:
                data = json.loads(event.data)
                return int(data.get("exit_code"))  # type: ignore[arg-type]
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
    return None
