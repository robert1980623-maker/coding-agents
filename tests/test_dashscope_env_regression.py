"""Regression test for DashScope environment variable handling.

This test documents the bug fixed in commit 2026-06-23:

** The Bug **
The Claude agent adapter was setting DashScope environment variables
(ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_MODEL) to empty string
instead of deleting them entirely. This caused Claude Code to still see these
variables (as empty strings) and potentially route through DashScope proxy
instead of the native Anthropic API, breaking authentication.

** The Fix **
Added `env_deletions()` method to BaseAgent that returns a list of environment
variable names to delete. The Claude adapter overrides this to return DashScope
vars when they contain "dashscope" in their value. The subprocess spawning code
now uses `env.pop(var, None)` to completely remove these vars from the
environment, rather than setting them to empty string.

** Why This Matters **
When a subprocess is spawned with `env={...}`, setting a var to "" still passes
it to the subprocess as an empty environment variable (e.g., `VAR=`). Claude
Code checks for the presence of these variables (even if empty) and may behave
differently. Deleting the vars entirely ensures Claude Code uses the native
Anthropic API.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coding_agents.agents.claude import ClaudeAgent


class TestDashScopeEnvVarDeletion:
    """Tests for proper deletion of DashScope environment variables."""

    def test_env_deletions_returns_dashscope_vars(self):
        """Verify that env_deletions() returns DashScope vars when present."""
        # Set up environment with DashScope vars
        os.environ['ANTHROPIC_BASE_URL'] = 'https://dashscope.example.com'
        os.environ['ANTHROPIC_AUTH_TOKEN'] = 'dashscope-token-123'
        os.environ['ANTHROPIC_MODEL'] = 'dashscope-model'

        try:
            agent = ClaudeAgent()
            deletions = agent.env_deletions()

            # All three DashScope vars should be marked for deletion
            assert 'ANTHROPIC_BASE_URL' in deletions
            assert 'ANTHROPIC_AUTH_TOKEN' in deletions
            assert 'ANTHROPIC_MODEL' in deletions
        finally:
            # Clean up
            os.environ.pop('ANTHROPIC_BASE_URL', None)
            os.environ.pop('ANTHROPIC_AUTH_TOKEN', None)
            os.environ.pop('ANTHROPIC_MODEL', None)

    def test_env_deletions_case_insensitive(self):
        """Verify that DashScope detection is case-insensitive."""
        # Set up with different cases
        os.environ['ANTHROPIC_BASE_URL'] = 'https://DashScope.example.com'
        os.environ['ANTHROPIC_AUTH_TOKEN'] = 'DASHSCOPE-token'

        try:
            agent = ClaudeAgent()
            deletions = agent.env_deletions()

            assert 'ANTHROPIC_BASE_URL' in deletions
            assert 'ANTHROPIC_AUTH_TOKEN' in deletions
        finally:
            os.environ.pop('ANTHROPIC_BASE_URL', None)
            os.environ.pop('ANTHROPIC_AUTH_TOKEN', None)

    def test_env_deletions_ignores_non_dashscope_vars(self):
        """Verify that non-DashScope vars are not marked for deletion."""
        # Set up with non-DashScope vars
        os.environ['ANTHROPIC_BASE_URL'] = 'https://api.anthropic.com'
        os.environ['ANTHROPIC_AUTH_TOKEN'] = 'sk-ant-123'

        try:
            agent = ClaudeAgent()
            deletions = agent.env_deletions()

            # These should NOT be marked for deletion
            assert 'ANTHROPIC_BASE_URL' not in deletions
            assert 'ANTHROPIC_AUTH_TOKEN' not in deletions
        finally:
            os.environ.pop('ANTHROPIC_BASE_URL', None)
            os.environ.pop('ANTHROPIC_AUTH_TOKEN', None)

    def test_subprocess_env_deletes_dashscope_vars(self):
        """Verify that subprocess spawning deletes DashScope vars entirely."""
        import subprocess

        # Set up environment with DashScope vars
        os.environ['ANTHROPIC_BASE_URL'] = 'https://dashscope.example.com'
        os.environ['ANTHROPIC_AUTH_TOKEN'] = 'dashscope-token'
        os.environ['KEEP_THIS'] = 'value'

        try:
            agent = ClaudeAgent()
            overrides = agent.env_overrides()
            deletions = agent.env_deletions()

            # Simulate subprocess env construction
            env = {**os.environ}
            env.update(overrides)
            for var in deletions:
                env.pop(var, None)

            # Verify DashScope vars are completely absent
            assert 'ANTHROPIC_BASE_URL' not in env
            assert 'ANTHROPIC_AUTH_TOKEN' not in env
            # Verify other vars are preserved
            assert env['KEEP_THIS'] == 'value'

            # Verify with actual subprocess
            result = subprocess.run(
                ['env'],
                env=env,
                capture_output=True,
                text=True,
            )
            # DashScope vars should not appear in subprocess env
            assert 'ANTHROPIC_BASE_URL=' not in result.stdout
            assert 'ANTHROPIC_AUTH_TOKEN=' not in result.stdout
            # Other vars should appear
            assert 'KEEP_THIS=value' in result.stdout
        finally:
            os.environ.pop('ANTHROPIC_BASE_URL', None)
            os.environ.pop('ANTHROPIC_AUTH_TOKEN', None)
            os.environ.pop('KEEP_THIS', None)

    def test_env_overrides_still_sets_home(self):
        """Verify that env_overrides() still correctly sets HOME."""
        original_home = os.environ.get('HOME', '')
        os.environ['HOME'] = '/fake/home'

        try:
            agent = ClaudeAgent()
            overrides = agent.env_overrides()

            # HOME should be overridden to real home
            assert 'HOME' in overrides
            assert overrides['HOME'] != '/fake/home'
        finally:
            if original_home:
                os.environ['HOME'] = original_home
            else:
                os.environ.pop('HOME', None)
