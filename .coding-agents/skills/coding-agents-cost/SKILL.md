---
name: coding-agents-cost
description: |
  How to estimate, monitor, and cap the cost of coding-agents
  sessions. Use this skill when you need to decide on a budget,
  track spend across runs, or design cost-aware batch pipelines.
---

# Coding Agents — Cost

## When to use this skill

- You're dispatching a session and need to set a sensible `--budget`
- You're designing a batch / pipeline and need per-task cost caps
- You want to compare agent / model costs across runs
- A session ran out of budget and you need to understand why

## How budget is enforced

`coding-agents dispatch ... --budget N` translates to the agent CLI's
`--max-budget-usd N` flag. The agent CLI is the source of truth — it
aborts the run when the spend hits N. coding-agents itself does not
add a second gate.

## Recommended budgets by task class

| Task class | Typical budget (USD) | Notes |
| --- | --- | --- |
| Trivial: typo, single-line fix | 0.30 – 0.50 | Will often under-shoot |
| Small: function-level refactor | 1.00 – 2.00 | |
| Medium: feature implementation | 2.00 – 5.00 | |
| Large: refactor across many files | 5.00 – 10.00 | Watch the live output |
| Investigation / read-only analysis | 1.00 – 3.00 | Usually cheap |
| "Fix all the warnings" | 3.00 – 8.00 | Can spiral if alerts are noisy |

When in doubt, **start low and re-dispatch** rather than over-budgeting.

## How to track cost across runs

```bash
# Show cost of a specific session (last 'result' event)
coding-agents status <session_id>

# List all sessions with cost info
coding-agents list

# Search for sessions over a cost threshold
coding-agents search "cost_usd:>5"
```

## Cost-aware patterns

### Cheap re-tries
```bash
# Quick: try with tight budget first
coding-agents dispatch claude "fix bug" --workdir ~/project --budget 0.50
# If it ran out, the events will tell you why
```

### Multi-stage pipeline
```bash
# Stage 1: cheap exploration
coding-agents dispatch claude "find the bug" --workdir ~/project --budget 0.30

# Stage 2: targeted fix (only if stage 1 found something)
coding-agents dispatch claude "fix the bug" --workdir ~/project --budget 1.00
```

## Hard rules

1. **Always set `--budget`** for any non-trivial task. Even if the
   expected cost is small, the budget is a safety net against prompt
   injection / agent loop bugs.
2. **Don't over-budget** to "save time re-dispatching" — a runaway
   session will burn the full budget before aborting.
3. **Re-read events before re-budgeting** if a session ran out. The
   events often show what was wasted.

## Related skills

- `coding-agents-dispatch` — how to set `--budget` on dispatch
- `coding-agents-recovery` — what to do when budget runs out
