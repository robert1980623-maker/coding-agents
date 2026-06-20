---
name: coding-agents-skills
description: |
  How to manage project-local and global skill directories used by
  coding-agents. Use this skill when you need to install a third-
  party skill, share skills across a team, or update the SKILL.md
  catalog this project ships with.
---

# Coding Agents — Skills (meta)

## When to use this skill

- You want to install a skill from a URL or local archive
- You want to share a skill across a team via a shared global dir
- You're editing the project-shipped skills under
  `.coding-agents/skills/`
- You need to know the search order between project / global

## Skill storage locations

coding-agents looks in two places, in this order:

1. **Project-local**: `<cwd>/.coding-agents/skills/<name>/SKILL.md`
   — wins on name collision
2. **Global**: `~/.coding-agents/skills/<name>/SKILL.md`

You can override either via env:

- `CODING_AGENTS_PROJECT_DIR` — for the project root
- `CODING_AGENTS_SKILLS_DIR` — for the global root

## CLI

```bash
# List all discoverable skills (project first, then global)
coding-agents skill list

# Show a single skill's full SKILL.md
coding-agents skill show <name>

# Install a skill from a URL or .zip / .tar.gz
coding-agents skill install <url-or-path>
coding-agents skill install <url-or-path> --global    # global, not project
coding-agents skill install <url-or-path> --force    # overwrite

# Remove a skill
coding-agents skill remove <name>
coding-agents skill remove <name> --global
```

## How agents see skills

By design (since v0.2.4), coding-agents does **not** inject a skill
list into the agent's system prompt. Claude Code and Codex each
discover skills natively (`~/.claude/skills/`, `AGENTS.md`, etc.).

`coding-agents skill list` is a tool for **operators** to inspect
what's available — not something the agent sees.

## Project-shipped skills (this repo)

This repo ships a small starter set under `.coding-agents/skills/`:

- `coding-agents-dispatch` — how to dispatch a session correctly
- `coding-agents-recovery` — how to recover from crashes
- `coding-agents-cost` — how to budget and monitor cost
- `coding-agents-skills` — this skill (meta)

To add a new one, create a directory + `SKILL.md` and commit it.

## SKILL.md format

The format is the agentskill.io standard:

```yaml
---
name: my-skill
description: |
  One-paragraph summary. State when to use this skill so an agent
  can decide whether to read it.
---

# My Skill

## When to use this skill
…

## How to use
…
```

Rules:
- `name` is kebab-case (lowercase, digits, hyphens only)
- `description` should answer "when would I read this?"
- The body is plain Markdown

## Hard rules

1. **Never inject skill contents into the agent's prompt.** Let
   the agent discover them.
2. **Never commit a skill with secrets** in its content — the
   whole `SKILL.md` is plain text in git.
3. **Keep skills focused.** One skill = one topic, not a kitchen
   sink.

## Related skills

- `coding-agents-dispatch` — what to do with a session
- `coding-agents-recovery` — what to do after a crash
- `coding-agents-cost` — what to set budgets to
