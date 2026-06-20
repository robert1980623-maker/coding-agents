"""Skill management CLI subcommands."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from coding_agents.skills.installer import install_skill, remove_skill
from coding_agents.skills.loader import (
    SkillValidationError,
    load_all_skills,
    load_skill_content,
)

app = typer.Typer(
    name="skill",
    help="Manage skills (agentskill.io compatible)",
    no_args_is_help=True,
)
console = Console()


@app.command()
def install(
    source: str = typer.Argument(
        ..., help="URL (.zip, .tar.gz, .tgz) or local file path"
    ),
    global_install: bool = typer.Option(
        False, "--global", "-g", help="Install to global (~/.coding-agents/skills/)"
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite existing skill without asking"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation prompts"
    ),
) -> None:
    """Install a skill from a URL or local file."""
    overwrite: bool | None = force or None  # True if force, else None (prompt)
    if yes:
        overwrite = True

    # Handle interactive overwrite prompt
    if overwrite is None:
        from coding_agents.skills.loader import (
            get_global_skills_dir,
            get_project_skills_dir,
        )

        # Peek at skill name to check if it exists
        try:
            target_dir = (
                get_global_skills_dir() if global_install
                else get_project_skills_dir()
            )
            # We can't know the name without downloading; defer to installer
            # and catch FileExistsError for the prompt
            pass
        except Exception:
            pass

    try:
        skill = install_skill(
            source,
            global_install=global_install,
            overwrite=overwrite,
        )
        if skill is None:
            console.print("[yellow]Installation cancelled.[/yellow]")
            raise typer.Exit(code=0)
        location = "global" if global_install else "project-local"
        console.print(
            f"[green]✓ Installed skill '{skill.name}' ({location})[/green]"
        )
    except FileExistsError as e:
        if overwrite is None and not yes:
            # Interactive prompt
            if typer.confirm(str(e) + ". Overwrite?"):
                try:
                    skill = install_skill(
                        source,
                        global_install=global_install,
                        overwrite=True,
                    )
                    if skill:
                        location = "global" if global_install else "project-local"
                        console.print(
                            f"[green]✓ Installed skill '{skill.name}' ({location})[/green]"
                        )
                    return
                except Exception as e2:
                    console.print(f"[red]Error: {e2}[/red]")
                    raise typer.Exit(code=1)
            else:
                console.print("[yellow]Cancelled.[/yellow]")
                raise typer.Exit(code=0)
        else:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=1)
    except (SkillValidationError, ValueError, FileNotFoundError) as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=1)


@app.command(name="list")
def list_skills() -> None:
    """List installed skills."""
    skills = load_all_skills()
    if not skills:
        console.print("[dim]No skills installed.[/dim]")
        return

    console.print(f"\n[bold]INSTALLED SKILLS ({len(skills)})[/bold]")
    console.print("─" * 60)
    for skill in skills:
        desc = skill.description.replace("\n", " ").strip()
        console.print(f"  [cyan]{skill.name:<25}[/cyan] {desc}")
    console.print("─" * 60)


@app.command()
def show(
    name: str = typer.Argument(..., help="Skill name"),
) -> None:
    """Show the full content of a skill's SKILL.md."""
    skill = load_skill_content(name)
    if skill is None:
        console.print(f"[red]Skill not found: {name}[/red]")
        raise typer.Exit(code=1)
    console.print(skill.content)


@app.command()
def remove(
    name: str = typer.Argument(..., help="Skill name to remove"),
    global_install: bool = typer.Option(
        False, "--global", "-g", help="Remove from global skills"
    ),
) -> None:
    """Remove an installed skill."""
    removed = remove_skill(name, global_install=global_install)
    if removed:
        console.print(f"[green]✓ Removed skill '{name}'[/green]")
    else:
        console.print(f"[red]Skill not found: {name}[/red]")
        raise typer.Exit(code=1)
