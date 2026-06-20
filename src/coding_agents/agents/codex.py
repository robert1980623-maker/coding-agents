"""Codex CLI agent adapter."""

from __future__ import annotations

import json
from typing import Any, Optional

import structlog

from coding_agents.agents.base import BaseAgent
from coding_agents.models import ExecutionConfig

logger = structlog.get_logger(__name__)


class CodexAgent(BaseAgent):
    """Adapter for OpenAI Codex CLI."""

    def build_command(self, prompt: str, config: ExecutionConfig) -> list[str]:
        cmd = [
            "codex",
            "exec",
            "--json",
            "--full-auto",
        ]

        if config.model:
            cmd.extend(["-m", config.model])

        # Note: We intentionally do NOT inject coding-agents skills into
        # the Codex prompt. Codex has its own skill discovery (AGENTS.md
        # and project conventions) and we should not pollute its context.
        # See ClaudeAgent.build_command for the same reasoning.

        cmd.append(prompt)
        return cmd

    def parse_output(self, line: str) -> Optional[dict[str, Any]]:
        """Parse Codex --json output."""
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None

        if event.get("type") == "turn.completed":
            usage = event.get("usage", {}) or {}
            return {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                # Codex does not provide cost
            }
        return None

    def extract_cost(self, output: str) -> Optional[float]:
        """Codex does not provide cost information."""
        return None
