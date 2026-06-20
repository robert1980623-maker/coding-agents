"""Claude Code agent adapter."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from coding_agents.agents.base import BaseAgent
from coding_agents.models import ExecutionConfig

logger = logging.getLogger(__name__)


class ClaudeAgent(BaseAgent):
    """Adapter for Claude Code CLI."""

    def build_command(self, prompt: str, config: ExecutionConfig) -> list[str]:
        cmd = [
            "claude",
            "-p",  # print mode
            "--verbose",
            "--output-format",
            "stream-json",
            "--permission-mode",
            "bypassPermissions",
        ]

        if config.max_budget_usd:
            cmd.extend(["--max-budget-usd", str(config.max_budget_usd)])

        if config.model:
            cmd.extend(["--model", config.model])

        cmd.append(prompt)
        return cmd

    def parse_output(self, line: str) -> Optional[dict[str, Any]]:
        """Parse Claude Code stream-json output.

        Extracts cost/tokens from the 'result' event type.
        """
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None

        if event.get("type") == "result":
            usage = event.get("usage", {}) or {}
            return {
                "cost_usd": event.get("total_cost_usd"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read_tokens": usage.get("cache_read_tokens"),
                "cache_write_tokens": usage.get("cache_write_tokens"),
                "model": event.get("model"),
            }
        return None

    def extract_cost(self, output: str) -> Optional[float]:
        """Extract cost from the last 'result' line in output."""
        try:
            for line in reversed(output.split("\n")):
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if event.get("type") == "result":
                    cost = event.get("total_cost_usd")
                    return float(cost) if cost is not None else None
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        return None
