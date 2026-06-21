"""Base agent interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from coding_agents.models import ExecutionConfig


class BaseAgent(ABC):
    """Abstract base class for agent adapters."""

    @abstractmethod
    def build_command(self, prompt: str, config: ExecutionConfig) -> list[str]:
        """Build the command-line invocation for the agent."""
        ...

    @abstractmethod
    def parse_output(self, line: str) -> Optional[dict[str, Any]]:
        """Parse a single line of output, returning structured data or None."""
        ...

    @abstractmethod
    def extract_cost(self, output: str) -> Optional[float]:
        """Extract cost information from accumulated output."""
        ...

    def env_overrides(self) -> dict[str, str]:
        """Return environment variable overrides for the agent subprocess.

        Subclasses override this to fix environment issues (e.g. HOME
        redirection by host processes like Hermes profiles). Default: no
        overrides.
        """
        return {}
