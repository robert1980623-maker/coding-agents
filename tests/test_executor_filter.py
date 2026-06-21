"""Tests for v0.2.18 smart event filtering in _extract_text."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coding_agents.executor import StreamExecutor
from coding_agents.models import (
    Event,
    EventType,
    ExecutionConfig,
    Session,
    SessionStatus,
)
from coding_agents.storage.sqlite import SQLiteStorage


class TestExtractTextFiltering:
    """v0.2.18: _extract_text should filter noisy events."""

    def _make_executor(self, output_mode: str = "standard") -> StreamExecutor:
        config = ExecutionConfig(output_mode=output_mode)
        return StreamExecutor(store=MagicMock(), config=config)

    def test_thinking_tokens_returns_none(self):
        """thinking_tokens events should be dropped entirely (return None)."""
        executor = self._make_executor()
        line = json.dumps({"type": "thinking_tokens", "tokens": 64})
        assert executor._extract_text(line, "standard") is None

    def test_thinking_tokens_with_payload_returns_none(self):
        """Even thinking_tokens with large payloads should be dropped."""
        executor = self._make_executor()
        line = json.dumps({
            "type": "thinking_tokens",
            "tokens": 1024,
            "data": "x" * 10_000,
        })
        assert executor._extract_text(line, "standard") is None

    def test_tool_result_returns_summary(self):
        """tool_result events should be replaced with a JSON summary."""
        executor = self._make_executor()
        line = json.dumps({
            "type": "tool_result",
            "tool_use_id": "toolu_abc123",
            "content": "file contents here",
        })
        result = executor._extract_text(line, "standard")
        assert result is not None
        summary = json.loads(result)
        assert summary["tool_use_id"] == "toolu_abc123"
        assert summary["status"] == "ok"
        assert summary["size_bytes"] == len("file contents here")
        assert summary["preview"] == "file contents here"

    def test_tool_result_error_status(self):
        """tool_result with is_error=true should have status='error'."""
        executor = self._make_executor()
        line = json.dumps({
            "type": "tool_result",
            "tool_use_id": "toolu_err",
            "content": "Error: file not found",
            "is_error": True,
        })
        result = executor._extract_text(line, "standard")
        assert result is not None
        summary = json.loads(result)
        assert summary["status"] == "error"

    def test_tool_result_large_content_preview_truncated(self):
        """Large tool_result content should be truncated in preview."""
        executor = self._make_executor()
        large_content = "x" * 1000
        line = json.dumps({
            "type": "tool_result",
            "tool_use_id": "toolu_big",
            "content": large_content,
        })
        result = executor._extract_text(line, "standard")
        assert result is not None
        summary = json.loads(result)
        assert summary["size_bytes"] == 1000
        assert len(summary["preview"]) == 203  # 200 chars + "..."
        assert summary["preview"].endswith("...")
        assert summary["preview"][:200] == "x" * 200

    def test_tool_result_non_string_content(self):
        """tool_result with non-string content (e.g. image blocks) should still summarize."""
        executor = self._make_executor()
        image_block = [{"type": "image", "source": {"data": "base64..."}}]
        line = json.dumps({
            "type": "tool_result",
            "tool_use_id": "toolu_img",
            "content": image_block,
        })
        result = executor._extract_text(line, "standard")
        assert result is not None
        summary = json.loads(result)
        assert summary["size_bytes"] > 0
        assert summary["preview"] == ""  # non-text content has no preview

    def test_assistant_text_preserved(self):
        """assistant text content should be preserved unchanged."""
        executor = self._make_executor()
        text = "This is the assistant's helpful response."
        line = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        })
        assert executor._extract_text(line, "standard") == text

    def test_system_event_passthrough(self):
        """system events (session metadata) should be preserved."""
        executor = self._make_executor()
        line = json.dumps({
            "type": "system",
            "session_id": "abc123",
            "model": "claude-sonnet-4-5",
        })
        # Falls through to the final `return line`
        assert executor._extract_text(line, "standard") == line

    def test_result_event_passthrough(self):
        """result events should be preserved."""
        executor = self._make_executor()
        line = json.dumps({
            "type": "result",
            "result": "Done!",
            "cost_usd": 0.05,
        })
        assert executor._extract_text(line, "standard") == line

    def test_passthrough_mode_skips_filtering(self):
        """passthrough mode must NOT apply any filtering."""
        executor = self._make_executor(output_mode="passthrough")
        line = json.dumps({"type": "thinking_tokens", "tokens": 64})
        # passthrough returns raw line, even for thinking_tokens
        assert executor._extract_text(line, "passthrough") == line

    def test_non_json_passthrough(self):
        """Non-JSON lines should pass through unchanged."""
        executor = self._make_executor()
        assert executor._extract_text("plain text", "standard") == "plain text"


class TestFilteredEventsNotStored:
    """End-to-end test: filtered events should not appear in storage."""

    async def test_thinking_tokens_not_stored(
        self, storage: SQLiteStorage, tmp_path: Path
    ):
        """thinking_tokens events should not be written to storage."""
        import sys

        config = ExecutionConfig()
        executor = StreamExecutor(store=storage, config=config)
        session = Session(agent="claude", prompt="test", workdir=str(tmp_path))
        await storage.create_session(session)

        # Simulate a subprocess that emits thinking_tokens + normal output.
        # The executor's stdout_reader runs _extract_text on each line;
        # thinking_tokens lines should produce no event in storage.
        lines = [
            json.dumps({"type": "thinking_tokens", "tokens": 64}),
            json.dumps({"type": "thinking_tokens", "tokens": 128}),
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello!"}]},
            }),
            json.dumps({"type": "thinking_tokens", "tokens": 32}),
        ]
        script = "; ".join(f"print({line!r})" for line in lines)
        command = [sys.executable, "-c", script]

        collected: list[Event] = []
        async for event in executor.execute(session.id, command, str(tmp_path)):
            collected.append(event)

        # The stdout events yielded should only contain the assistant text,
        # not the thinking_tokens lines.
        stdout_events = [e for e in collected if e.type == EventType.STDOUT]
        assert len(stdout_events) == 1, (
            f"expected 1 stdout event (assistant text), got {len(stdout_events)}: "
            f"{[e.data[:60] for e in stdout_events]}"
        )
        assert "Hello!" in stdout_events[0].data

        # Storage should also only contain the non-filtered event.
        stored = await storage.get_events(session.id)
        stored_stdout = [e for e in stored if e.type == EventType.STDOUT]
        assert len(stored_stdout) == 1
        assert "Hello!" in stored_stdout[0].data

    async def test_tool_result_summary_stored(
        self, storage: SQLiteStorage, tmp_path: Path
    ):
        """tool_result events should be stored as summaries."""
        import sys

        config = ExecutionConfig()
        executor = StreamExecutor(store=storage, config=config)
        session = Session(agent="claude", prompt="test", workdir=str(tmp_path))
        await storage.create_session(session)

        large_content = "x" * 5000
        tool_result_line = json.dumps({
            "type": "tool_result",
            "tool_use_id": "toolu_abc",
            "content": large_content,
        })
        command = [sys.executable, "-c", f"print({tool_result_line!r})"]

        collected: list[Event] = []
        async for event in executor.execute(session.id, command, str(tmp_path)):
            collected.append(event)

        stdout_events = [e for e in collected if e.type == EventType.STDOUT]
        assert len(stdout_events) == 1

        # The stored data should be a summary, NOT the full content.
        data = stdout_events[0].data
        summary = json.loads(data)
        assert summary["tool_use_id"] == "toolu_abc"
        assert summary["status"] == "ok"
        assert summary["size_bytes"] == 5000
        assert len(summary["preview"]) < 5000
        assert len(summary["preview"]) <= 203  # 200 + "..."

        # The full 5000-char content should NOT be in the stored data.
        assert large_content not in data
