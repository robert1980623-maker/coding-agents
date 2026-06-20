"""Tests for the skills module (agentskill.io standard)."""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import pytest

from coding_agents.skills.injector import build_skills_preamble
from coding_agents.skills.loader import (
    KEBAB_CASE_RE,
    Skill,
    SkillValidationError,
    _parse_skill_content,
)


# === loader tests ===

VALID_SKILL_MD = """---
name: test-skill
description: |
  A test skill for unit testing.
---

# Test Skill

Some instructions here.
"""

INVALID_NAME_SKILL_MD = """---
name: Invalid_Name!
description: A skill with bad name.
---

# Bad
"""

MISSING_NAME_SKILL_MD = """---
description: A skill missing name field.
---

# No name
"""


def test_kebab_case_regex():
    assert KEBAB_CASE_RE.match("hello-world")
    assert KEBAB_CASE_RE.match("foo")
    assert KEBAB_CASE_RE.match("a1b2")
    assert not KEBAB_CASE_RE.match("Hello")
    assert not KEBAB_CASE_RE.match("hello_world")
    assert not KEBAB_CASE_RE.match("hello world")


def test_parse_skill_md_valid(tmp_path: Path):
    skill = _parse_skill_content(VALID_SKILL_MD, tmp_path)
    assert skill.name == "test-skill"
    assert "test skill" in skill.description.lower()
    assert "# Test Skill" in skill.content


def test_parse_skill_md_invalid_name(tmp_path: Path):
    with pytest.raises(SkillValidationError):
        _parse_skill_content(INVALID_NAME_SKILL_MD, tmp_path)


def test_parse_skill_md_missing_name(tmp_path: Path):
    with pytest.raises(SkillValidationError):
        _parse_skill_content(MISSING_NAME_SKILL_MD, tmp_path)


def test_parse_skill_md_no_frontmatter(tmp_path: Path):
    with pytest.raises(SkillValidationError):
        _parse_skill_content("# Just a heading\nNo frontmatter", tmp_path)


# === injector tests ===

def test_injector_empty():
    assert build_skills_preamble([]) == ""


def test_injector_single_skill():
    skills = [Skill(name="foo", description="does foo things", content="...", path=Path("/tmp"))]
    result = build_skills_preamble(skills)
    assert "# Available Skills" in result
    assert "## foo" in result
    assert "does foo things" in result


def test_injector_multiple_skills():
    skills = [
        Skill(name="alpha", description="first skill", content="...", path=Path("/tmp")),
        Skill(name="beta", description="second skill", content="...", path=Path("/tmp")),
    ]
    result = build_skills_preamble(skills)
    assert "## alpha" in result
    assert "## beta" in result
    assert "first skill" in result
    assert "second skill" in result


# === installer tests (with env var override) ===

def test_install_skill_from_zip(tmp_path: Path, monkeypatch):
    """Install a skill from a local .zip to a temp project dir."""
    import os
    from coding_agents.skills.installer import install_skill

    # Redirect project skills dir to a temp location
    monkeypatch.setenv("CODING_AGENTS_PROJECT_DIR", str(tmp_path))
    
    # Create a fake skill zip
    skill_md = VALID_SKILL_MD
    
    zip_path = tmp_path / "myskill.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("myskill/SKILL.md", skill_md)
    
    skill = install_skill(str(zip_path), global_install=False, overwrite=True)
    
    assert skill is not None
    assert skill.name == "test-skill"
    target = tmp_path / ".coding-agents" / "skills" / "test-skill"
    assert (target / "SKILL.md").exists()


def test_install_skill_from_tarball(tmp_path: Path, monkeypatch):
    from coding_agents.skills.installer import install_skill

    monkeypatch.setenv("CODING_AGENTS_PROJECT_DIR", str(tmp_path))
    
    skill_md = VALID_SKILL_MD
    
    tar_path = tmp_path / "skill.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        import io
        data = skill_md.encode("utf-8")
        info = tarfile.TarInfo(name="tarball_skill/SKILL.md")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    
    skill = install_skill(str(tar_path), global_install=False, overwrite=True)
    
    assert skill is not None
    assert skill.name == "test-skill"


def test_install_skill_rollback_on_invalid(tmp_path: Path, monkeypatch):
    """Installing an invalid skill should not leave partial files."""
    from coding_agents.skills.installer import install_skill

    monkeypatch.setenv("CODING_AGENTS_PROJECT_DIR", str(tmp_path))
    
    # Create a zip with no SKILL.md
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("bad/README.md", "not a skill")
    
    with pytest.raises((SkillValidationError, ValueError, FileNotFoundError)):
        install_skill(str(zip_path), global_install=False, overwrite=True)
    
    # The skills dir may exist but should not contain the broken skill
    skills_dir = tmp_path / ".coding-agents" / "skills"
    if skills_dir.exists():
        # Either empty, or no "bad" subdir
        assert not (skills_dir / "bad").exists()


def test_remove_skill(tmp_path: Path, monkeypatch):
    from coding_agents.skills.installer import install_skill, remove_skill

    monkeypatch.setenv("CODING_AGENTS_PROJECT_DIR", str(tmp_path))
    
    skill_md = VALID_SKILL_MD
    
    zip_path = tmp_path / "removeme.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("removeme/SKILL.md", skill_md)
    
    install_skill(str(zip_path), global_install=False, overwrite=True)
    
    target = tmp_path / ".coding-agents" / "skills" / "test-skill"
    assert target.exists()
    
    removed = remove_skill("test-skill", global_install=False)
    assert removed is True
    assert not target.exists()


# === CLI integration ===

def test_cli_skill_help():
    """Verify the skill subcommand is registered with the main app."""
    import typer.main
    from coding_agents.cli import app
    from coding_agents.cli_skill import app as skill_app
    
    # Get the underlying click group
    click_group = typer.main.get_command(app)
    
    # Check that the skill subcommand is registered
    assert "skill" in click_group.commands
    
    # Check that skill_app has the 4 expected subcommands
    skill_click = typer.main.get_command(skill_app)
    for cmd in ["install", "list", "show", "remove"]:
        assert cmd in skill_click.commands, f"Missing skill command: {cmd}"

