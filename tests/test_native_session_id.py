#!/usr/bin/env python3
"""Test script to verify the native session ID fix works properly."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

from src.coding_agents.orchestrator.cli_integration import build_resume_command
from src.coding_agents.models import AgentType


def test_claude_with_native_session_id():
    """Test Claude Code with native session ID."""
    base_command = ["claude", "code", "--model", "sonnet"]
    result = build_resume_command(
        base_command,
        session_id="ca-uuid-123",
        last_seq=5,
        agent_type=AgentType.CLAUDE,
        session_metadata={"native_session_id": "cc-native-456"}
    )

    expected = ["claude", "code", "--model", "sonnet", "--resume", "cc-native-456"]
    assert result == expected, f"Expected {expected}, got {result}"
    print("✓ Claude with native session ID: PASSED")


def test_claude_without_native_session_id():
    """Test Claude Code without native session ID (fallback)."""
    base_command = ["claude", "code", "--model", "sonnet"]
    result = build_resume_command(
        base_command,
        session_id="ca-uuid-123",
        last_seq=5,
        agent_type=AgentType.CLAUDE,
        session_metadata={}
    )

    expected = ["claude", "code", "--model", "sonnet", "--resume", "ca-uuid-123"]
    assert result == expected, f"Expected {expected}, got {result}"
    print("✓ Claude without native session ID: PASSED")


def test_codex_with_native_session_id():
    """Test Codex with native session ID (thread ID)."""
    base_command = ["codex", "exec", "--model", "gpt-4"]
    result = build_resume_command(
        base_command,
        session_id="ca-uuid-123",
        last_seq=5,
        agent_type=AgentType.CODEX,
        session_metadata={"native_session_id": "thread-789"}
    )

    expected = ["codex", "exec", "resume", "thread-789"]
    assert result == expected, f"Expected {expected}, got {result}"
    print("✓ Codex with native session ID: PASSED")


def test_codex_without_native_session_id():
    """Test Codex without native session ID (fallback)."""
    base_command = ["codex", "exec", "--model", "gpt-4"]
    result = build_resume_command(
        base_command,
        session_id="ca-uuid-123",
        last_seq=5,
        agent_type=AgentType.CODEX,
        session_metadata={}
    )

    expected = ["codex", "exec", "--model", "gpt-4", "--resume-from", "5"]
    assert result == expected, f"Expected {expected}, got {result}"
    print("✓ Codex without native session ID: PASSED")


def test_generic_with_native_session_id():
    """Test generic agent with native session ID."""
    base_command = ["custom-agent", "--param", "value"]
    result = build_resume_command(
        base_command,
        session_id="ca-uuid-123",
        last_seq=5,
        agent_type=None,
        session_metadata={"native_session_id": "custom-native-000"}
    )

    expected = ["custom-agent", "--param", "value", "--resume", "custom-native-000", "--from-seq", "5"]
    assert result == expected, f"Expected {expected}, got {result}"
    print("✓ Generic with native session ID: PASSED")


def test_generic_without_native_session_id():
    """Test generic agent without native session ID (fallback)."""
    base_command = ["custom-agent", "--param", "value"]
    result = build_resume_command(
        base_command,
        session_id="ca-uuid-123",
        last_seq=5,
        agent_type=None,
        session_metadata={}
    )

    expected = ["custom-agent", "--param", "value", "--resume", "ca-uuid-123", "--from-seq", "5"]
    assert result == expected, f"Expected {expected}, got {result}"
    print("✓ Generic without native session ID: PASSED")


if __name__ == "__main__":
    print("Testing native session ID fix...")
    test_claude_with_native_session_id()
    test_claude_without_native_session_id()
    test_codex_with_native_session_id()
    test_codex_without_native_session_id()
    test_generic_with_native_session_id()
    test_generic_without_native_session_id()
    print("\n🎉 All tests passed!")