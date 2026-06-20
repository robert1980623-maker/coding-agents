"""StreamExecutor — async subprocess execution with streaming events."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Optional

from coding_agents.models import (
    Event,
    EventType,
    ExecutionConfig,
    SessionStatus,
    TERMINAL_STATUSES,
)
from coding_agents.storage.base import StorageBackend

logger = logging.getLogger(__name__)


class SeqCounter:
    """Per-session monotonically-increasing sequence counter.

    Uses asyncio.Lock to guarantee uniqueness across concurrent readers.
    """

    def __init__(self) -> None:
        self._value = 0
        self._lock = asyncio.Lock()

    async def next(self) -> int:
        async with self._lock:
            self._value += 1
            return self._value

    @property
    def value(self) -> int:
        return self._value


class StreamExecutor:
    """Stream subprocess output as events with durable storage.

    Design (DESIGN.md §4.2 + v1.2.1 P0-NEW fixes):
    - SeqCounter for globally monotonic seq across channels
    - Write-to-storage before yielding (storage is durable truth)
    - Heartbeat throttled to at most 1 write/second
    - Idle-timeout watchdog kills process after idle_timeout_seconds
    - session.start / error / result events use json.dumps for safety
    - stdout/stderr readers push to a single asyncio.Queue (readers_remaining counter)
    - finally block checks terminal status to avoid overwriting TIMEOUT set by watchdog
    - _flush atomically swaps buffer to avoid concurrent drops
    """

    def __init__(self, store: StorageBackend, config: ExecutionConfig) -> None:
        self.store = store
        self.config = config
        self._process: Optional[asyncio.subprocess.Process] = None
        self._seq = SeqCounter()
        self._buffer: list[Event] = []
        self._last_flush = time.monotonic()
        self._last_heartbeat_write = 0.0

    async def execute(
        self,
        session_id: str,
        command: list[str],
        workdir: str,
        env: Optional[dict[str, str]] = None,
    ) -> AsyncIterator[Event]:
        """Execute command and yield events as they arrive."""

        # Emit session.start event
        start_seq = await self._seq.next()
        start_event = Event(
            session_id=session_id,
            channel="system",
            seq=start_seq,
            type=EventType.SESSION_START,
            data=json.dumps(
                {
                    "session_id": session_id,
                    "agent": command[0] if command else "unknown",
                    "timestamp": datetime.now(timezone.utc).timestamp(),
                }
            ),
        )
        self._buffer.append(start_event)
        await self._flush()
        yield start_event

        # Merge config.env with passed env
        merged_env: Optional[dict[str, str]] = None
        if self.config.env or env:
            import os

            merged_env = {**os.environ, **(env or {}), **self.config.env}

        # Launch subprocess
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.config.line_limit,
                env=merged_env,
            )
        except Exception as e:
            # Emit error event
            error_seq = await self._seq.next()
            error_event = Event(
                session_id=session_id,
                channel="system",
                seq=error_seq,
                type=EventType.ERROR,
                data=json.dumps(
                    {
                        "code": "SUBPROCESS_FAILED",
                        "message": str(e),
                        "command": command,
                        "timestamp": datetime.now(timezone.utc).timestamp(),
                    }
                ),
            )
            self._buffer.append(error_event)
            await self._flush()
            yield error_event

            # P0-NEW-5: Mark session FAILED on startup failure
            await self.store.update_session(
                session_id,
                status=SessionStatus.FAILED,
                finished_at=datetime.now(timezone.utc),
                metadata={"error": str(e), "error_type": type(e).__name__},
            )
            return

        # Update session with PID and RUNNING status
        await self.store.update_session(
            session_id,
            pid=self._process.pid,
            status=SessionStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
            last_heartbeat_at=datetime.now(timezone.utc),
        )

        # Event queue with readers_remaining counter
        event_queue: asyncio.Queue[Optional[Event]] = asyncio.Queue()
        readers_remaining = 2  # stdout reader + stderr drain

        async def mark_reader_done() -> None:
            nonlocal readers_remaining
            readers_remaining -= 1
            if readers_remaining == 0:
                await event_queue.put(None)

        # stderr drain task
        stderr_task = asyncio.create_task(
            self._drain_stderr(session_id, self._process.stderr, event_queue, mark_reader_done)
        )

        # Idle-timeout watchdog
        watchdog_task = asyncio.create_task(self._idle_watchdog(session_id))

        # stdout reader task
        async def stdout_reader() -> None:
            try:
                assert self._process is not None and self._process.stdout is not None
                async for line in self._process.stdout:
                    now = time.monotonic()
                    if now - self._last_heartbeat_write >= 1.0:
                        await self.store.update_session(
                            session_id, last_heartbeat_at=datetime.now(timezone.utc)
                        )
                        self._last_heartbeat_write = now

                    seq = await self._seq.next()
                    text = line.decode(errors="replace")
                    event = Event(
                        session_id=session_id,
                        channel="stdout",
                        seq=seq,
                        type=EventType.STDOUT,
                        data=self._extract_text(text, self.config.output_mode),
                        raw_json=text if self.config.output_mode == "passthrough" else None,
                    )
                    self._buffer.append(event)
                    await self._flush_if_needed()
                    await event_queue.put(event)
            finally:
                await mark_reader_done()

        stdout_task = asyncio.create_task(stdout_reader())

        try:
            while True:
                event = await event_queue.get()
                if event is None:
                    break
                yield event
                await self._check_watch_patterns(session_id, event)
        finally:
            await self._flush()

            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass

            try:
                await stdout_task
            except asyncio.CancelledError:
                pass

            try:
                await stderr_task
            except asyncio.CancelledError:
                pass

            # Wait for process to finish
            if self._process is not None:
                exit_code = await self._process.wait()
            else:
                exit_code = -1

            # P0-NEW-3: Don't overwrite terminal status (e.g., TIMEOUT from watchdog)
            current_session = await self.store.get_session(session_id)
            if current_session is not None and current_session.status in TERMINAL_STATUSES:
                logger.info(
                    "session %s already in terminal state %s, skipping status update",
                    session_id,
                    current_session.status.value,
                )
            else:
                await self.store.update_session(
                    session_id,
                    status=SessionStatus.COMPLETED if exit_code == 0 else SessionStatus.FAILED,
                    exit_code=exit_code,
                    finished_at=datetime.now(timezone.utc),
                )

            result_seq = await self._seq.next()
            yield Event(
                session_id=session_id,
                channel="system",
                seq=result_seq,
                type=EventType.RESULT,
                data=json.dumps({"exit_code": exit_code}),
            )

    async def _drain_stderr(
        self,
        session_id: str,
        stderr: Optional[asyncio.StreamReader],
        queue: asyncio.Queue[Optional[Event]],
        on_done: Callable[[], Any],
    ) -> None:
        """Read stderr and push events to the shared queue."""
        try:
            if stderr is None:
                return
            async for line in stderr:
                seq = await self._seq.next()
                text = line.decode(errors="replace")
                event = Event(
                    session_id=session_id,
                    channel="stderr",
                    seq=seq,
                    type=EventType.STDERR,
                    data=text,
                )
                self._buffer.append(event)
                await self._flush_if_needed()
                await queue.put(event)
        finally:
            await on_done()

    async def _idle_watchdog(self, session_id: str) -> None:
        """Check for idle timeout every 5 seconds and terminate if exceeded."""
        try:
            while True:
                await asyncio.sleep(5)
                session = await self.store.get_session(session_id)
                if session is None:
                    return
                if session.last_heartbeat_at is None:
                    continue
                idle_seconds = (
                    datetime.now(timezone.utc) - session.last_heartbeat_at
                ).total_seconds()
                if idle_seconds > self.config.idle_timeout_seconds:
                    if self._process is not None and self._process.returncode is None:
                        self._process.terminate()
                    await self.store.update_session(
                        session_id,
                        status=SessionStatus.TIMEOUT,
                        finished_at=datetime.now(timezone.utc),
                    )
                    return
        except asyncio.CancelledError:
            raise

    def _extract_text(self, line: str, output_mode: str) -> str:
        """Extract text content for standard mode; pass-through otherwise."""
        if output_mode == "passthrough":
            return line

        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return line

        # Claude Code: assistant.message.content[0].text
        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content", [])
            if content and content[0].get("type") == "text":
                text = content[0].get("text", "")
                return str(text) if text else ""
        # Codex: item.text
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text", "")
                return str(text) if text else ""
        return line

    async def _flush_if_needed(self) -> None:
        """Flush buffer when it reaches 100 items or 100ms has elapsed."""
        if len(self._buffer) >= 100 or time.monotonic() - self._last_flush >= 0.1:
            await self._flush()

    async def _flush(self) -> None:
        """Write buffered events to storage.

        P1-NEW-1: atomically swaps the buffer to avoid concurrent drops
        when stdout/stderr both append during the await on store.append_events.
        """
        if not self._buffer:
            return
        events, self._buffer = self._buffer, []
        self._last_flush = time.monotonic()
        await self.store.append_events(events)

    async def _check_watch_patterns(self, session_id: str, event: Event) -> None:
        """Check watch patterns and act on matches."""
        for wp in self.config.watch_patterns:
            if wp.pattern in event.data:
                if wp.action == "stop":
                    if self._process is not None and self._process.returncode is None:
                        self._process.terminate()
                elif wp.action == "notify":
                    # Notify placeholder — future: callback dispatch
                    pass
                elif wp.action == "callback" and wp.callback:
                    # Future: HTTP webhook
                    pass

    async def kill(self) -> None:
        """Terminate the running subprocess."""
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
