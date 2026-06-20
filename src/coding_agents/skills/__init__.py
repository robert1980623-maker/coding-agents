"""Skill system for coding-agents (agentskill.io compatible)."""

from coding_agents.skills.loader import (
    Skill,
    SkillValidationError,
    get_global_skills_dir,
    get_project_skills_dir,
    load_all_skills,
    load_skill_content,
    parse_skill_md,
)
from coding_agents.skills.injector import build_skills_preamble
from coding_agents.skills.installer import install_skill, remove_skill

__all__ = [
    "Skill",
    "SkillValidationError",
    "build_skills_preamble",
    "get_global_skills_dir",
    "get_project_skills_dir",
    "install_skill",
    "load_all_skills",
    "load_skill_content",
    "parse_skill_md",
    "remove_skill",
]
