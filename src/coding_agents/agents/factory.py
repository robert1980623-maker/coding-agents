"""Agent factory — return the right adapter for a given AgentType."""

from __future__ import annotations

from coding_agents.agents.base import BaseAgent
from coding_agents.agents.claude import ClaudeAgent
from coding_agents.agents.codex import CodexAgent
from coding_agents.models import AgentType


def get_agent(agent_type: AgentType | str) -> BaseAgent:
    """Return the appropriate agent adapter for the given type."""
    if isinstance(agent_type, str):
        agent_type = AgentType(agent_type)

    if agent_type == AgentType.CLAUDE:
        return ClaudeAgent()
    elif agent_type == AgentType.CODEX:
        return CodexAgent()
    else:
        raise ValueError(f"Unsupported agent type: {agent_type}")
