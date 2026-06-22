"""``run``, ``dispatch``, ``dispatch-bg`` and the hidden ``_bg-runner`` command."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import typer

from coding_agents.cli import get_agent
from coding_agents.executor import StreamExecutor
from coding_agents.models import (
    AgentType,
    EventType,
    ExecutionConfig,
    Session,
    SessionStatus,
)
from coding_agents.registry import SessionRegistry


def register(app: typer.Typer) -> None:
    """Register the run-family commands on the given Typer app."""
    app.command()(run)
    app.command(name="dispatch")(dispatch)
    app.command(name="dispatch-bg")(dispatch_bg)
    app.command(name="_bg-runner", hidden=True)(bg_runner)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def run(
    agent: str = typer.Argument(..., help="Agent type: claude or codex"),
    prompt: str = typer.Argument(..., help="Prompt to send to the agent"),
    workdir: str = typer.Option(".", help="Working directory for the agent"),
    model: Optional[str] = typer.Option(None, help="Model override"),
    budget: Optional[float] = typer.Option(None, help="Max budget in USD"),
    output_mode: str = typer.Option("standard", help="Output mode: standard or passthrough"),
    verbose: bool = typer.Option(False, help="Verbose output"),
) -> None:
    """[DEPRECATED] Run a coding agent session. Use 'dispatch' instead."""
    import warnings
    warnings.warn(
        "'coding-agents run' is deprecated; use 'coding-agents dispatch' instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    from coding_agents.cli._utils import _run_async
    _run_async(_run_session(agent, prompt, workdir, model, budget, output_mode, verbose))


def dispatch(
    agent: str = typer.Argument(..., help="Agent type: claude or codex"),
    prompt: str = typer.Argument(..., help="Prompt to send to the agent"),
    workdir: Optional[str] = typer.Option(
        None,
        "--workdir", "-w",
        help="Working directory for the agent subprocess (default: current dir). "
             "This is where the agent reads AGENTS.md / CLAUDE.md / .claude/skills/ "
             "from, so always set this to your project root.",
    ),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model override"),
    budget: Optional[float] = typer.Option(None, "--budget", "-b", help="Max budget in USD"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
) -> None:
    """Dispatch a coding agent session in the current project.

    This is the recommended way to run an agent: the subprocess starts in
    ``--workdir`` (default: current directory), so it sees your project's
    AGENTS.md / CLAUDE.md / .claude/skills/ natively — no prompt injection
    needed.

    Output is bounded: dispatch only prints the final result event
    (one JSON line, < 1KB) plus the session id. All intermediate
    stdout/stderr goes to SQLite. Use ``coding-agents status <id>`` or
    ``coding-agents tail <id>`` to inspect the full stream — this
    keeps dispatch output safely below the OpenClaw exec 1MB buffer.

    Example:
        coding-agents dispatch claude "fix the auth bug" --workdir ~/project
    """
    # Default workdir to current dir (not '.') so it resolves to the absolute path
    # the agent subprocess actually sees.
    effective_workdir = workdir or "."
    from coding_agents.cli._utils import _run_async
    _run_async(
        _run_session(agent, prompt, effective_workdir, model, budget, "standard", verbose)
    )


def dispatch_bg(
    agent: str = typer.Argument(..., help="Agent type: claude or codex"),
    prompt: str = typer.Argument(..., help="Prompt to send to the agent"),
    workdir: Optional[str] = typer.Option(
        None,
        "--workdir", "-w",
        help="Working directory for the agent subprocess (default: current dir).",
    ),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model override"),
    budget: Optional[float] = typer.Option(None, "--budget", "-b", help="Max budget in USD"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
) -> None:
    """Dispatch a coding agent in fire-and-forget mode.

    Unlike ``dispatch``, this command returns the session_id within ~1 second
    and exits immediately. The actual agent execution runs in a detached
    subprocess that is independent of the dispatch wrapper's lifetime.

    Why this exists: OpenClaw's exec tool has a 30s timeout. Calling
    ``dispatch`` from a long-running agent would kill the wrapper before the
    agent finishes. ``dispatch-bg`` solves this by returning immediately;
    the caller polls ``status <id>`` / ``tail <id>`` to inspect progress.

    Output (always 2 lines, < 1KB):
        session_id=<uuid>
        {"session_id": "...", "status": "running"}
    """
    effective_workdir = workdir or "."
    from coding_agents.cli._utils import _run_async
    _run_async(
        _dispatch_bg_setup(agent, prompt, effective_workdir, model, budget, verbose)
    )


def bg_runner(
    session_id: str = typer.Argument(..., help="Session id to resume"),
    agent_name: str = typer.Argument(..., help="Agent type: claude or codex"),
    workdir: str = typer.Argument(..., help="Working directory"),
    model: str = typer.Argument("", help="Model override (empty = none)"),
    budget: str = typer.Argument("", help="Max budget (empty = none)"),
) -> None:
    """Internal: detached runner that executes a session.

    Spawned by ``dispatch-bg``. Runs independently of the dispatch wrapper,
    so the agent subprocess continues even if the wrapper is killed by
    OpenClaw's 30s exec timeout.

    Use ``coding-agents status <id>`` or ``coding-agents tail <id>`` to
    inspect progress — do not call this directly.
    """
    budget_val = float(budget) if budget else None
    model_val = model or None
    from coding_agents.cli._utils import _run_async
    _run_async(
        _run_session(
            agent_name,
            _read_prompt(session_id),
            workdir,
            model_val,
            budget_val,
            "standard",
            False,
            existing_session_id=session_id,
        )
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_prompt(session_id: str) -> str:
    """Read the prompt from the persisted session record (sync helper for bg runner)."""
    from coding_agents.cli._utils import DEFAULT_DB
    db_path = os.environ.get("CODING_AGENTS_DB", DEFAULT_DB)
    db_path = os.path.expanduser(db_path)
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            row = conn.execute("SELECT prompt FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"Session not found: {session_id}")
            return row[0]
    except Exception as e:
        raise RuntimeError(f"Failed to read session prompt: {e}")


async def _dispatch_bg_setup(
    agent_name: str,
    prompt: str,
    workdir: str,
    model: Optional[str],
    budget: Optional[float],
    verbose: bool,
) -> None:
    """Set up a session and spawn a detached runner subprocess.

    The runner subprocess (coding-agents _bg-runner) takes over the
    execution lifecycle independently of this wrapper's lifetime. When this
    wrapper exits, the runner continues running in its own process group.
    """
    from coding_agents.cli._utils import _get_storage, console
    try:
        agent_type = AgentType(agent_name)
    except ValueError:
        console.print(f"[red]Unknown agent type: {agent_name}[/red]")
        raise typer.Exit(code=1)

    storage = _get_storage()
    await storage.initialize()
    try:
        session = Session(
            agent=agent_type, prompt=prompt, workdir=workdir, model=model
        )
        await storage.create_session(session)
    finally:
        await storage.close()

    # Print session_id early so the caller always has it, even if spawn fails.
    console.print(f"session_id={session.id}")

    # Spawn the runner in a fully detached subprocess. close_fds=True +
    # start_new_session=True gives the runner its own session/process group,
    # so even SIGHUP/SIGTERM to this wrapper doesn't propagate to the agent.
    # Use the `coding-agents` CLI wrapper (not `python -m coding_agents`)
    # because the package has no __main__.py.
    runner_argv = [
        "coding-agents", "_bg-runner",
        session.id,
        agent_name,
        workdir,
        model or "",
        str(budget) if budget is not None else "",
    ]
    if verbose:
        console.print(f"[dim]spawning runner: {' '.join(runner_argv)}[/dim]")
    try:
        proc = subprocess.Popen(
            runner_argv,
            start_new_session=True,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "CODING_AGENTS_BG_RUNNER": "1"},
        )
        if verbose:
            console.print(f"[dim]runner PID: {proc.pid}[/dim]")
    except Exception as e:
        # Spawn failed; mark session failed before exiting.
        storage = _get_storage()
        await storage.initialize()
        try:
            await storage.update_session(
                session.id,
                status=SessionStatus.FAILED,
                finished_at=datetime.now(timezone.utc),
                metadata={"error": f"bg-runner spawn failed: {e}"},
            )
        finally:
            await storage.close()
        console.print(json.dumps({
            "session_id": session.id,
            "status": "spawn_failed",
            "error": str(e),
        }))
        raise typer.Exit(code=1)

    # Bound the wrapper output: a single JSON line summarising the dispatch.
    sys.stdout.write(json.dumps({
        "session_id": session.id,
        "status": "running",
    }) + "\n")
    sys.stdout.flush()


async def _run_session(
    agent_name: str,
    prompt: str,
    workdir: str,
    model: Optional[str],
    budget: Optional[float],
    output_mode: str,
    verbose: bool,
    existing_session_id: Optional[str] = None,
) -> None:
    """Run the agent subprocess and persist events to SQLite.

    Output contract: only the final result event (one line) plus the
    session id are written to stdout/stderr. Everything else is
    persisted to SQLite and retrieved with `status` / `tail`.
    This keeps dispatch output safely below the OpenClaw exec 1MB buffer.

    Signal safety: if the wrapper receives SIGTERM / SIGINT (e.g. OpenClaw
    1MB-buffer SIGKILL cascade, Ctrl-C, orchestrator timeout), the signal
    handler converts it into a ``SystemExit`` so the finally block runs.
    The finally block guarantees the session is moved out of ``running``
    and into ``failed`` (with the signal recorded in metadata) before the
    process actually exits — this is what prevents the "session stuck in
    running until gc/recover turns it into orphaned" bug.
    """
    from coding_agents.cli._utils import _get_storage, console
    try:
        agent_type = AgentType(agent_name)
    except ValueError:
        console.print(f"[red]Unknown agent type: {agent_name}[/red]")
        raise typer.Exit(code=1)

    adapter = get_agent(agent_type)
    config = ExecutionConfig(
        output_mode=output_mode,
        model=model,
        max_budget_usd=budget,  # None means "no cap"; agents honor or warn
    )

    # Merge agent-specific env overrides (e.g. Codex needs real HOME
    # when the parent process redirects it, like Hermes profiles).
    overrides = adapter.env_overrides()
    if overrides:
        config.env = {**config.env, **overrides}

    command = adapter.build_command(prompt, config)
    if verbose:
        console.print(f"[dim]command: {' '.join(command)}[/dim]")

    storage = _get_storage()
    await storage.initialize()

    # v0.2.18: If dispatch-bg pre-created the session, reuse the id.
    # Without this, the runner creates a duplicate row that hits the
    # sessions.id PRIMARY KEY constraint, throws, and exits without
    # finalising the pre-existing session (status stays "pending"
    # forever even though the agent subprocess is still running).
    if existing_session_id is not None:
        existing = await storage.get_session(existing_session_id)
        if existing is None:
            raise RuntimeError(
                f"dispatch-bg session {existing_session_id} not found"
            )
        session = existing
    else:
        session = Session(agent=agent_type, prompt=prompt, workdir=workdir, model=model)
        await storage.create_session(session)
    # Always print the session id early so the caller can poll status
    # / tail even if dispatch is killed by the 1MB buffer.
    console.print(f"session_id={session.id}")

    registry = SessionRegistry()
    acquired = await registry.acquire(session.id)
    if not acquired:
        console.print("[red]Timed out waiting for execution slot[/red]")
        await storage.update_session(
            session.id,
            status=SessionStatus.FAILED,
            finished_at=datetime.now(timezone.utc),
            metadata={"error": "Timed out waiting for execution slot"},
        )
        await storage.close()
        raise typer.Exit(code=1)

    executor = StreamExecutor(store=storage, config=config)
    exit_code: Optional[int] = None
    error_text: Optional[str] = None

    # --- Signal handling: convert SIGTERM/SIGINT into a controlled exit ---
    # We track which signal arrived (if any) so the finally block can
    # record it in metadata. Raising SystemExit lets the finally block
    # run and do the cleanup; SIGKILL bypasses everything, but there's
    # nothing any userspace code can do about that.
    _received_signal: dict[str, Any] = {"signal": None}

    def _on_signal(signum: int) -> None:
        _received_signal["signal"] = signum
        # SystemExit is a BaseException that propagates out of the event
        # loop, so ``asyncio.run`` exits with code 128 + signum (POSIX
        # convention). Combined with the v0.2.11 finally block that
        # finalizes the session, this guarantees the session is moved
        # out of ``running`` even if the wrapper is killed by SIGTERM
        # / SIGINT (e.g. OpenClaw 1MB-buffer SIGKILL cascade).
        #
        # v0.2.13: with start_new_session=True, the subprocess is in
        # its own process group and detached. The wrapper's SIGTERM
        # is not propagated to the subprocess; it continues running.
        # The executor's finally block uses a short wait timeout so
        # the wrapper can exit without blocking on the detached
        # subprocess.
        raise SystemExit(128 + signum)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, OSError):
            # Windows doesn't support add_signal_handler; OSError if not main thread.
            pass

    async def _finalize_session(sig: Optional[int]) -> None:
        """Move session out of ``running`` if it is still stuck there.

        Called from the finally block. Idempotent: if the executor loop
        finished normally (status already COMPLETED / FAILED / KILLED)
        this is a no-op unless ``sig`` is set, in which case we enrich
        the metadata with signal info. If a signal or unrecoverable
        exception killed the loop mid-stream we mark it FAILED with
        signal/error metadata.
        """
        try:
            current = await storage.get_session(session.id)
        except Exception:  # pragma: no cover - best-effort; storage broken
            return
        if current is None:
            return
        if current.status != SessionStatus.RUNNING:
            # v0.2.13: enrich metadata with signal info even if executor
            # already finalized the session (it terminates the subprocess
            # and records exit_code=-15, but not the wrapper's signal).
            if sig is not None:
                merged = dict(current.metadata or {})
                merged["signal"] = sig
                try:
                    merged["signal_name"] = signal.Signals(sig).name
                except (ValueError, AttributeError):
                    pass
                merged["error"] = "wrapper terminated"
                try:
                    await storage.update_session(
                        session.id,
                        metadata=merged,
                        # Normalize: signal-terminated sessions are
                        # conventionally exit_code=-1 in our schema.
                        exit_code=-1,
                    )
                except Exception:
                    pass
            return

        metadata: dict[str, Any] = {"error": "wrapper terminated"}
        if sig is not None:
            metadata["signal"] = sig
            try:
                metadata["signal_name"] = signal.Signals(sig).name
            except (ValueError, AttributeError):
                pass
        elif exit_code is not None:
            # Partial result: executor loop ended without a RESULT event
            # being fully processed (shouldn't normally happen, but be safe).
            metadata["partial_exit_code"] = exit_code
        if error_text:
            metadata["partial_error"] = error_text

        try:
            await storage.update_session(
                session.id,
                status=SessionStatus.FAILED,
                finished_at=datetime.now(timezone.utc),
                exit_code=exit_code if exit_code is not None else -1,
                metadata=metadata,
            )
        except Exception:  # pragma: no cover - best-effort
            # Last-ditch: even if the rich update fails, mark terminal.
            try:
                await storage.update_session(
                    session.id,
                    status=SessionStatus.FAILED,
                    finished_at=datetime.now(timezone.utc),
                )
            except Exception:
                pass

    try:
        try:
            async for event in executor.execute(session.id, command, workdir):
                # All events flow to SQLite. We only act on the terminal ones.
                if event.type == EventType.RESULT:
                    data = json.loads(event.data)
                    exit_code = data.get("exit_code")
                elif event.type == EventType.ERROR:
                    error_text = event.data
        except asyncio.CancelledError:
            # v0.2.13: When asyncio's signal handler raises SystemExit,
            # the running coroutine receives CancelledError instead.
            # We translate it back to SystemExit so the finally block
            # can finalize the session with the signal metadata.
            #
            # We do NOT call executor._terminate_process() here: with
            # start_new_session=True the subprocess is in its own
            # process group and detached. Killing it would defeat the
            # point of v0.2.13's process-group design (the user expects
            # to `tail` the subprocess's output after the wrapper dies).
            # Instead, the executor's finally block uses a short
            # wait_for(self._process.wait(), timeout=0.5) so the
            # wrapper can exit cleanly without blocking on the
            # detached subprocess.
            if _received_signal["signal"] is not None:
                raise SystemExit(128 + _received_signal["signal"])
            raise
        # v0.2.13: If CancelledError was swallowed inside the executor's
        # async generator, the loop exits normally but the signal handler
        # already ran. Surface it as SystemExit so the outer finally
        # records the signal in session metadata.
        if _received_signal["signal"] is not None and exit_code is None:
            error_text = error_text or "wrapper terminated"
            raise SystemExit(128 + _received_signal["signal"])
    except Exception as e:
        console.print(f"[red]Execution error: {e}[/red]")
        error_text = str(e)
        # Finalize now (status -> FAILED). The finally block will see
        # it's no longer RUNNING and skip the duplicate update.
        await _finalize_session(None)
    finally:
        # Always: release registry + finalize + close storage.
        try:
            await registry.release(session.id)
        except Exception:  # pragma: no cover
            pass
        # If the except branch above already finalized, this is a no-op.
        await _finalize_session(_received_signal["signal"])
        # Restore default signal handlers (best-effort; loop may be closing).
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, OSError, RuntimeError):
                pass
        try:
            await storage.close()
        except Exception:  # pragma: no cover
            pass

    # Emit a single bounded result line so the caller knows how it ended.
    result_line = {
        "session_id": session.id,
        "exit_code": exit_code,
        "error": error_text,
    }
    sys.stdout.write(json.dumps(result_line, ensure_ascii=False) + "\n")
    sys.stdout.flush()

    if verbose:
        console.print(f"[dim]full event stream: coding-agents tail {session.id}[/dim]")
