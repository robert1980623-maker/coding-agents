---
name: coding-agents-cost
description: |
  How to estimate, monitor, and cap the cost of coding-agents
  sessions. Use this skill when you need to decide on a budget,
  track spend across runs, or design cost-aware batch pipelines.
---

# Coding Agents — Cost

## When to use this skill

- Before dispatching a session (picking a sensible `--budget`)
- After seeing a budget-related warning on dispatch
- When you want to understand historical cost patterns

## How billing works per agent

Not every agent honors `--budget`. Know the difference before you set one.

| Agent | Billing | `--budget` behavior |
| --- | --- | --- |
| `claude` (Claude Code) | Subscription, with a per-run hard cap | Translated to `--max-budget-usd N`. The CLI aborts the run when spend hits N. **Always set this.** |
| `codex` (Codex CLI) | Subscription, **no per-run cap concept** | **Ignored.** v0.2.8+ emits a warning: codex has no `--max-budget-usd` flag. The run is unbounded. |

Rule of thumb: don't assume the agent will stop at your budget. For
codex, cost control is your responsibility via prompt scope and
`--workdir`.

## Estimating cost from history

The SQLite DB at `~/.coding-agents/data.db` has a `sessions` table
with `cost_usd` and `duration_ms` per run.

```bash
# Top 10 most expensive completed sessions
sqlite3 ~/.coding-agents/data.db \
  "SELECT id, agent, cost_usd, duration_ms/1000 AS sec
   FROM sessions WHERE status='completed'
   ORDER BY cost_usd DESC LIMIT 10;"
```

Rough benchmarks (Claude Code, real-world runs):

- Review / small read-only analysis: **$0.30 – $1**
- Single-file change / focused fix: **$0.50 – $2**
- Medium feature implementation: **$2 – $5**
- Complex multi-file / multi-step work: **$5 – $20**

When in doubt, start low and re-dispatch — overspending on a tight
task is more common than running out on a generous one.

## Controlling cost

### Always scope the workdir

```bash
coding-agents dispatch claude "fix the login bug" \
  --workdir ~/projects/my-app --budget 2
```

`--workdir` is the single biggest cost lever. Without it the agent
sees the whole repo and burns tokens reading unrelated files.

### Pick budget by task complexity

```bash
# Simple review / single-file change
--budget 2

# Medium feature implementation
--budget 5

# Complex multi-step / cross-file work
--budget 10   # or 15-20 for genuinely large tasks
```

**Don't default to `--budget 20`.** A review task with a $20 cap will
happily spend $8-12 exploring corners it doesn't need to. Match the
cap to the task.

### Multi-stage for expensive work

```bash
# Stage 1: cheap exploration — find the bug
coding-agents dispatch claude "locate the login regression" \
  --workdir ~/projects/my-app --budget 1

# Stage 2: targeted fix, only if stage 1 found something
coding-agents dispatch claude "fix: <summary from stage 1>" \
  --workdir ~/projects/my-app --budget 3
```

Cheaper overall than one open-ended $10 run.

## Inspecting cost of a running / finished session

```bash
# Quick summary including cost
coding-agents status <session_id> --no-events

# Last N events — each includes token + cost deltas
coding-agents tail <session_id> --limit 50

# Prune stored events, keep only the final result (saves disk)
coding-agents gc --keep-result-only
```

## Hard rules

1. **Always pass `--workdir`** — it's the cheapest win.
2. **Always set `--budget` for claude** — safety net against runaway
   loops or prompt injection.
3. **Don't set `--budget` for codex expecting a cap** — it's ignored;
   you'll just see a warning.
4. **Match budget to task size.** Review ≠ implementation ≠ refactor.
5. **Re-read events before re-budgeting** if a session ran out — the
   events usually show where tokens were wasted.

## Related skills

- `coding-agents-dispatch` — dispatch syntax and agent selection
- `coding-agents-lifecycle` — status / tail / gc reference
- `coding-agents-recovery` — what to do when a session goes wrong
