"""Codex CLI agent adapter."""

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
    HOME=~/.hermes/profiles/<name>/home), Codex fails to find its config
    at ~/.codex/config.toml. We resolve the real home via pwd database
    which is immune to HOME overrides.
    """
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, AttributeError):
        return os.path.expanduser("~")


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

        # Codex CLI does not support a per-session cost/budget cap.
        # It bills via OpenAI ChatGPT subscription (not per-token), so
        # there is no `--max-budget-usd` flag. Warn the user when they
        # explicitly set --budget so they know it is a no-op.
        # (Default budget is None — no warning when user did not opt in.)
        if config.max_budget_usd is not None:
            logger.warning(
                "codex-agent: --budget is ignored; codex CLI has no "
                "--max-budget-usd flag (subscription billing). "
                "Use --agent claude if you need a hard cost cap.",
            )

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

    def env_overrides(self) -> dict[str, str]:
        """Ensure HOME points to the real user home.

        Codex resolves ``~/.codex/config.toml`` and other paths via HOME.
        When the parent process (e.g. Hermes with profiles) redirects HOME
        to a profile directory, Codex cannot find its config and fails
        with "No such file or directory".
        """
        real_home = _get_real_home()
        current_home = os.environ.get("HOME", "")
        if current_home != real_home:
            logger.info(
                "codex-home-override",
                original_home=current_home,
                real_home=real_home,
            )
            return {"HOME": real_home}
        return {}
