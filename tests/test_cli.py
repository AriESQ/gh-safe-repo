"""Tests for CLI subcommands and shared helpers."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from gh_safe_repo.commands._common import (
    _resolve_branches,
    build_context,
    format_plan_json,
    parse_repo_arg,
)
from gh_safe_repo.cli import main
from gh_safe_repo.commands import create, fix, scan
from gh_safe_repo.config_manager import ConfigManager
from gh_safe_repo.diff import Change, ChangeCategory, ChangeType, Plan
from gh_safe_repo.security_scanner import SecurityScanner


def make_config(overrides=None):
    config = ConfigManager(config_path="/tmp/nonexistent-gh-safe-repo.ini")
    if overrides:
        config.apply_overrides(overrides)
    return config


class TestParseRepoArg:
    def test_valid_owner_repo(self):
        owner, repo = parse_repo_arg("alice/my-repo")
        assert owner == "alice"
        assert repo == "my-repo"

    def test_bare_name_exits(self):
        with pytest.raises(SystemExit) as exc_info:
            parse_repo_arg("my-repo")
        assert exc_info.value.code == 2

    def test_empty_owner_exits(self):
        with pytest.raises(SystemExit) as exc_info:
            parse_repo_arg("/my-repo")
        assert exc_info.value.code == 2

    def test_empty_repo_exits(self):
        with pytest.raises(SystemExit) as exc_info:
            parse_repo_arg("alice/")
        assert exc_info.value.code == 2

    def test_owner_with_nested_slash(self):
        owner, repo = parse_repo_arg("alice/my/repo")
        assert owner == "alice"
        assert repo == "my/repo"


class TestBuildContext:
    def _make_args(self):
        args = MagicMock()
        args.config = None
        args.debug = False
        return args

    @patch("gh_safe_repo.commands._common.GitHubClient")
    @patch("gh_safe_repo.commands._common.ConfigManager")
    def test_owner_case_insensitive(self, MockConfig, MockClient):
        """Owner comparison should be case-insensitive (GitHub usernames are)."""
        mock_client = MagicMock()
        mock_client.get_owner.return_value = "AriESQ"
        mock_client.get_plan_name.return_value = "free"
        MockClient.return_value = mock_client
        MockConfig.return_value = MagicMock()

        # Should NOT exit — "ariesq" matches "AriESQ" case-insensitively
        ctx = build_context(self._make_args(), expected_owner="ariesq")
        assert ctx.owner == "AriESQ"

    @patch("gh_safe_repo.commands._common.GitHubClient")
    @patch("gh_safe_repo.commands._common.ConfigManager")
    def test_owner_mismatch_exits(self, MockConfig, MockClient):
        """Genuinely different owners should still be rejected."""
        mock_client = MagicMock()
        mock_client.get_owner.return_value = "alice"
        mock_client.get_plan_name.return_value = "free"
        MockClient.return_value = mock_client
        MockConfig.return_value = MagicMock()

        with pytest.raises(SystemExit) as exc_info:
            build_context(self._make_args(), expected_owner="bob")
        assert exc_info.value.code == 1

    @patch("gh_safe_repo.commands._common.GitHubClient")
    @patch("gh_safe_repo.commands._common.ConfigManager")
    def test_owner_mismatch_allowed_when_not_required(self, MockConfig, MockClient):
        """With require_owner_match=False, different owners should be accepted."""
        mock_client = MagicMock()
        mock_client.get_owner.return_value = "alice"
        mock_client.get_plan_name.return_value = "free"
        MockClient.return_value = mock_client
        MockConfig.return_value = MagicMock()

        ctx = build_context(
            self._make_args(), expected_owner="some-org",
            require_owner_match=False,
        )
        assert ctx.owner == "alice"


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

    def test_falls_back_to_config(self):
        config = make_config({("branch_protection", "protected_branch"): "trunk"})
        result = _resolve_branches(config)
        assert result == ["trunk"]

    def test_falls_back_to_default_config(self):
        config = make_config()
        result = _resolve_branches(config)
        assert result == ["master", "main"]

    def test_config_single_branch_returned_as_list(self):
        config = make_config({("branch_protection", "protected_branch"): "main"})
        result = _resolve_branches(config)
        assert result == ["main"]

    def test_config_comma_separated_branches_parsed(self):
        config = make_config({("branch_protection", "protected_branch"): "master, main, develop"})
        result = _resolve_branches(config)
        assert result == ["master", "main", "develop"]


class TestCreateFlagValidation:
    def test_local_and_from_are_mutually_exclusive(self):
        with patch("sys.argv", [
            "gh-safe-repo", "create", "alice/my-repo",
            "--local", ".", "--from", "alice/other-repo", "--public",
        ]):
            with patch("gh_safe_repo.commands.create.build_context") as mock_ctx:
                mock_ctx.return_value = MagicMock(
                    client=MagicMock(), owner="alice", plan_name="free",
                    is_paid_plan=False, config=make_config(),
                )
                with pytest.raises(SystemExit) as exc_info:
                    main()
        assert exc_info.value.code == 2

    def test_local_nonexistent_path_exits_with_error(self):
        with patch("sys.argv", [
            "gh-safe-repo", "create", "alice/my-repo",
            "--local", "/nonexistent/path/xyz", "--dry-run",
        ]):
            with patch("gh_safe_repo.commands.create.build_context") as mock_ctx:
                mock_ctx.return_value = MagicMock(
                    client=MagicMock(), owner="alice", plan_name="free",
                    is_paid_plan=False, config=make_config(),
                )
                with pytest.raises(SystemExit) as exc_info:
                    main()
        assert exc_info.value.code == 2

    def test_bare_repo_name_exits(self):
        """create my-repo (no owner/) should exit with error."""
        with patch("sys.argv", ["gh-safe-repo", "create", "my-repo"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 2


class TestFixFlagValidation:
    def test_bare_repo_name_exits(self):
        with patch("sys.argv", ["gh-safe-repo", "fix", "my-repo"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 2

    def test_no_admin_permission_exits(self):
        """fix should reject repos where the user lacks admin permissions."""
        with patch("sys.argv", ["gh-safe-repo", "fix", "some-org/some-repo", "--dry-run"]):
            with patch("gh_safe_repo.commands.fix.build_context") as mock_ctx:
                mock_client = MagicMock()
                mock_client.get_repo_data.return_value = {
                    "private": False,
                    "default_branch": "main",
                    "permissions": {"admin": False, "push": True, "pull": True},
                }
                mock_ctx.return_value = MagicMock(
                    client=mock_client, owner="myuser", plan_name="free",
                    is_paid_plan=False, config=MagicMock(),
                )
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1

    def test_admin_permission_proceeds(self):
        """fix should allow repos where the user has admin permissions (no exit at permission check)."""
        with patch("sys.argv", ["gh-safe-repo", "fix", "some-org/some-repo", "--dry-run"]):
            with patch("gh_safe_repo.commands.fix.build_context") as mock_ctx:
                mock_client = MagicMock()
                mock_client.get_repo_data.return_value = {
                    "private": False,
                    "default_branch": "main",
                    "permissions": {"admin": True, "push": True, "pull": True},
                }
                # call_api returns (status, body) tuples
                mock_client.call_api.return_value = (200, "{}")
                mock_ctx.return_value = MagicMock(
                    client=mock_client, owner="myuser", plan_name="free",
                    is_paid_plan=False, config=make_config(),
                )
                # Should proceed past the permission check (may exit 0 from dry-run
                # or raise elsewhere — the point is it does NOT exit 1 for permissions)
                try:
                    main()
                except SystemExit as e:
                    assert e.code != 1, "Should not exit 1 — admin permission is granted"


class TestNoSubcommand:
    def test_no_args_exits(self):
        with patch("sys.argv", ["gh-safe-repo"]):
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
        mock_client.repo_path.return_value = "/repos/alice/my-public-repo"
        mock_client.call_api.return_value = (404, {})  # repo doesn't exist
        return mock_client

    def test_scan_plan_entry_includes_scanner_description(self, capsys):
        with patch("sys.argv", [
            "gh-safe-repo", "create", "alice/my-public-repo",
            "--from", "alice/my-private-repo", "--public", "--dry-run",
        ]):
            with patch("gh_safe_repo.commands.create.build_context") as mock_ctx:
                mock_client = self._make_mock_client()
                mock_ctx.return_value = MagicMock(
                    client=mock_client, owner="alice", plan_name="free",
                    is_paid_plan=False, config=make_config({("repo", "private"): "false"}),
                )

                # Patch plugin plan() calls to return empty plans
                with patch("gh_safe_repo.commands.create.RepositoryPlugin") as MockRepo, \
                     patch("gh_safe_repo.commands.create.ActionsPlugin") as MockActions, \
                     patch("gh_safe_repo.commands.create.BranchProtectionPlugin") as MockBP, \
                     patch("gh_safe_repo.commands.create.SecurityPlugin") as MockSec, \
                     patch("gh_safe_repo.commands.create.TagProtectionPlugin") as MockTag:

                    for MockPlugin in (MockRepo, MockActions, MockBP, MockSec, MockTag):
                        instance = MockPlugin.return_value
                        instance.plan.return_value = Plan()

                    # Force scanner to report "regex only"
                    original_init = SecurityScanner.__init__
                    def patched_init(self_inner, config, debug=False):
                        original_init(self_inner, config, debug=debug)
                        self_inner._discovery = {"method": "none"}
                    with patch.object(SecurityScanner, "__init__", patched_init):
                        with pytest.raises(SystemExit):
                            main()

        captured = capsys.readouterr()
        assert "regex only" in captured.out


class TestNoDeadFlags:
    """Every optional CLI flag must be referenced in its command's run() function.

    This prevents flags that are accepted by argparse but silently ignored
    at runtime (e.g. --json was once accepted by 'scan' but never read).
    """

    @pytest.mark.parametrize("cmd_module", [create, fix, scan], ids=lambda m: m.NAME)
    def test_all_flags_referenced_in_run(self, cmd_module):
        import argparse
        import inspect

        parser = argparse.ArgumentParser()
        cmd_module.add_arguments(parser)

        optional_dests = {
            a.dest for a in parser._actions
            if a.option_strings and a.dest != "help"
        }

        # Source of run() plus any helper in the module that accepts the
        # args namespace (e.g. build_context reads args.config and
        # args.debug on behalf of create/fix).
        source = inspect.getsource(cmd_module.run)
        for obj in vars(cmd_module).values():
            if not callable(obj) or obj is cmd_module.run:
                continue
            try:
                if "args" in inspect.signature(obj).parameters:
                    source += inspect.getsource(obj)
            except (ValueError, TypeError):
                pass

        for dest in optional_dests:
            assert f"args.{dest}" in source, (
                f"{cmd_module.NAME}: --{dest.replace('_', '-')} is accepted "
                f"by the parser but never referenced as args.{dest} in run()"
            )


class TestScanSkippedDirsWarning:
    """scan command warns when SKIP_DIRS subdirectories were present but not scanned."""

    def test_skipped_dirs_warning_printed_to_stdout(self, tmp_path, capsys):
        node_modules = tmp_path / "node_modules"
        node_modules.mkdir()
        (node_modules / "index.js").write_text("hello")

        with patch("sys.argv", ["gh-safe-repo", "scan", str(tmp_path)]):
            with pytest.raises(SystemExit):
                main()

        captured = capsys.readouterr()
        assert "skipped during scan" in captured.out
        assert "node_modules" in captured.out

    def test_no_warning_when_no_skip_dirs_present(self, tmp_path, capsys):
        (tmp_path / "main.py").write_text("print('hello')")

        with patch("sys.argv", ["gh-safe-repo", "scan", str(tmp_path)]):
            with pytest.raises(SystemExit):
                main()

        captured = capsys.readouterr()
        assert "skipped during scan" not in captured.out
