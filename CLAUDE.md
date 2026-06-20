# CLAUDE.md — Claude Code, see AGENTS.md

This file is the Claude Code entry point for this repository.
For the list of skills available to dispatched agents, and the
dispatch convention (workdir + prompt, no skill list), see
[`AGENTS.md`](./AGENTS.md).

## Claude Code–specific notes

- **Invoking a skill interactively.** From inside a Claude Code
  session, type `/skill-name` (e.g. `/coding-agents-dispatch`) to
  invoke a skill. The skill pages are discovered via the
  `.claude/skills/` view — see below.
- **Skill mirror.** `.claude/skills/` is a **symlink view** over
  `.coding-agents/skills/`. The source of truth lives under
  `.coding-agents/skills/<name>/SKILL.md`; the `.claude/skills/`
  entries are symlinks so Claude Code's native skill discovery
  picks them up without us having to maintain two copies.
- **Editing a skill.** Always edit
  `.coding-agents/skills/<name>/SKILL.md` directly. The symlink
  under `.claude/skills/` will reflect the change automatically.
- **Do not duplicate skill content** into this file — reference the
  SKILL.md path instead, so there is exactly one place to update.
