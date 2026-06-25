# AGENTS.md — Available Skills for Dispatched Agents

This file is read automatically by Claude Code, Codex, and other
agents dispatched by `coding-agents` into this repository.

## Dispatch Convention

`coding-agents dispatch` hands the agent:

- a **workdir** (this repository root),
- a **prompt** (the concrete task to perform).

It does **not** pass a list of skills. The agent is expected to
discover what it can do by reading this file (`AGENTS.md`),
`CLAUDE.md`, and any skill pages linked from them.

These are **mandatory conventions** for any dispatched agent:
before starting work, scan the skills below and read the ones whose
trigger conditions match the task you were given.

## Available Skills

| Skill | One-line description | Path |
|---|---|---|
| `coding-agents-dispatch` | How to correctly dispatch a coding-agents session for a project task (workdir, prompt, isolation, handoff). | `.coding-agents/skills/coding-agents-dispatch/SKILL.md` |
| `coding-agents-lifecycle` | How to inspect, monitor, and garbage-collect coding-agents sessions (status, logs, GC). | `.coding-agents/skills/coding-agents-lifecycle/SKILL.md` |
| `coding-agents-recovery` | How to detect, diagnose, and recover from crashed or orphaned sessions. Auto-cleanup (v0.2.32+) handles stuck sessions automatically via `status` and `poll`. Use `recover` for a full database scan. | `.coding-agents/skills/coding-agents-recovery/SKILL.md` |
| `coding-agents-cost` | How to estimate, monitor, and cap the cost/token budget of a coding-agents session. | `.coding-agents/skills/coding-agents-cost/SKILL.md` |
| `coding-agents-skills` | How to manage project-local and global skill directories used by coding-agents. | `.coding-agents/skills/coding-agents-skills/SKILL.md` |

Each row's **Path** is relative to this file (the repository root).
Read the full `SKILL.md` before acting on that skill's domain — the
table entry is only a trigger hint, not the skill itself.

## Triggers

Skill names are also valid slash-commands for interactive use:

- `/coding-agents-dispatch` — before dispatching a new task
- `/coding-agents-lifecycle` — when checking a running session
- `/coding-agents-recovery` — when a session is stuck or orphaned
- `/coding-agents-cost` — when budgeting or capping spend
- `/coding-agents-skills` — when adding/removing a skill

## Sources of truth

- **Project-local skills**: `.coding-agents/skills/<name>/SKILL.md`
  (this repo, git-tracked). Edit these directly.
- **Claude Code mirror**: `.claude/skills/<name>/SKILL.md` is a
  **symlink** to the same file, so Claude Code's native skill
  discovery picks it up. Never edit the symlink target.
- **User-global skills**: `~/.claude/skills/`, `~/.codex/skills/`
  are reserved for personal, non-shared skills. Do not put
  project conventions there.

## Release Checklist

When bumping the version or making a release, **always run `./install.sh`** to update the system installation:

```bash
# 1. Update version in pyproject.toml
# 2. Commit and push
git add pyproject.toml && git commit -m "chore: bump version to X.Y.Z"
git push origin main

# 3. Create and push tag
git tag vX.Y.Z
git push origin vX.Y.Z

# 4. **IMPORTANT**: Reinstall to update system binary
./install.sh
```

The `install.sh` script:
- Creates/updates a dedicated venv at `~/.local/share/coding-agents`
- Installs the package from source
- Creates a wrapper at `~/.local/bin/coding-agents`

**Why this is required**: The system binary at `~/.local/bin/coding-agents` points to the venv. After code changes, the venv must be refreshed to pick up the new version.
