"""Skill injector — build system prompt preamble for skills."""

from __future__ import annotations

from coding_agents.skills.loader import Skill


def build_skills_preamble(skills: list[Skill]) -> str:
    """Build the system prompt preamble listing available skills.

    Format follows agentskill.io standard:
        # Available Skills

        The following skills are available. Use them when relevant:

        ## skill-name-1
        Description 1.

        ## skill-name-2
        Description 2.

    If no skills are provided, returns an empty string.
    """
    if not skills:
        return ""

    lines = [
        "# Available Skills",
        "",
        "The following skills are available. Use them when relevant:",
    ]

    for skill in skills:
        lines.append("")
        lines.append(f"## {skill.name}")
        lines.append(skill.description)

    return "\n".join(lines)
