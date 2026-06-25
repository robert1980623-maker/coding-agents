---
name: coding-agents-recovery
description: |
  How to detect, diagnose, and recover from crashed / orphaned
  coding-agents sessions. Use this skill when a session is stuck in
  "running" status, a previous run died mid-task, or you need to
  figure out what happened to a session that never completed.

  Note: Auto-cleanup (v0.2.32+) handles stuck sessions automatically
  via `status` and `poll`. Use this skill for a full database scan
  or when auto-cleanup has been disabled.
---

# Coding Agents — Recovery

## When to use this skill

- A session is stuck in `running` status but the agent process is gone
- A previous run died because the parent terminal / SSH session closed
- The agent's last event is `stdout` / `stderr` with no `result`
- You need to know whether a session is still alive before re-running

## How to detect orphans

```bash
# List all running sessions
coding-agents list --status running

# Check a specific session's events
coding-agents search "session:<session_id>"

# Run a recovery scan (marks orphans as failed)
coding-agents recover
```

`coding-agents recover` reads each `running` session's heartbeat
and, if stale, marks it `failed` with an `error: "orphaned"` metadata
entry. It does **not** restart the session.

## Common causes

| Cause | Symptom | Fix |
| --- | --- | --- |
| SSH disconnect / terminal closed | `running` for >1h, no events | `coding-agents recover` then re-dispatch |
| Process killed by user (`kill -9`) | Status flips to `failed` automatically | Inspect events, re-dispatch |
| `coding-agents serve` killed mid-request | HTTP SSE stream cuts off | Client re-subscribes with `last_event_seq` |
| Agent CLI ran out of budget | `result` event with `cost_usd` near `--budget` | Increase budget, re-dispatch |
| Disk full / SQLite lock | Storage errors in events | Free disk, restart |

## Re-dispatching after a crash

```bash
# Find the failed session
coding-agents list --status failed

# Inspect what happened
coding-agents search "session:<session_id>"

# Re-dispatch with a fresh budget, same workdir
coding-agents dispatch claude "continue from where you left off" \
  --workdir ~/project \
  --budget 3.0
```

## Hard rules

1. **Never re-run with a larger budget** without first reading the
   events. The same bug may burn the larger budget too.
2. **Always use the same `--workdir`** when resuming — the agent
   relies on the project's local state.
3. **Tag the new session** (`--tag`) with `resumed-from:<old_id>` so
   you can correlate the chain.

## Related skills

- `coding-agents-dispatch` — the canonical run command
- `coding-agents-cost` — how to budget and monitor cost
