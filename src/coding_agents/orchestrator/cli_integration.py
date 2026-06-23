"""CLI integration helpers for the orchestrator.

This module bridges the orchestrator with the agent/CLI layer.  It does
**not** modify ``cli.py`` — instead it provides helper functions that
can be used to build CLI commands for flow execution, and a monkey-patch
mechanism for adding resume support to existing agent adapters.

Design notes (v0.2.0-S3 / T3.2):
- ``enable_resume_support`` wraps an agent's ``build_command`` to append
  ``--resume`` arguments when resume state is present.
- ``build_resume_command`` constructs a resume command from a base command
  and resume metadata.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from coding_agents.agents.base import BaseAgent
from coding_agents.models import AgentType, ExecutionConfig


def enable_resume_support(agent: BaseAgent) -> BaseAgent:
    """Add resume capability to an agent via monkey-patching.

    Wraps ``agent.build_command`` so that when ``config.metadata`` contains
    a ``"resume"`` key with ``{"session_id": ..., "last_seq": ...}``, the
    returned command is augmented with ``--resume`` arguments.

    Returns the same agent instance (now patched).
    """
    original: Callable[..., list[str]] = agent.build_command

    def patched(
        prompt: str,
        config: ExecutionConfig,
    ) -> list[str]:
        cmd = original(prompt, config)
        resume_info: Optional[dict[str, Any]] = getattr(
            config, "metadata", {}
        ).get("resume") if hasattr(config, "metadata") else None

        if resume_info and "session_id" in resume_info:
            # Get session metadata if available in config
            session_metadata = config.metadata.get("session_metadata", {}) if hasattr(config, "metadata") else {}
            cmd = build_resume_command(
                cmd,
                session_id=resume_info["session_id"],
                last_seq=resume_info.get("last_seq", 0),
                agent_type=(
                    AgentType(config.metadata.get("agent_type", "claude"))
                    if hasattr(config, "metadata")
                    and config.metadata.get("agent_type")
                    else None
                ),
                session_metadata=session_metadata,
            )
        return cmd

    agent.build_command = patched  # type: ignore[method-assign]
    return agent


def build_resume_command(
    base_command: list[str],
    session_id: str,
    last_seq: int,
    agent_type: Optional[AgentType] = None,
    session_metadata: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Build a command with resume arguments appended.

    Different agent CLIs may use different resume flags:
    - Claude Code: ``--resume <session-id>``
    - Codex: ``--resume-from <seq>``
    - Generic fallback: ``--resume <session_id> --from-seq <last_seq>``

    If session_metadata is provided and contains "native_session_id", that
    will be used instead of the passed session_id for Claude Code and Codex.
    """
    # Use native session ID if available in metadata for backward compatibility
    effective_session_id = session_id
    if session_metadata and "native_session_id" in session_metadata:
        effective_session_id = session_metadata["native_session_id"]

    if agent_type == AgentType.CLAUDE:
        # For Claude Code: use native session ID when available, otherwise fallback
        cmd = list(base_command)
        cmd.extend(["--resume", effective_session_id])
        return cmd
    elif agent_type == AgentType.CODEX:
        # For Codex: if native session ID exists, use "codex exec resume <thread_id>"
        # instead of "codex exec --resume-from <seq>"
        if session_metadata and "native_session_id" in session_metadata:
            # Replace the entire command with "codex exec resume <thread_id>"
            native_thread_id = session_metadata["native_session_id"]
            return ["codex", "exec", "resume", native_thread_id]
        else:
            # Fallback to original behavior with base command
            cmd = list(base_command)
            cmd.extend(["--resume-from", str(last_seq)])
            return cmd
    else:
        # Generic fallback: use effective session ID with from-seq
        cmd = list(base_command)
        cmd.extend(["--resume", effective_session_id, "--from-seq", str(last_seq)])
        return cmd


def build_flow_command(
    flow_id: str,
    task_ids: list[str],
    parallel: bool = True,
) -> list[str]:
    """Build a CLI command for executing a task flow.

    This is a helper for future CLI integration — the current implementation
    does not expose a ``flow`` sub-command.
    """
    cmd = ["coding-agents", "flow", flow_id]
    if not parallel:
        cmd.append("--sequential")
    cmd.extend(task_ids)
    return cmd
