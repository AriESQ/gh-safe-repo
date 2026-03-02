"""Tests for cli.py helpers — focused on _resolve_branches() and argument validation."""

import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from gh_safe_repo.cli import _resolve_branches, format_plan_json, main
from gh_safe_repo.config_manager import ConfigManager
from gh_safe_repo.diff import Change, ChangeCategory, ChangeType, Plan
from gh_safe_repo.security_scanner import SecurityScanner


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


class TestLocalFlagValidation:
    def _make_mock_client(self):
        mock_client = MagicMock()
        mock_client.get_owner.return_value = "alice"
        mock_client.get_plan_name.return_value = "free"
        return mock_client

    def test_local_and_from_are_mutually_exclusive(self):
        with patch("sys.argv", [
            "gh-safe-repo", "my-repo",
            "--local", ".", "--from", "other-repo", "--public",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 2

    def test_local_and_audit_are_mutually_exclusive(self):
        with patch("sys.argv", [
            "gh-safe-repo", "my-repo", "--local", ".", "--audit",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 2

    def test_local_nonexistent_path_exits_with_error(self):
        with patch("sys.argv", [
            "gh-safe-repo", "my-repo", "--local", "/nonexistent/path/xyz", "--dry-run",
        ]):
            with patch("gh_safe_repo.cli.GitHubClient") as MockClient:
                MockClient.return_value = self._make_mock_client()
                with pytest.raises(SystemExit) as exc_info:
                    main()
        assert exc_info.value.code == 2


class TestFormatPlanJson:
    def _make_plan(self):
        plan = Plan()
        plan.add(Change(type=ChangeType.ADD,    category=ChangeCategory.REPO,     key="private",     new=True))
        plan.add(Change(type=ChangeType.UPDATE,  category=ChangeCategory.ACTIONS,  key="permissions", old="all", new="none"))
        plan.add(Change(type=ChangeType.DELETE,  category=ChangeCategory.SECURITY, key="auto_fix",    old=True))
        plan.add(Change(type=ChangeType.SKIP,    category=ChangeCategory.SECURITY, key="dependabot",  reason="Requires paid plan"))
        return plan

    def test_output_is_valid_json(self):
        plan = self._make_plan()
        result = json.loads(format_plan_json(plan))
        assert isinstance(result, dict)

    def test_all_four_change_types_present(self):
        plan = self._make_plan()
        result = json.loads(format_plan_json(plan))
        types = {c["type"] for c in result["changes"]}
        assert types == {"add", "update", "delete", "skip"}

    def test_boolean_values_not_serialised_as_strings(self):
        plan = Plan()
        plan.add(Change(type=ChangeType.ADD, category=ChangeCategory.REPO, key="private", new=True))
        result = json.loads(format_plan_json(plan))
        assert result["changes"][0]["new"] is True

    def test_none_values_serialise_as_null(self):
        plan = Plan()
        plan.add(Change(type=ChangeType.ADD, category=ChangeCategory.REPO, key="private", new=True))
        result = json.loads(format_plan_json(plan))
        assert result["changes"][0]["old"] is None
        assert result["changes"][0]["reason"] is None

    def test_summary_counts_match_count_by_type(self):
        plan = self._make_plan()
        result = json.loads(format_plan_json(plan))
        expected = {t.value: n for t, n in plan.count_by_type().items()}
        assert result["summary"] == expected

    def test_skip_change_includes_reason(self):
        plan = Plan()
        plan.add(Change(type=ChangeType.SKIP, category=ChangeCategory.SECURITY, key="dependabot", reason="Requires paid plan"))
        result = json.loads(format_plan_json(plan))
        assert result["changes"][0]["reason"] == "Requires paid plan"

    def test_summary_omits_absent_change_types(self):
        plan = Plan()
        plan.add(Change(type=ChangeType.ADD, category=ChangeCategory.REPO, key="private", new=True))
        result = json.loads(format_plan_json(plan))
        assert "delete" not in result["summary"]
        assert "skip" not in result["summary"]
        assert result["summary"]["add"] == 1


class TestScannerDescriptionInPlan:
    """Scanner description appears in the SCAN change's new field."""

    def _make_mock_client(self):
        mock_client = MagicMock()
        mock_client.get_owner.return_value = "alice"
        mock_client.get_plan_name.return_value = "free"
        mock_client.repo_path.return_value = "/repos/alice/my-repo"
        mock_client.call_api.return_value = (404, {})  # repo doesn't exist
        return mock_client

    def test_scan_plan_entry_includes_scanner_description(self, capsys):
        # In --dry-run --from mode, the SCAN plan entry's new field should
        # include the scanner description in parentheses.
        with patch("sys.argv", [
            "gh-safe-repo", "my-public-repo",
            "--from", "my-private-repo", "--public", "--dry-run",
        ]):
            with patch("gh_safe_repo.cli.GitHubClient") as MockClient:
                mock_client = self._make_mock_client()
                MockClient.return_value = mock_client

                # Patch plugin plan() calls to return empty plans
                with patch("gh_safe_repo.cli.RepositoryPlugin") as MockRepo, \
                     patch("gh_safe_repo.cli.ActionsPlugin") as MockActions, \
                     patch("gh_safe_repo.cli.BranchProtectionPlugin") as MockBP, \
                     patch("gh_safe_repo.cli.SecurityPlugin") as MockSec:

                    for MockPlugin in (MockRepo, MockActions, MockBP, MockSec):
                        instance = MockPlugin.return_value
                        instance.plan.return_value = Plan()

                    # Force scanner to report "regex only" (no trufflehog, no container)
                    original_init = SecurityScanner.__init__
                    def patched_init(self_inner, config, debug=False):
                        original_init(self_inner, config, debug=debug)
                        self_inner._discovery = {"method": "none"}
                    with patch.object(SecurityScanner, "__init__", patched_init):
                        with pytest.raises(SystemExit):
                            main()

        captured = capsys.readouterr()
        # The plan table is printed to stdout; SCAN new field should contain description
        assert "regex only" in captured.out
