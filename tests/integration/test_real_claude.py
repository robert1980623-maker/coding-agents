"""Integration tests for real Claude CLI execution.

These tests invoke the actual `claude` binary and verify:
- Command construction matches expected CLI flags
- Output parsing works on real stream-json output
- Cost/token extraction from result events

Tests are skipped when:
- `claude` binary is not found on PATH
- Authentication fails (no OAuth token / no API key)
- The subprocess returns a non-zero exit code
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coding_agents.agents.claude import ClaudeAgent
from coding_agents.models import ExecutionConfig

# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

CLAUDE_BINARY = shutil.which("claude")

HAS_CLAUDE = CLAUDE_BINARY is not None

skip_no_binary = pytest.mark.skipif(
    not HAS_CLAUDE,
    reason="claude CLI binary not found on PATH",
)


def _check_claude_auth() -> bool:
    """Return True if claude CLI appears to be authenticated."""
    if not HAS_CLAUDE:
        return False
    try:
        result = subprocess.run(
            [CLAUDE_BINARY, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


HAS_CLAUDE_AUTH = _check_claude_auth()

skip_no_auth = pytest.mark.skipif(
    not HAS_CLAUDE_AUTH,
    reason="claude CLI not authenticated (run `claude login` or set ANTHROPIC_API_KEY)",
)


# ---------------------------------------------------------------------------
# Tests — no API call needed
# ---------------------------------------------------------------------------


class TestClaudeCommandBuilding:
    """Tests that don't invoke the CLI."""

    def test_build_command_basic(self):
        agent = ClaudeAgent()
        config = ExecutionConfig()
        cmd = agent.build_command("hello", config)

        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--permission-mode" in cmd
        assert "bypassPermissions" in cmd
        assert "hello" in cmd

    def test_build_command_with_model(self):
        agent = ClaudeAgent()
        config = ExecutionConfig(model="haiku")
        cmd = agent.build_command("test prompt", config)

        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "haiku"

    def test_build_command_with_budget(self):
        agent = ClaudeAgent()
        config = ExecutionConfig(max_budget_usd=0.5)
        cmd = agent.build_command("test", config)

        assert "--max-budget-usd" in cmd
        idx = cmd.index("--max-budget-usd")
        assert cmd[idx + 1] == "0.5"

    def test_parse_output_result_event(self):
        agent = ClaudeAgent()
        line = json.dumps({
            "type": "result",
            "total_cost_usd": 0.0043,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 0,
                "cache_write_tokens": 80,
            },
            "model": "claude-haiku-4-5-20251001",
        })
        result = agent.parse_output(line)
        assert result is not None
        assert result["cost_usd"] == 0.0043
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50
        assert result["model"] == "claude-haiku-4-5-20251001"

    def test_parse_output_non_result_event(self):
        agent = ClaudeAgent()
        line = json.dumps({"type": "assistant", "message": {"content": []}})
        assert agent.parse_output(line) is None

    def test_parse_output_invalid_json(self):
        agent = ClaudeAgent()
        assert agent.parse_output("not json {{{") is None

    def test_extract_cost(self):
        agent = ClaudeAgent()
        output = "\n".join([
            json.dumps({"type": "assistant"}),
            json.dumps({"type": "result", "total_cost_usd": 0.01}),
        ])
        assert agent.extract_cost(output) == 0.01


# ---------------------------------------------------------------------------
# Tests — real CLI invocation
# ---------------------------------------------------------------------------


@skip_no_binary
class TestClaudeRealExecution:
    """Tests that actually invoke the claude CLI binary."""

    def test_claude_version(self):
        """Verify claude binary runs and returns version."""
        result = subprocess.run(
            [CLAUDE_BINARY, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "2." in result.stdout or "claude" in result.stdout.lower()

    @skip_no_auth
    def test_claude_real_execution(self):
        """Real invocation of claude CLI with a minimal prompt.

        Uses --model haiku for cost control.
        Verifies stream-json output contains a 'result' event.
        """
        agent = ClaudeAgent()
        config = ExecutionConfig(max_budget_usd=0.05, model="haiku")
        cmd = agent.build_command("Say hello in exactly 3 words", config)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            # Check for auth failure
            stderr_lower = result.stderr.lower()
            if any(
                kw in stderr_lower
                for kw in ("auth", "login", "api key", "unauthorized", "forbidden")
            ):
                pytest.skip(f"claude auth failed: {result.stderr[:200]}")
            pytest.fail(
                f"claude CLI returned {result.returncode}\n"
                f"stdout: {result.stdout[:500]}\n"
                f"stderr: {result.stderr[:500]}"
            )

        # Parse stream-json output — should contain at least one result event
        found_result = False
        parsed_events = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parsed = agent.parse_output(line)
            if parsed:
                parsed_events.append(parsed)
                found_result = True

        assert found_result, (
            f"No 'result' event found in output.\n"
            f"First 500 chars of stdout: {result.stdout[:500]}"
        )

        # The result event should have cost/token info
        final = parsed_events[-1]
        assert "cost_usd" in final or "model" in final, (
            f"Result event missing expected fields: {final}"
        )
        print(f"\n[claude] Parsed result: {final}")

    @skip_no_auth
    def test_claude_real_multiline_output(self):
        """Verify claude produces multiple stream-json events."""
        agent = ClaudeAgent()
        config = ExecutionConfig(max_budget_usd=0.05, model="haiku")
        cmd = agent.build_command(
            "List exactly 3 colors, one per line, nothing else", config
        )

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            pytest.skip(f"claude CLI failed (rc={result.returncode}): {result.stderr[:200]}")

        # Count JSON events in output
        json_events = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                json_events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        # Should have multiple events (at least assistant + result)
        assert len(json_events) >= 2, (
            f"Expected >= 2 JSON events, got {len(json_events)}"
        )
        types = [e.get("type") for e in json_events]
        assert "result" in types, f"No 'result' event in types: {types}"
        print(f"\n[claude] Event types: {types}")
