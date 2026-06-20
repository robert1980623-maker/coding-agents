"""Tests for agent adapters."""

from __future__ import annotations

import json

from coding_agents.agents.claude import ClaudeAgent
from coding_agents.agents.codex import CodexAgent
from coding_agents.agents.factory import get_agent
from coding_agents.models import AgentType, ExecutionConfig


class TestClaudeAgent:
    def test_build_command_basic(self):
        agent = ClaudeAgent()
        config = ExecutionConfig()
        cmd = agent.build_command("test prompt", config)
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--permission-mode" in cmd
        assert "bypassPermissions" in cmd
        assert "test prompt" in cmd

    def test_build_command_with_budget(self):
        agent = ClaudeAgent()
        config = ExecutionConfig(max_budget_usd=5.0)
        cmd = agent.build_command("test", config)
        assert "--max-budget-usd" in cmd
        idx = cmd.index("--max-budget-usd")
        assert cmd[idx + 1] == "5.0"

    def test_build_command_with_model(self):
        agent = ClaudeAgent()
        config = ExecutionConfig(model="claude-sonnet-4-20250514")
        cmd = agent.build_command("test", config)
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-sonnet-4-20250514"

    def test_parse_output_result(self):
        agent = ClaudeAgent()
        line = json.dumps({
            "type": "result",
            "total_cost_usd": 0.15,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 10,
                "cache_write_tokens": 20,
            },
            "model": "claude-sonnet-4-20250514",
        })
        result = agent.parse_output(line)
        assert result is not None
        assert result["cost_usd"] == 0.15
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50
        assert result["model"] == "claude-sonnet-4-20250514"

    def test_parse_output_non_result(self):
        agent = ClaudeAgent()
        line = json.dumps({"type": "assistant", "message": {}})
        result = agent.parse_output(line)
        assert result is None

    def test_parse_output_invalid_json(self):
        agent = ClaudeAgent()
        result = agent.parse_output("not json")
        assert result is None

    def test_extract_cost(self):
        agent = ClaudeAgent()
        output = json.dumps({"type": "assistant"}) + "\n"
        output += json.dumps({"type": "result", "total_cost_usd": 1.23})
        assert agent.extract_cost(output) == 1.23

    def test_extract_cost_no_result(self):
        agent = ClaudeAgent()
        assert agent.extract_cost('{"type": "assistant"}') is None

    def test_extract_cost_invalid(self):
        agent = ClaudeAgent()
        assert agent.extract_cost("not json at all") is None


class TestCodexAgent:
    def test_build_command_basic(self):
        agent = CodexAgent()
        config = ExecutionConfig()
        cmd = agent.build_command("test prompt", config)
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "--json" in cmd
        assert "--full-auto" in cmd
        assert "test prompt" in cmd

    def test_build_command_with_model(self):
        agent = CodexAgent()
        config = ExecutionConfig(model="o4-mini")
        cmd = agent.build_command("test", config)
        assert "-m" in cmd
        idx = cmd.index("-m")
        assert cmd[idx + 1] == "o4-mini"

    def test_parse_output_turn_completed(self):
        agent = CodexAgent()
        line = json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 200, "output_tokens": 100},
        })
        result = agent.parse_output(line)
        assert result is not None
        assert result["input_tokens"] == 200
        assert result["output_tokens"] == 100

    def test_parse_output_non_turn(self):
        agent = CodexAgent()
        line = json.dumps({"type": "other"})
        assert agent.parse_output(line) is None

    def test_extract_cost_always_none(self):
        agent = CodexAgent()
        assert agent.extract_cost("anything") is None


class TestFactory:
    def test_get_claude(self):
        agent = get_agent(AgentType.CLAUDE)
        assert isinstance(agent, ClaudeAgent)

    def test_get_codex(self):
        agent = get_agent(AgentType.CODEX)
        assert isinstance(agent, CodexAgent)

    def test_get_from_string(self):
        agent = get_agent("claude")
        assert isinstance(agent, ClaudeAgent)

    def test_invalid_type(self):
        import pytest
        with pytest.raises(ValueError):
            get_agent("invalid_agent")
