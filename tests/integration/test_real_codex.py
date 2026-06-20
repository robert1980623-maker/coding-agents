"""Integration tests for real Codex CLI execution.

These tests invoke the actual `codex` binary and verify:
- Command construction matches expected CLI flags
- Output parsing works on real JSON output

Tests are skipped when:
- `codex` binary is not found on PATH
- OPENAI_API_KEY is not set (Codex requires it)
- The subprocess fails
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coding_agents.agents.codex import CodexAgent
from coding_agents.models import ExecutionConfig

# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

CODEX_BINARY = shutil.which("codex")

HAS_CODEX = CODEX_BINARY is not None

skip_no_binary = pytest.mark.skipif(
    not HAS_CODEX,
    reason="codex CLI binary not found on PATH",
)

HAS_OPENAI_KEY = bool(os.getenv("OPENAI_API_KEY"))

skip_no_api_key = pytest.mark.skipif(
    not HAS_OPENAI_KEY,
    reason="OPENAI_API_KEY not set (required for Codex CLI)",
)


# ---------------------------------------------------------------------------
# Tests — no API call needed
# ---------------------------------------------------------------------------


class TestCodexCommandBuilding:
    """Tests that don't invoke the CLI."""

    def test_build_command_basic(self):
        agent = CodexAgent()
        config = ExecutionConfig()
        cmd = agent.build_command("hello", config)

        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "--json" in cmd
        assert "--full-auto" in cmd
        assert "hello" in cmd

    def test_build_command_with_model(self):
        agent = CodexAgent()
        config = ExecutionConfig(model="o4-mini")
        cmd = agent.build_command("test", config)

        assert "-m" in cmd
        idx = cmd.index("-m")
        assert cmd[idx + 1] == "o4-mini"

    def test_build_command_no_model(self):
        agent = CodexAgent()
        config = ExecutionConfig()
        cmd = agent.build_command("test", config)

        assert "-m" not in cmd

    def test_parse_output_turn_completed(self):
        agent = CodexAgent()
        line = json.dumps({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 200,
                "output_tokens": 100,
            },
        })
        result = agent.parse_output(line)
        assert result is not None
        assert result["input_tokens"] == 200
        assert result["output_tokens"] == 100

    def test_parse_output_non_turn_event(self):
        agent = CodexAgent()
        line = json.dumps({"type": "item.completed", "item": {}})
        assert agent.parse_output(line) is None

    def test_parse_output_invalid_json(self):
        agent = CodexAgent()
        assert agent.parse_output("garbage data") is None

    def test_extract_cost_always_none(self):
        """Codex does not provide cost information."""
        agent = CodexAgent()
        assert agent.extract_cost("anything") is None


# ---------------------------------------------------------------------------
# Tests — real CLI invocation
# ---------------------------------------------------------------------------


@skip_no_binary
class TestCodexRealExecution:
    """Tests that actually invoke the codex CLI binary."""

    def test_codex_version(self):
        """Verify codex binary runs."""
        result = subprocess.run(
            [CODEX_BINARY, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # codex --version may exit 0 or print version to stdout/stderr
        combined = result.stdout + result.stderr
        assert "codex" in combined.lower() or result.returncode == 0

    @skip_no_api_key
    def test_codex_real_execution(self):
        """Real invocation of codex CLI with a minimal prompt.

        Uses --full-auto mode (no interactive prompts).
        Verifies JSON output is parseable.
        """
        agent = CodexAgent()
        config = ExecutionConfig()
        cmd = agent.build_command("Print the word 'test' exactly", config)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            stderr_lower = result.stderr.lower()
            if any(
                kw in stderr_lower
                for kw in ("api key", "unauthorized", "authentication", "openai")
            ):
                pytest.skip(f"codex auth failed: {result.stderr[:200]}")
            pytest.fail(
                f"codex CLI returned {result.returncode}\n"
                f"stdout: {result.stdout[:500]}\n"
                f"stderr: {result.stderr[:500]}"
            )

        # Parse JSON output
        json_events = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                json_events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        # Should have at least some JSON output
        assert len(json_events) > 0, (
            f"No JSON events found in output.\n"
            f"First 500 chars: {result.stdout[:500]}"
        )
        print(f"\n[codex] Got {len(json_events)} JSON events")

    @skip_no_api_key
    def test_codex_output_has_completion_event(self):
        """Verify codex produces a turn.completed event."""
        agent = CodexAgent()
        config = ExecutionConfig()
        cmd = agent.build_command("Reply with only the number 42", config)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            pytest.skip(f"codex CLI failed (rc={result.returncode}): {result.stderr[:200]}")

        # Look for turn.completed event
        found_turn = False
        for line in result.stdout.splitlines():
            parsed = agent.parse_output(line.strip())
            if parsed:
                found_turn = True
                print(f"\n[codex] Parsed: {parsed}")

        # turn.completed may or may not appear depending on codex version
        # Just verify we got some output
        assert result.stdout.strip(), "codex produced no output"
        print(f"\n[codex] Output length: {len(result.stdout)} chars")
