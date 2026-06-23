"""StreamExecutor — async subprocess execution with streaming events."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Optional

import structlog

from coding_agents.models import (
    Event,
    EventType,
    ExecutionConfig,
    SessionStatus,
    TERMINAL_STATUSES,
)
from coding_agents.storage.base import StorageBackend

logger = structlog.get_logger(__name__)


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
        # Monotonic timestamp of last stdout line (used by _heartbeat_writer
        # to gate DB writes — preserves the original semantic that heartbeat
        # only advances when there is actual stdout activity).
        self._last_stdout_activity: float = 0.0

    async def execute(
        self,
        session_id: str,
        command: list[str],
        workdir: str,
        env: Optional[dict[str, str]] = None,
    ) -> AsyncIterator[Event]:
        """Execute command and yield events as they arrive."""

        # Store the session ID so we can access it in _extract_text
        self._current_session_id = session_id

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
                start_new_session=True,  # v0.2.14: detach subprocess to its own process group
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

        # Heartbeat checker: polls DB for kill/failed signals
        heartbeat_task = asyncio.create_task(self._heartbeat_checker(session_id))

        # Heartbeat writer: independent task that updates last_heartbeat_at
        # every second. Previously embedded in stdout_reader hot path where
        # the heavy store.update_session call (Lock + to_thread + commit)
        # blocked stdout reads.
        heartbeat_writer_task = asyncio.create_task(
            self._heartbeat_writer(session_id)
        )

        # stdout reader task
        async def stdout_reader() -> None:
            try:
                assert self._process is not None and self._process.stdout is not None
                async for line in self._process.stdout:
                    self._last_stdout_activity = time.monotonic()
                    text = line.decode(errors="replace")
                    # v0.2.18: _extract_text returns None for events that
                    # should be filtered out of storage entirely (e.g.
                    # thinking_tokens). Skip event creation for those so
                    # they don't consume a seq number or a DB row.
                    data = self._extract_text(text, self.config.output_mode)
                    if data is None:
                        continue
                    
                    # Extract native session IDs from agent output (async context)
                    try:
                        event_data = json.loads(data)
                        event_type = event_data.get("type")
                        # Claude Code: {"type":"system","subtype":"init","session_id":"..."}
                        if (event_type == "system" and
                            event_data.get("subtype") == "init" and
                            "session_id" in event_data):
                            await self._store_native_session_id(event_data["session_id"])
                        # Codex: {"type":"thread.started","thread_id":"..."}
                        elif (event_type == "thread.started" and
                              "thread_id" in event_data):
                            await self._store_native_session_id(event_data["thread_id"])
                    except (json.JSONDecodeError, ValueError):
                        pass
                    
                    seq = await self._seq.next()
                    event = Event(
                        session_id=session_id,
                        channel="stdout",
                        seq=seq,
                        type=EventType.STDOUT,
                        data=data,
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

            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

            heartbeat_writer_task.cancel()
            try:
                await heartbeat_writer_task
            except asyncio.CancelledError:
                pass

            # v0.2.14: With start_new_session=True, the subprocess is in
            # its own process group and detached from the wrapper. If the
            # executor is being cancelled (e.g. wrapper got SIGTERM), we
            # must NOT terminate the subprocess - it should keep running
            # so the user can `tail` its output. We cancel the readers
            # explicitly (they'd otherwise block forever on the still-open
            # pipes) and use a short timeout for the wait.
            stdout_task.cancel()
            try:
                await stdout_task
            except asyncio.CancelledError:
                pass

            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass

            # Wait for the subprocess with a short timeout. In normal
            # completion the subprocess has already exited (the readers
            # saw EOF before we got here, so wait() returns immediately).
            # In cancellation the subprocess is detached and may still
            # be running; we don't block on it.
            if self._process is not None:
                try:
                    exit_code = await asyncio.wait_for(
                        self._process.wait(), timeout=0.5
                    )
                except asyncio.TimeoutError:
                    # Subprocess is detached and still running; the OS
                    # will reap it when it exits. Don't block the wrapper.
                    exit_code = -1
            else:
                exit_code = -1

            # P0-NEW-3: Don't overwrite terminal status (e.g., TIMEOUT from watchdog)
            current_session = await self.store.get_session(session_id)
            if current_session is not None and current_session.status in TERMINAL_STATUSES:
                logger.info(
                    "session_terminal_state_skip_update",
                    session_id=session_id,
                    status=current_session.status.value,
                )
            else:
                # v0.2.14: When the wrapper detached (exited while the
                # subprocess is still running in its own process group),
                # exit_code will be -1 from the 0.5s wait timeout. Record
                # the subprocess PID in metadata so users can `tail` the
                # agent's output or send SIGTERM to the process group.
                metadata_update: dict = {}
                if exit_code == -1 and self._process is not None and self._process.pid is not None:
                    metadata_update["detached_subprocess"] = True
                    metadata_update["subprocess_pid"] = int(self._process.pid)
                    try:
                        import os as _os
                        metadata_update["subprocess_pgid"] = _os.getpgid(int(self._process.pid))
                    except (ProcessLookupError, PermissionError):
                        pass
                await self.store.update_session(
                    session_id,
                    status=SessionStatus.COMPLETED if exit_code == 0 else SessionStatus.FAILED,
                    exit_code=exit_code,
                    finished_at=datetime.now(timezone.utc),
                    metadata=metadata_update or None,
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

    async def _heartbeat_checker(self, session_id: str) -> None:
        """Poll DB for kill/failed signals and terminate subprocess if detected.

        Checks every 2 seconds. When a terminal status (KILLED, FAILED) is
        observed, sends SIGTERM, waits up to 5s for graceful shutdown, then
        SIGKILL if needed.
        """
        try:
            while True:
                await asyncio.sleep(2)
                session = await self.store.get_session(session_id)
                if session is None:
                    return
                if session.status in {SessionStatus.KILLED, SessionStatus.FAILED}:
                    if self._process is not None and self._process.returncode is None:
                        logger.info(
                            "heartbeat_kill_signal",
                            session_id=session_id,
                            status=session.status.value,
                        )
                        await self._terminate_process()
                    return
        except asyncio.CancelledError:
            raise

    async def _heartbeat_writer(self, session_id: str) -> None:
        """Write last_heartbeat_at every second, independent of stdout reads.

        Extracted from stdout_reader so the heavy store.update_session call
        (asyncio.Lock + asyncio.to_thread + DB commit) does not block the
        stdout hot path.

        Only writes when stdout has produced activity since the last write —
        preserves the original semantic where the heartbeat represents
        "process is producing output", not just "process exists". The idle
        watchdog relies on this: if no output arrives, heartbeat stops
        advancing, and the watchdog eventually kills the process.
        """
        last_written_activity: float = 0.0
        try:
            while True:
                await asyncio.sleep(1)
                activity = self._last_stdout_activity
                if activity > last_written_activity:
                    await self.store.update_session(
                        session_id, last_heartbeat_at=datetime.now(timezone.utc)
                    )
                    last_written_activity = activity
        except asyncio.CancelledError:
            raise

    async def _store_native_session_id(self, native_session_id: str) -> None:
        """Store native agent session ID in session metadata."""
        try:
            session = await self.store.get_session(self._current_session_id)
            if session is None:
                return
            metadata = dict(session.metadata or {})
            metadata["native_session_id"] = native_session_id
            await self.store.update_session(self._current_session_id, metadata=metadata)
        except Exception:
            pass  # Best-effort; don't break execution if this fails

    async def _terminate_process(self) -> None:
        """Gracefully terminate the subprocess: SIGTERM, wait 5s, then SIGKILL.

        v0.2.14: When ``start_new_session=True`` is set, the subprocess
        is in its own process group, so ``terminate()`` (which sends
        SIGTERM to the process only) is propagated. We also send SIGTERM
        to the process group to catch any grandchildren, then SIGKILL
        the group if the process doesn't exit in time.
        """
        if self._process is None or self._process.returncode is not None:
            return
        import os
        import signal as _sig
        pid = self._process.pid
        # Send SIGTERM to the whole process group (catches grandchildren).
        try:
            os.killpg(os.getpgid(pid), _sig.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            if self._process.returncode is not None:
                return
            logger.warning(
                "graceful_timeout_force_kill",
                pid=pid,
            )
            try:
                os.killpg(os.getpgid(pid), _sig.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    async def force_terminate_process_group(self) -> None:
        """v0.2.14: Force-kill the subprocess's process group.

        When ``start_new_session=True`` is set, the subprocess is detached
        from the wrapper's process group, so the wrapper's SIGTERM is not
        propagated. This method sends SIGTERM to the entire process group
        and falls back to SIGKILL if it doesn't exit in time.
        """
        if self._process is None or self._process.returncode is not None:
            return
        import os
        import signal as _sig
        pid = self._process.pid
        try:
            os.killpg(os.getpgid(pid), _sig.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(self._process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(pid), _sig.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    # v0.2.18: tool_result preview cutoff (bytes).
    _TOOL_RESULT_PREVIEW_LIMIT: int = 200

    def _extract_text(self, line: str, output_mode: str) -> Optional[str]:
        """Extract text content for standard mode; pass-through otherwise.

        v0.2.20: smart filtering for noise reduction. Returns ``None`` when
        the event should be dropped entirely (not stored in SQLite). This
        keeps the SQLite event stream focused on analytically-useful data:

        - ``thinking_tokens`` events (``{"type":"system","subtype":
          "thinking_tokens",...}``) are dropped — token totals are tracked
          at the session level.
        - ``tool_result`` events (``{"type":"user","message":{"content":
          [{"type":"tool_result",...}]}}``) are replaced with a JSON summary
          containing ``tool_use_id``, ``status``, ``size_bytes``, and a short
          ``preview``.
        - ``assistant`` text, ``result``, and other ``system`` events are
          preserved unchanged.
        """
        if output_mode == "passthrough":
            return line

        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return line

        event_type = event.get("type")

        # v0.2.18 → v0.2.20: drop thinking_tokens events — pure noise in
        # the event stream. Claude Code emits them as:
        #   {"type": "system", "subtype": "thinking_tokens", ...}
        # Token usage is already tracked per-session.
        if event_type == "system" and event.get("subtype") == "thinking_tokens":
            return None

        # Claude Code: assistant.message.content[0].text
        if event_type == "assistant":
            content = event.get("message", {}).get("content", [])
            if content and content[0].get("type") == "text":
                text = content[0].get("text", "")
                return str(text) if text else ""

        # v0.2.18 → v0.2.20: summarize tool_result events. Claude Code
        # nests tool_result content blocks inside user messages:
        #   {"type": "user", "message": {"content": [
        #       {"type": "tool_result", "tool_use_id": "...", "content": ...}
        #   ]}}
        # Full tool return values (often multi-KB file contents or
        # base64-encoded images) are noise for session analysis; a compact
        # summary preserves the useful metadata (tool name, success status,
        # payload size) plus a short preview of the content.
        if event_type == "user":
            content_blocks = event.get("message", {}).get("content", [])
            if (
                isinstance(content_blocks, list)
                and content_blocks
                and content_blocks[0].get("type") == "tool_result"
            ):
                return self._summarize_tool_result(content_blocks[0])

        # Codex: item.text
        if event_type == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text", "")
                return str(text) if text else ""

        return line

    def _summarize_tool_result(self, event: dict) -> str:
        """Build a compact JSON summary for a ``tool_result`` event.

        Extracts the tool use id, success/error status, payload size in
        bytes, and the first ``_TOOL_RESULT_PREVIEW_LIMIT`` characters of
        content (if available and text-based). Returns a JSON string that
        replaces the full event data in storage.
        """
        tool_use_id = event.get("tool_use_id", "")
        content = event.get("content")

        # Determine if the result is an error. Claude tool_result events
        # carry ``is_error: true`` for failed tool invocations.
        is_error = event.get("is_error", False)
        status = "error" if is_error else "ok"

        # Measure raw content size. Content may be a string or a list of
        # content blocks (text/image); stringify the latter for size.
        # v0.2.20: None/empty content → 0 bytes (not 4 for "null").
        if content is None or content == "":
            size_bytes = 0
            preview = ""
        elif isinstance(content, str):
            size_bytes = len(content.encode("utf-8", errors="replace"))
            preview = content[: self._TOOL_RESULT_PREVIEW_LIMIT]
            if len(content) > self._TOOL_RESULT_PREVIEW_LIMIT:
                preview += "..."
        else:
            # Non-string content (e.g. image blocks) — serialize for size
            # but don't try to produce a readable preview.
            serialized = json.dumps(content)
            size_bytes = len(serialized.encode("utf-8", errors="replace"))
            preview = ""

        summary = {
            "tool_use_id": tool_use_id,
            "status": status,
            "size_bytes": size_bytes,
            "preview": preview,
        }
        return json.dumps(summary)

    async def _flush_if_needed(self) -> None:
        """Flush buffer when it reaches 100 items or 100ms has elapsed."""
        if len(self._buffer) >= 100 or time.monotonic() - self._last_flush >= 0.1:
            await self._flush()

    async def _flush(self) -> None:
        """Write buffered events to storage.

        P1-NEW-1: atomically swaps the buffer to avoid concurrent drops
        when stdout/stderr both append during the await on store.append_events.
        On append failure, events are best-effort re-prepended so a later
        flush can retry instead of silently losing them.
        """
        if not self._buffer:
            return
        events, self._buffer = self._buffer, []
        self._last_flush = time.monotonic()
        try:
            await self.store.append_events(events)
        except Exception:
            logger.exception(
                "flush_append_failed",
                event_count=len(events),
            )
            # Best-effort retry: put events back at the head of the buffer
            self._buffer = events + self._buffer

    async def _check_watch_patterns(self, session_id: str, event: Event) -> None:
        """Check watch patterns and act on matches."""
        for wp in self.config.watch_patterns:
            if wp.pattern in event.data:
                if wp.action == "stop":
                    if self._process is not None and self._process.returncode is None:
                        # v0.2.14: with start_new_session=True the subprocess
                        # is in its own process group. ``terminate()`` only
                        # sends SIGTERM to the process itself; use killpg
                        # to also catch grandchildren and ensure the
                        # process group actually exits so the readers
                        # see EOF and the executor's stream can finish.
                        import os as _os
                        import signal as _sig
                        try:
                            _os.killpg(
                                _os.getpgid(self._process.pid),
                                _sig.SIGTERM,
                            )
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
                elif wp.action == "notify":
                    # Notify placeholder — future: callback dispatch
                    pass
                elif wp.action == "callback" and wp.callback:
                    # Future: HTTP webhook
                    pass

    async def kill(self) -> None:
        """Terminate the running subprocess."""
        await self._terminate_process()
