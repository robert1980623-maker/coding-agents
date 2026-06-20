"""Skill loader — scan and parse SKILL.md files."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

KEBAB_CASE_RE = re.compile(r"^[a-z0-9-]+$")


class SkillValidationError(ValueError):
    """Raised when a SKILL.md is invalid."""


@dataclass
class Skill:
    """A loaded skill."""

    name: str
    description: str
    content: str  # Full SKILL.md content
    path: Path  # Directory containing SKILL.md


def get_project_skills_dir() -> Path:
    """Get the project-local skills directory."""
    override = os.environ.get("CODING_AGENTS_PROJECT_DIR")
    if override:
        return Path(override) / ".coding-agents" / "skills"
    return Path.cwd() / ".coding-agents" / "skills"


def get_global_skills_dir() -> Path:
    """Get the global skills directory."""
    override = os.environ.get("CODING_AGENTS_SKILLS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".coding-agents" / "skills"


def parse_skill_md(skill_dir: Path) -> Skill:
    """Parse a SKILL.md file and return a Skill object.

    Validates:
    - SKILL.md exists
    - YAML frontmatter has 'name' and 'description'
    - name is kebab-case
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        raise SkillValidationError(f"SKILL.md not found in {skill_dir}")

    content = skill_md.read_text(encoding="utf-8")
    return _parse_skill_content(content, skill_dir)


def _parse_skill_content(content: str, skill_dir: Path) -> Skill:
    """Parse SKILL.md content string into a Skill object."""
    if not content.startswith("---"):
        raise SkillValidationError("SKILL.md must start with YAML frontmatter (---)")

    parts = content.split("---", 2)
    if len(parts) < 3:
        raise SkillValidationError("SKILL.md must have closing --- for frontmatter")

    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        raise SkillValidationError(f"Invalid YAML frontmatter: {e}") from e

    if not isinstance(meta, dict):
        raise SkillValidationError("Frontmatter must be a YAML mapping")

    name = meta.get("name")
    description = meta.get("description")

    if not name:
        raise SkillValidationError("Frontmatter missing 'name'")
    if not description:
        raise SkillValidationError("Frontmatter missing 'description'")

    name = str(name).strip()
    if not KEBAB_CASE_RE.match(name):
        raise SkillValidationError(
            f"Skill name '{name}' must be kebab-case (^[a-z0-9-]+$)"
        )

    description = str(description).strip()

    return Skill(name=name, description=description, content=content, path=skill_dir)


def load_all_skills() -> list[Skill]:
    """Load all available skills.

    Project-local skills take priority over global skills with the same name.
    """
    skills: dict[str, Skill] = {}

    # Global first (lower priority)
    global_dir = get_global_skills_dir()
    if global_dir.exists():
        for skill_dir in sorted(global_dir.iterdir()):
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                try:
                    skill = parse_skill_md(skill_dir)
                    skills[skill.name] = skill
                except SkillValidationError:
                    pass  # Skip invalid skills silently

    # Project-local (higher priority, overrides global)
    project_dir = get_project_skills_dir()
    if project_dir.exists():
        for skill_dir in sorted(project_dir.iterdir()):
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                try:
                    skill = parse_skill_md(skill_dir)
                    skills[skill.name] = skill
                except SkillValidationError:
                    pass

    return sorted(skills.values(), key=lambda s: s.name)


def load_skill_content(name: str) -> Optional[Skill]:
    """Load a single skill by name.

    Project-local skills take priority over global.
    """
    # Check project-local first
    project_dir = get_project_skills_dir()
    project_skill_dir = project_dir / name
    if project_skill_dir.is_dir() and (project_skill_dir / "SKILL.md").exists():
        try:
            return parse_skill_md(project_skill_dir)
        except SkillValidationError:
            pass

    # Then global
    global_dir = get_global_skills_dir()
    global_skill_dir = global_dir / name
    if global_skill_dir.is_dir() and (global_skill_dir / "SKILL.md").exists():
        try:
            return parse_skill_md(global_skill_dir)
        except SkillValidationError:
            pass

    return None
