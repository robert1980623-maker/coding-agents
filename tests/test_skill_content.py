"""Tests for SKILL.md content correctness (v0.2.15).

These tests verify that the dispatch and cost skills encode the
"don't pass --model / --budget unless asked" rule (PM Iron Rule #4).
They guard against accidental regression in skill content.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DISPATCH_SKILL = PROJECT_ROOT / ".coding-agents" / "skills" / "coding-agents-dispatch" / "SKILL.md"
COST_SKILL = PROJECT_ROOT / ".coding-agents" / "skills" / "coding-agents-cost" / "SKILL.md"


def _read(path: Path) -> str:
    assert path.exists(), f"missing skill file: {path}"
    return path.read_text(encoding="utf-8")


# === dispatch skill ===

class TestDispatchSkillEncouragesMinimalFlags:
    """The dispatch skill must teach minimal default dispatch."""

    def test_default_section_present(self):
        text = _read(DISPATCH_SKILL)
        assert "Default: dispatch without model or budget" in text

    def test_explicit_dont_pass_model_budget(self):
        text = _read(DISPATCH_SKILL)
        # Hard rule #3 (or equivalent) must forbid unprompted --model/--budget
        assert re.search(
            r"Do NOT pass\s+`--model`\s*/\s*`--budget`",
            text,
        ), "Hard rule must explicitly say 'Do NOT pass --model / --budget'"

    def test_no_unqualified_always_set_budget(self):
        text = _read(DISPATCH_SKILL)
        # The old "Always set --budget" advice must be gone
        assert not re.search(r"\*\*Always set\s+`--budget`\*\*", text), (
            "Old 'Always set --budget' advice must be removed"
        )

    def test_common_mistakes_lists_unprompted_model(self):
        text = _read(DISPATCH_SKILL)
        assert "--model" in text and "default" in text.lower()

    def test_common_mistakes_lists_unprompted_budget(self):
        text = _read(DISPATCH_SKILL)
        # Must mention adding --budget "just in case" as a mistake
        assert re.search(
            r"Add\s+`--budget\s+\d+\`\s+\"just in case\"",
            text,
        ), "Common mistakes should call out unprompted --budget"

    def test_advanced_budget_example_marked_advanced(self):
        text = _read(DISPATCH_SKILL)
        # The --budget example should be labeled "Advanced:" not canonical
        assert "Advanced: budget cap" in text
        # And mention "only when the human asks"
        assert "only when the human asks" in text

    def test_advanced_model_example_marked_advanced(self):
        text = _read(DISPATCH_SKILL)
        assert "Advanced: model override" in text
        assert "only when the human asks" in text


# === cost skill ===

class TestCostSkillDoesNotPushBudget:
    """The cost skill must not push `--budget` as a default."""

    def test_default_section_present(self):
        text = _read(COST_SKILL)
        assert "do not set --budget" in text.lower() or "do not pass --budget" in text.lower()

    def test_hard_rule_rejects_unprompted_budget(self):
        text = _read(COST_SKILL)
        # Hard rule #2 must say "Do NOT set --budget unless the human asks"
        assert re.search(
            r"Do NOT set\s+`--budget`\s+unless",
            text,
        ), "Cost skill must have a 'do not set --budget unless asked' hard rule"

    def test_no_always_set_budget_for_claude(self):
        text = _read(COST_SKILL)
        assert not re.search(
            r"\*\*Always set\s+`--budget`\s+for claude\*\*",
            text,
        ), "Old 'Always set --budget for claude' must be removed"

    def test_multi_stage_budget_marked_as_approved(self):
        text = _read(COST_SKILL)
        # The multi-stage example should require explicit approval
        assert "only when the human approved" in text.lower() or \
               "human approved" in text.lower()


# === regression: both skills must agree ===

class TestSkillsAreConsistent:
    """If dispatch says don't pass --budget, cost must say the same."""

    def test_dispatch_and_cost_agree_on_default(self):
        dispatch = _read(DISPATCH_SKILL)
        cost = _read(COST_SKILL)
        # Both must say "default is uncapped" or equivalent
        assert "default is no budget" in cost.lower() or \
               "default is uncapped" in cost.lower(), (
            "Cost skill must acknowledge the dispatch skill's no-budget default"
        )
        assert "Default: dispatch without model or budget" in dispatch


# === prompt-size guidance ===

class TestDispatchSkillTeachesShortPrompts:
    """The dispatch skill must teach PMs to write short prompts."""

    def test_prompt_guidance_section_present(self):
        text = _read(DISPATCH_SKILL)
        assert "Keep prompts short" in text or "prompts short" in text.lower()

    def test_size_guideline_present(self):
        text = _read(DISPATCH_SKILL)
        assert "> 6 KB" in text or "6 KB" in text, (
            "Dispatch skill must include a size guideline"
        )

    def test_bad_prompt_example_present(self):
        text = _read(DISPATCH_SKILL)
        assert "Bad prompt" in text or "do not do this" in text.lower()

    def test_good_prompt_example_present(self):
        text = _read(DISPATCH_SKILL)
        assert "Good prompt" in text or "do this" in text.lower()

    def test_excludes_code_templates(self):
        text = _read(DISPATCH_SKILL)
        # Should warn against complete code templates
        assert "code templates" in text.lower() or "代码模板" in text or \
               "完整代码" in text, (
            "Dispatch skill must warn against complete code templates"
        )
