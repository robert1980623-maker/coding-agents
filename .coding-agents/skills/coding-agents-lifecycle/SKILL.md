---
name: coding-agents-lifecycle
description: |
  How to inspect, monitor, and garbage-collect coding-agents sessions.
  Use when you need to check session progress, debug failures, or
  reclaim disk space. Covers `tail`, `status`, and `gc` subcommands.
---

# coding-agents-lifecycle

Manage the lifecycle of coding-agents sessions stored in SQLite.

> **v0.2.6+ contract change**: `dispatch` no longer streams output to the CLI.
> It only prints `session_id=<id>` early and one JSON result line at the end.
> Intermediate stdout/stderr events live in SQLite. Use `tail` / `status`
> to read them.

## When to use this skill

- A session is running and you want to check progress without killing it
- You got a `session_id` from `dispatch` and want to see what happened
- You want to debug a failed session (read its stderr)
- Your `~/.coding-agents/data.db` is getting big and you want to clean up
- You want to drop intermediate stdout but keep the final result

## The three commands

### `coding-agents status <session-id>`

Show session metadata (agent, prompt, status, exit code, cost, timestamps,
tags) **plus the last 20 events** (one-line summaries).

```bash
coding-agents status <session-id>
coding-agents status <session-id> --limit 50     # more events
coding-agents status <session-id> --no-events    # metadata only
```

**Why this exists**: The OpenClaw exec wrapper has a 1MB stdout buffer.
`status` is the safe default — even 50 events fit comfortably under 1MB.

### `coding-agents tail <session-id>`

Show the **most recent** events of a session, oldest-first within the
window. Larger default window than `status` (100 vs 20).

```bash
coding-agents tail <session-id>              # last 100 events
coding-agents tail <session-id> --limit 500  # last 500
```

Use `tail` when `status` doesn't give you enough context.

### `coding-agents gc`

Garbage-collect old sessions to keep SQLite bounded.

**Defaults** (designed to be safe to run unattended):
- Drop `completed` / `killed` / `timeout` sessions older than **30 days**
- Drop `failed` sessions older than **7 days**
- Mark `running` sessions with no activity for **24h** as `orphaned` (not deleted)

```bash
coding-agents gc                # use defaults + VACUUM
coding-agents gc --dry-run      # report only, no deletes
coding-agents gc --keep-result-only   # drop stdout/stderr, keep result
coding-agents gc --older-than 14      # more aggressive cutoff
coding-agents gc --no-vacuum          # skip VACUUM (faster)
```

**`--keep-result-only`**: For retained sessions, drop all `stdout` /
`stderr` events but keep the `result` event. Frees disk; you lose the
intermediate output but still have the final answer.

**`--dry-run`**: Always run this first if you're uncertain.

## Reading SQLite directly

For ad-hoc analysis, you can query SQLite directly. The DB lives at
`~/.coding-agents/data.db` (or `$CODING_AGENTS_DB`).

```bash
sqlite3 ~/.coding-agents/data.db "SELECT id, status, started_at FROM sessions ORDER BY started_at DESC LIMIT 10"
```

```sql
-- Find failed sessions in the last 24h
SELECT id, agent, finished_at, metadata
FROM sessions
WHERE status = 'failed'
  AND finished_at > strftime('%s', 'now', '-24 hours');

-- Count events per session
SELECT session_id, COUNT(*) AS n
FROM events
GROUP BY session_id
ORDER BY n DESC
LIMIT 10;
```

## Common workflows

### "Did my long-running dispatch finish?"

```bash
SID=$(...)                                        # from dispatch output
coding-agents status $SID                        # check now
coding-agents tail $SID --follow                 # poll (NOT from inside OpenClaw)
```

### "My data.db is 500MB"

```bash
coding-agents gc --dry-run                      # see what would go
coding-agents gc --older-than 14                # 2 weeks instead of 30 days
coding-agents gc --older-than 14 --keep-result-only  # more aggressive
```

### "I want to free space but keep recent work"

```bash
coding-agents gc --keep-result-only             # keeps 30 days of results
```

## Bounded output contract (important for OpenClaw users)

All three commands (`status`, `tail`, `gc --dry-run`) print **bounded output**:

- `status`: 20 events × ≤200 chars/event = ~4KB typical, never > ~50KB
- `tail`: 100 events × ≤200 chars = ~20KB typical, never > ~250KB
- `gc`: just session IDs, always < 10KB

**Safe to call from inside an OpenClaw exec session.** Do NOT use
`tail --follow` from inside OpenClaw — it will hit the 1MB buffer over time.

## What this skill does NOT cover

- Building/running agents: see `coding-agents-dispatch`
- Recovering crashed sessions: see `coding-agents-recovery`
- Estimating/limiting cost: see `coding-agents-cost`
- Managing skills: see `coding-agents-skills`