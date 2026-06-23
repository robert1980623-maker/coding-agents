"""Claude Code agent adapter."""

from __future__ import annotations

import json
import os
import pwd
from typing import Any, Optional

import structlog

from coding_agents.agents.base import BaseAgent
from coding_agents.models import ExecutionConfig

logger = structlog.get_logger(__name__)


def _get_real_home() -> str:
    """Resolve the real user home directory.

    When the parent process redirects HOME (e.g. Hermes profiles set
    HOME=~/.hermes/profiles/<name>/home), Claude Code fails to find its
    config at ~/.claude/. We resolve the real home via pwd database
    which is immune to HOME overrides.
    """
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, AttributeError):
        return os.path.expanduser("~")


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

        if config.max_budget_usd is not None:
            cmd.extend(["--max-budget-usd", str(config.max_budget_usd)])

        if config.model:
            cmd.extend(["--model", config.model])

        # Note: We intentionally do NOT inject coding-agents skills via
        # --append-system-prompt. Claude Code has its own skill system
        # (~/.claude/skills/, /skill command) and will pick the best one
        # for the task on its own. Forcing our skill list would compete
        # with Claude's native discovery and could mislead the agent.

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

    def env_overrides(self) -> dict[str, str]:
        """Ensure HOME points to the real user home.

        Claude Code resolves ``~/.claude/`` config/skills via HOME.
        When the parent process (e.g. Hermes with profiles) redirects HOME
        to a profile directory, Claude Code cannot find its config.
        """
        overrides = {}
        real_home = _get_real_home()
        current_home = os.environ.get("HOME", "")
        if current_home != real_home:
            logger.info(
                "claude-home-override",
                original_home=current_home,
                real_home=real_home,
            )
            overrides["HOME"] = real_home

        return overrides

    def env_deletions(self) -> list[str]:
        """Strip DashScope environment variables.

        These cause Claude Code to route through DashScope proxy instead of
        native Anthropic API, breaking authentication.
        """
        dashscope_vars = ['ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_MODEL']
        to_delete = []
        for var in dashscope_vars:
            if os.environ.get(var, '').lower().find('dashscope') != -1:
                to_delete.append(var)
        return to_delete
