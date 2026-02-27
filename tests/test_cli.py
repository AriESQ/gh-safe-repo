"""Tests for cli.py helpers — focused on _resolve_branches()."""

import subprocess
from unittest.mock import patch

from gh_safe_repo.cli import _resolve_branches
from gh_safe_repo.config_manager import ConfigManager


def make_config(overrides=None):
    config = ConfigManager(config_path="/tmp/nonexistent-gh-safe-repo.ini")
    if overrides:
        config.apply_overrides(overrides)
    return config


class TestResolveBranches:
    def test_post_default_branch_takes_priority(self):
        config = make_config()
        result = _resolve_branches(config, post_default_branch="develop")
        assert result == ["develop"]

    def test_source_default_branch_used_when_no_post(self):
        config = make_config()
        result = _resolve_branches(config, source_default_branch="master")
        assert result == ["master"]

    def test_post_takes_priority_over_source(self):
        config = make_config()
        result = _resolve_branches(
            config, post_default_branch="main", source_default_branch="master"
        )
        assert result == ["main"]

    def test_git_symbolic_ref_used_when_no_post_or_source(self):
        config = make_config()
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="feature-x\n", stderr="")
        with patch("subprocess.run", return_value=completed):
            result = _resolve_branches(config)
        assert result == ["feature-x"]

    def test_git_symbolic_ref_main_branch(self):
        config = make_config()
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="main\n", stderr="")
        with patch("subprocess.run", return_value=completed):
            result = _resolve_branches(config)
        assert result == ["main"]

    def test_falls_back_to_config_when_git_fails(self):
        config = make_config({("branch_protection", "protected_branch"): "trunk"})
        completed = subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="")
        with patch("subprocess.run", return_value=completed):
            result = _resolve_branches(config)
        assert result == ["trunk"]

    def test_falls_back_to_default_when_git_fails_and_config_is_default(self):
        # SAFE_DEFAULTS has "master, main" — both branches returned as fallback
        config = make_config()
        completed = subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="")
        with patch("subprocess.run", return_value=completed):
            result = _resolve_branches(config)
        assert result == ["master", "main"]

    def test_git_subprocess_exception_handled(self):
        config = make_config()
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            result = _resolve_branches(config)
        # Falls back to config default "master, main"
        assert result == ["master", "main"]

    def test_config_single_branch_returned_as_list(self):
        config = make_config({("branch_protection", "protected_branch"): "main"})
        completed = subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="")
        with patch("subprocess.run", return_value=completed):
            result = _resolve_branches(config)
        assert result == ["main"]

    def test_config_comma_separated_branches_parsed(self):
        config = make_config({("branch_protection", "protected_branch"): "master, main, develop"})
        completed = subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="")
        with patch("subprocess.run", return_value=completed):
            result = _resolve_branches(config)
        assert result == ["master", "main", "develop"]

    def test_git_empty_output_falls_back_to_config(self):
        config = make_config({("branch_protection", "protected_branch"): "main"})
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="   \n", stderr="")
        with patch("subprocess.run", return_value=completed):
            result = _resolve_branches(config)
        assert result == ["main"]
