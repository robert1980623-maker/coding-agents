"""Skill installer — download, extract, validate and install skills."""

from __future__ import annotations

import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from coding_agents.skills.loader import (
    Skill,
    SkillValidationError,
    _parse_skill_content,
    get_global_skills_dir,
    get_project_skills_dir,
)


def install_skill(
    source: str,
    *,
    global_install: bool = False,
    overwrite: bool | None = None,
) -> Skill:
    """Install a skill from a URL or local file.

    Args:
        source: URL (http/https) or local file path (.zip, .tar.gz, .tgz)
        global_install: If True, install to ~/.coding-agents/skills/
        overwrite: True=overwrite without asking, False=raise if exists,
                   None=prompt user (returns None if declined)

    Returns:
        The installed Skill, or None if user declined overwrite prompt.

    Raises:
        SkillValidationError: If the skill archive is invalid.
        ValueError: If the source format is unsupported.
        FileExistsError: If skill exists and overwrite=False.
    """
    # Determine target directory
    if global_install:
        target_dir = get_global_skills_dir()
    else:
        target_dir = get_project_skills_dir()

    target_dir.mkdir(parents=True, exist_ok=True)

    # Download / copy to temp file
    tmp_dir = tempfile.mkdtemp(prefix="coding-agents-skill-")
    try:
        tmp_file = _download_source(source, tmp_dir)
        extract_dir = Path(tmp_dir) / "extracted"
        extract_dir.mkdir()
        _extract_archive(tmp_file, extract_dir)

        # Find skill root (handle archives with/without wrapper directory)
        skill_root = _find_skill_root(extract_dir)

        # Validate SKILL.md
        skill_md_path = skill_root / "SKILL.md"
        if not skill_md_path.exists():
            raise SkillValidationError(
                "No SKILL.md found in the archive. "
                "Expected a directory containing SKILL.md."
            )

        content = skill_md_path.read_text(encoding="utf-8")
        skill = _parse_skill_content(content, skill_root)

        # Check if already exists
        dest = target_dir / skill.name
        if dest.exists():
            if overwrite is False:
                raise FileExistsError(f"Skill '{skill.name}' is already installed")
            if overwrite is None:
                # Prompt handled by CLI; from library code, default to raise
                raise FileExistsError(
                    f"Skill '{skill.name}' is already installed. "
                    "Use overwrite=True to replace."
                )
            # overwrite=True: remove old
            shutil.rmtree(dest)

        # Move to destination
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(skill_root), str(dest))

        # Re-read from final location
        return _parse_skill_content(content, dest)

    except Exception:
        # Clean up temp on any failure
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    # Clean up temp on success
    shutil.rmtree(tmp_dir, ignore_errors=True)


def remove_skill(name: str, *, global_install: bool = False) -> bool:
    """Remove an installed skill.

    Returns True if removed, False if not found.
    """
    if global_install:
        skill_dir = get_global_skills_dir() / name
    else:
        # Check project-local first, then global
        skill_dir = get_project_skills_dir() / name
        if not skill_dir.exists():
            skill_dir = get_global_skills_dir() / name

    if skill_dir.exists():
        shutil.rmtree(skill_dir)
        return True
    return False


def _download_source(source: str, tmp_dir: str) -> Path:
    """Download or copy source to a temp file. Returns the file path."""
    if source.startswith(("http://", "https://")):
        # Determine extension from URL
        ext = ""
        url_lower = source.lower()
        if url_lower.endswith(".tar.gz") or url_lower.endswith(".tgz"):
            ext = ".tar.gz"
        elif url_lower.endswith(".zip"):
            ext = ".zip"

        tmp_file = Path(tmp_dir) / f"download{ext}"
        urllib.request.urlretrieve(source, str(tmp_file))  # noqa: S310
        return tmp_file
    else:
        src = Path(source)
        if not src.exists():
            raise FileNotFoundError(f"Source file not found: {source}")
        tmp_file = Path(tmp_dir) / src.name
        shutil.copy2(str(src), str(tmp_file))
        return tmp_file


def _extract_archive(archive_path: Path, extract_dir: Path) -> None:
    """Extract a zip or tar.gz archive."""
    name = archive_path.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(extract_dir)
    elif name.endswith(".tar.gz") or name.endswith(".tgz"):
        with tarfile.open(archive_path, "r:gz") as tf:
            # Safety: filter out absolute paths and path traversal
            for member in tf.getmembers():
                if member.name.startswith("/") or ".." in member.name:
                    raise SkillValidationError(
                        f"Unsafe path in archive: {member.name}"
                    )
            tf.extractall(extract_dir, filter="data")
    else:
        raise ValueError(
            f"Unsupported archive format: {archive_path.name}. "
            "Supported: .zip, .tar.gz, .tgz"
        )


def _find_skill_root(extract_dir: Path) -> Path:
    """Find the actual skill directory within the extracted archive.

    If the archive has a single top-level directory, return that.
    Otherwise, return the extract directory itself.
    """
    entries = list(extract_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extract_dir
