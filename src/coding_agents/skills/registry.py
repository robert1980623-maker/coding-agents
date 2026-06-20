"""Skill registry — convenience functions for skill discovery."""

from __future__ import annotations

from coding_agents.skills.loader import (
    Skill,
    load_all_skills,
    load_skill_content,
)

__all__ = [
    "Skill",
    "load_all_skills",
    "load_skill_content",
]
