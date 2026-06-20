"""Agent adapters."""

from coding_agents.agents.base import BaseAgent
from coding_agents.agents.claude import ClaudeAgent
from coding_agents.agents.codex import CodexAgent
from coding_agents.agents.factory import get_agent

__all__ = ["BaseAgent", "ClaudeAgent", "CodexAgent", "get_agent"]
