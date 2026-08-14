"""Tests for CLI subcommands and shared helpers."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from gh_safe_repo.errors import APIError, AuthError
from gh_safe_repo.commands._common import (
    _resolve_branches,
    build_context,
    format_plan_json,
    parse_repo_arg,
    print_success,
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

    @pytest.mark.parametrize("arg", [
        "alice/my/repo",     # extra segment — a typo, not a deep link
        "alice/my/repo/x",
        "../..",             # would traverse in /repos/{owner}/{repo}
        "alice/..",
        "./repo",
    ])
    def test_extra_or_traversing_segments_rejected(self, arg):
        """A bare argument must be exactly owner/repo.

        Repo names cannot contain "/", so folding a trailing path into the name
        only ever yields a guaranteed 404. Deep links are resolved for URL
        arguments, where a host makes the trailing segments unambiguous.
        """
        with pytest.raises(SystemExit) as exc_info:
            parse_repo_arg(arg)
        assert exc_info.value.code == 2

    def test_trailing_git_suffix_stripped(self):
        owner, repo = parse_repo_arg("alice/my-repo.git")
        assert (owner, repo) == ("alice", "my-repo")


class TestParseRepoArgURLs:
    """URL forms accepted by parse_repo_arg (#65)."""

    @pytest.mark.parametrize("arg", [
        "https://github.com/alice/my-repo",
        "http://github.com/alice/my-repo",
        "https://github.com/alice/my-repo/",
        "https://github.com/alice/my-repo.git",
        "https://www.github.com/alice/my-repo",
        "github.com/alice/my-repo",
        "HTTPS://GitHub.com/alice/my-repo",
        "git@github.com:alice/my-repo.git",
        "git@github.com:alice/my-repo",
        "ssh://git@github.com/alice/my-repo.git",
    ])
    def test_url_forms(self, arg):
        assert parse_repo_arg(arg) == ("alice", "my-repo")

    @pytest.mark.parametrize("arg", [
        # A URL copied from the browser address bar carries a query string or
        # fragment; neither belongs in the repo name.
        "https://github.com/alice/my-repo?tab=readme-ov-file",
        "https://github.com/alice/my-repo#readme",
        "https://github.com/alice/my-repo/?tab=stars",
        # Deep links resolve to the repo they point into.
        "https://github.com/alice/my-repo/issues/65",
        "https://github.com/alice/my-repo/tree/main/src",
        "https://github.com/alice/my-repo/blob/main/README.md#install",
    ])
    def test_url_extras_stripped(self, arg):
        assert parse_repo_arg(arg) == ("alice", "my-repo")

    @pytest.mark.parametrize("arg", [
        "https://gitlab.com/alice/my-repo",      # not GitHub
        "https://github.com.evil.test/alice/x",  # host only looks like GitHub
        "https://github.com/alice",              # owner, no repo
        "https://github.com//my-repo",           # empty owner
        "https://github.com/../..",              # would traverse in /repos/...
        "https://github.com/alice/..",
        "ssh://git@gitlab.com/alice/my-repo",
    ])
    def test_urlish_but_unusable_exits(self, arg):
        """URL-shaped input must not fall through to the "/" splitter.

        Splitting these would yield a nonsense owner ("https:") and an error
        message describing the wrong problem.
        """
        with pytest.raises(SystemExit) as exc_info:
            parse_repo_arg(arg)
        assert exc_info.value.code == 2

    @pytest.mark.parametrize("arg", [
        # Every page a user is plausibly looking at when they decide to run
        # this tool, copied straight from the browser address bar.
        "https://github.com/AriESQ/gh-safe-repo",
        "https://github.com/AriESQ/gh-safe-repo?tab=readme-ov-file",
        "https://github.com/AriESQ/gh-safe-repo/pull/67/changes",
        "https://github.com/AriESQ/gh-safe-repo/pull/67/files",
        "https://github.com/AriESQ/gh-safe-repo/issues/65",
        "https://github.com/AriESQ/gh-safe-repo/blob/master/README.md?plain=1#L10",
        "https://github.com/AriESQ/gh-safe-repo/compare/master...feat",
        "https://github.com/AriESQ/gh-safe-repo/releases/tag/v0.2.0",
        "https://github.com/AriESQ/gh-safe-repo/actions/runs/123",
        "https://github.com/AriESQ/gh-safe-repo/settings",
        "https://github.com/AriESQ/gh-safe-repo/tree/master/gh_safe_repo",
        # Clone and remote lines.
        "git@github.com:AriESQ/gh-safe-repo.git",
        "https://github.com/AriESQ/gh-safe-repo.git",
        # Paste artifacts: shell quoting and trailing newlines preserve these.
        " AriESQ/gh-safe-repo ",
        "\tAriESQ/gh-safe-repo\n",
        "https://github.com/AriESQ/gh-safe-repo\n",
        # Plain form.
        "AriESQ/gh-safe-repo",
    ])
    def test_paste_matrix_resolves_to_repo(self, arg):
        """Forms a user is likely to paste all resolve to the same repo."""
        assert parse_repo_arg(arg) == ("AriESQ", "gh-safe-repo")

    @pytest.mark.parametrize("arg", [
        "https://github.com/orgs/myorg/repositories",
        "https://github.com/settings/tokens",
        "https://github.com/topics/python",
        "https://github.com/sponsors/AriESQ",
        "https://github.com/users/AriESQ/projects",
        "https://github.com/apps/dependabot",
        "https://github.com/marketplace/actions/checkout",
        "https://github.com/notifications",
        "https://github.com/search?q=gh-safe-repo",
    ])
    def test_reserved_github_paths_rejected(self, arg):
        """github.com site pages are not repositories.

        Without this, /orgs/myorg/repositories parses as the repo "orgs/myorg"
        and fails later with an error describing the wrong problem.
        """
        with pytest.raises(SystemExit) as exc_info:
            parse_repo_arg(arg)
        assert exc_info.value.code == 2

    @pytest.mark.parametrize("arg", [
        "git+https://github.com/AriESQ/gh-safe-repo.git",  # pip requirement syntax
        "<https://github.com/AriESQ/gh-safe-repo>",        # Markdown/Slack autolink
    ])
    def test_wrapped_url_syntaxes_rejected(self, arg):
        """Rejected by design: the URL is recognisable but the wrapper is not.

        Rejecting is safe — the user gets the accepted-forms message. Change
        this test if either becomes worth unwrapping.
        """
        with pytest.raises(SystemExit) as exc_info:
            parse_repo_arg(arg)
        assert exc_info.value.code == 2

    def test_case_is_preserved(self):
        """Push URLs are case-sensitive; casing must survive parsing."""
        assert parse_repo_arg("https://github.com/AriESQ/GH-Safe-Repo") == (
            "AriESQ", "GH-Safe-Repo",
        )

    @pytest.mark.parametrize("arg,expected", [
        ("owner/my.repo", ("owner", "my.repo")),
        ("owner/my_repo", ("owner", "my_repo")),
        ("owner/repo.js", ("owner", "repo.js")),
        ("https://github.com/owner/dot.net.sdk", ("owner", "dot.net.sdk")),
    ])
    def test_punctuation_in_repo_names(self, arg, expected):
        """Dots and underscores are legal in repo names and must survive."""
        assert parse_repo_arg(arg) == expected

    def test_rejection_message_names_the_url(self, capsys):
        with pytest.raises(SystemExit):
            parse_repo_arg("https://gitlab.com/alice/my-repo")
        err = capsys.readouterr().err
        assert "Not a GitHub repository URL" in err
        assert "https://gitlab.com/alice/my-repo" in err


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

    def test_local_not_a_git_repo_exits_with_error(self, tmp_path, capsys):
        """--local PATH must be a git repository; plain directories are rejected."""
        with patch("sys.argv", [
            "gh-safe-repo", "create", "alice/my-repo",
            "--local", str(tmp_path), "--dry-run",
        ]):
            with patch("gh_safe_repo.commands.create.build_context") as mock_ctx:
                mock_ctx.return_value = MagicMock(
                    client=MagicMock(), owner="alice", plan_name="free",
                    is_paid_plan=False, config=make_config(),
                )
                with pytest.raises(SystemExit) as exc_info:
                    main()
        assert exc_info.value.code == 2
        assert "not a git repository" in capsys.readouterr().err

    def test_bare_repo_name_exits(self):
        """create my-repo (no owner/) should exit with error."""
        with patch("sys.argv", ["gh-safe-repo", "create", "my-repo"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 2

    def test_credential_failure_aborts_before_repo_creation(self, tmp_path):
        """When --local is set and git creds are missing, create must fail
        BEFORE any API call — otherwise we leave an empty repo behind."""
        mock_client = MagicMock()
        mock_transport = MagicMock()
        mock_transport.preflight.side_effect = AuthError(
            "SSH authentication to git@github.com failed."
        )

        with patch("sys.argv", [
            "gh-safe-repo", "create", "alice/my-repo", "--local", str(tmp_path), "--yes",
        ]):
            with patch("gh_safe_repo.commands.create.build_context") as mock_ctx, \
                 patch("gh_safe_repo.commands.create.discover_transport",
                       return_value=mock_transport) as mock_discover:
                mock_ctx.return_value = MagicMock(
                    client=mock_client, owner="alice", plan_name="free",
                    is_paid_plan=False, config=make_config(),
                )
                with pytest.raises(SystemExit) as exc_info:
                    main()

        assert exc_info.value.code == 1
        mock_discover.assert_called_once()
        mock_transport.preflight.assert_called_once()
        # Critical: no repo creation API call was made
        mock_client.call_json.assert_not_called()
        mock_client.push_local.assert_not_called()

    def test_credential_check_skipped_when_no_push_needed(self):
        """Plain create (no --local/--from) doesn't push, so don't probe creds."""
        mock_client = MagicMock()
        with patch("sys.argv", [
            "gh-safe-repo", "create", "alice/my-repo", "--dry-run",
        ]):
            with patch("gh_safe_repo.commands.create.build_context") as mock_ctx, \
                 patch("gh_safe_repo.commands.create.discover_transport") as mock_discover:
                mock_ctx.return_value = MagicMock(
                    client=mock_client, owner="alice", plan_name="free",
                    is_paid_plan=False, config=make_config(),
                )
                try:
                    main()
                except SystemExit:
                    pass
        mock_discover.assert_not_called()

    def test_credential_check_skipped_in_dry_run(self, tmp_path):
        """--dry-run makes zero external calls; credential probe is also skipped."""
        (tmp_path / ".git").mkdir()
        mock_client = MagicMock()
        with patch("sys.argv", [
            "gh-safe-repo", "create", "alice/my-repo", "--local", str(tmp_path), "--dry-run",
        ]):
            with patch("gh_safe_repo.commands.create.build_context") as mock_ctx, \
                 patch("gh_safe_repo.commands.create.discover_transport") as mock_discover:
                mock_ctx.return_value = MagicMock(
                    client=mock_client, owner="alice", plan_name="free",
                    is_paid_plan=False, config=make_config(),
                )
                try:
                    main()
                except SystemExit:
                    pass
        mock_discover.assert_not_called()

    def test_push_local_called_with_canonical_owner(self, tmp_path):
        """Direct coverage: client.push_local must receive ctx.owner, not the typed owner.

        This was the actual bug — a typed lowercase owner produced a redirected
        push URL, which GitHub rejects for repos containing workflow files.
        """
        (tmp_path / ".git").mkdir()
        mock_client = MagicMock()
        mock_client.check_repo_exists.return_value = False
        # Successful repo create response
        mock_client.call_json.return_value = {"default_branch": "main"}

        with patch("sys.argv", [
            "gh-safe-repo", "create", "ariesq/my-repo", "--local", str(tmp_path), "--yes",
        ]):
            with patch("gh_safe_repo.commands.create.build_context") as mock_ctx, \
                 patch("gh_safe_repo.commands.create.discover_transport"), \
                 patch("gh_safe_repo.commands.create.check_repo_exists", return_value=False), \
                 patch("gh_safe_repo.commands.create.run_preflight_scan_local", return_value=True), \
                 patch("gh_safe_repo.commands.create.RepositoryPlugin") as MockRepo, \
                 patch("gh_safe_repo.commands.create.ActionsPlugin"), \
                 patch("gh_safe_repo.commands.create.BranchProtectionPlugin"), \
                 patch("gh_safe_repo.commands.create.SecurityPlugin"), \
                 patch("gh_safe_repo.commands.create.TagProtectionPlugin"):
                MockRepo.return_value = MagicMock(created_default_branch="main")
                mock_ctx.return_value = MagicMock(
                    client=mock_client, owner="AriESQ", plan_name="free",
                    is_paid_plan=False, config=make_config(),
                )
                try:
                    main()
                except SystemExit:
                    pass

        mock_client.push_local.assert_called_once()
        call_owner = mock_client.push_local.call_args[0][1]
        assert call_owner == "AriESQ"

    def test_plugins_constructed_with_canonical_owner(self, tmp_path):
        """Plugin constructors must receive the canonical owner for API paths."""
        mock_client = MagicMock()

        with patch("sys.argv", [
            "gh-safe-repo", "create", "ariesq/my-repo", "--dry-run",
        ]):
            with patch("gh_safe_repo.commands.create.build_context") as mock_ctx, \
                 patch("gh_safe_repo.commands.create.RepositoryPlugin") as MockRepo, \
                 patch("gh_safe_repo.commands.create.ActionsPlugin") as MockActions, \
                 patch("gh_safe_repo.commands.create.BranchProtectionPlugin") as MockBP, \
                 patch("gh_safe_repo.commands.create.SecurityPlugin") as MockSec, \
                 patch("gh_safe_repo.commands.create.TagProtectionPlugin") as MockTag:
                for M in (MockRepo, MockActions, MockBP, MockSec, MockTag):
                    M.return_value = MagicMock()
                    M.return_value.plan.return_value = Plan()
                mock_ctx.return_value = MagicMock(
                    client=mock_client, owner="AriESQ", plan_name="free",
                    is_paid_plan=False, config=make_config(),
                )
                try:
                    main()
                except SystemExit:
                    pass

        for M in (MockRepo, MockActions, MockBP, MockSec, MockTag):
            assert M.call_args[0][1] == "AriESQ", \
                f"{M} called with non-canonical owner: {M.call_args}"

    def test_uses_canonical_owner_casing_in_plan(self, capsys):
        """User types 'ariesq/repo' but ctx.owner is 'AriESQ' — plan must show canonical casing.

        Git push URLs are case-sensitive; redirected pushes are rejected for workflow files.
        """
        with patch("sys.argv", [
            "gh-safe-repo", "create", "ariesq/my-repo", "--dry-run",
        ]):
            with patch("gh_safe_repo.commands.create.build_context") as mock_ctx:
                mock_ctx.return_value = MagicMock(
                    client=MagicMock(), owner="AriESQ", plan_name="free",
                    is_paid_plan=False, config=make_config(),
                )
                with pytest.raises(SystemExit) as exc_info:
                    main()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "AriESQ/my-repo" in out
        assert "ariesq/my-repo" not in out


class TestFixFlagValidation:
    def test_bare_repo_name_exits(self):
        with patch("sys.argv", ["gh-safe-repo", "fix", "my-repo"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 2

    def test_no_admin_permission_exits(self, capsys):
        """fix should reject repos where the user lacks admin permissions."""
        with patch("sys.argv", ["gh-safe-repo", "fix", "some-org/some-repo", "--dry-run"]):
            with patch("gh_safe_repo.commands.fix.build_context") as mock_ctx:
                mock_client = MagicMock()
                mock_client.get_repo_data.return_value = {
                    "id": 123,
                    "full_name": "some-org/some-repo",
                    "owner": {"login": "some-org", "type": "Organization"},
                    "private": False,
                    "default_branch": "main",
                    "permissions": {"admin": False, "push": True, "pull": True},
                }
                mock_ctx.return_value = MagicMock(
                    client=mock_client, owner="rootnotez", plan_name="free",
                    is_paid_plan=False, config=MagicMock(),
                )
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1
        # The clarified message names the active account and covers read-only use.
        err = capsys.readouterr().err
        assert "rootnotez" in err
        assert "read or modify" in err

    def test_404_reports_access_ambiguity(self, capsys):
        """A 404 should not be reported as a flat 'does not exist'; it must also
        name the active account and flag the private/not-visible possibility."""
        with patch("sys.argv", ["gh-safe-repo", "fix", "ariesq/private-repo", "--dry-run"]):
            with patch("gh_safe_repo.commands.fix.build_context") as mock_ctx:
                mock_client = MagicMock()
                mock_client.get_repo_data.side_effect = APIError(
                    "GET /repos/ariesq/private-repo returned 404", status_code=404
                )
                mock_ctx.return_value = MagicMock(
                    client=mock_client, owner="rootnotez", plan_name="free",
                    is_paid_plan=False, config=MagicMock(),
                )
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "does not exist" in err   # e2e contract preserved
        assert "not visible" in err
        assert "rootnotez" in err

    def test_plugins_constructed_with_canonical_owner(self):
        """fix plugins must receive the canonical owner derived from repo_data."""
        with patch("sys.argv", [
            "gh-safe-repo", "fix", "some-org/some-repo", "--dry-run",
        ]):
            with patch("gh_safe_repo.commands.fix.build_context") as mock_ctx, \
                 patch("gh_safe_repo.commands.fix.RepositoryPlugin") as MockRepo, \
                 patch("gh_safe_repo.commands.fix.ActionsPlugin") as MockActions, \
                 patch("gh_safe_repo.commands.fix.BranchProtectionPlugin") as MockBP, \
                 patch("gh_safe_repo.commands.fix.SecurityPlugin") as MockSec, \
                 patch("gh_safe_repo.commands.fix.TagProtectionPlugin") as MockTag:
                for M in (MockRepo, MockActions, MockBP, MockSec, MockTag):
                    M.return_value = MagicMock()
                    M.return_value.plan.return_value = Plan()
                    M.return_value.fetch_current_state.return_value = {}
                mock_client = MagicMock()
                mock_client.get_repo_data.return_value = {
                    "id": 123,
                    "full_name": "Some-Org/Some-Repo",
                    "name": "Some-Repo",
                    "owner": {"login": "Some-Org", "type": "Organization"},
                    "private": False,
                    "default_branch": "main",
                    "permissions": {"admin": True, "push": True, "pull": True},
                }
                mock_ctx.return_value = MagicMock(
                    client=mock_client, owner="myuser", plan_name="free",
                    is_paid_plan=False, config=make_config(),
                )
                try:
                    main()
                except SystemExit:
                    pass

        for M in (MockRepo, MockActions, MockBP, MockSec, MockTag):
            args_, _kw = M.call_args
            assert args_[1] == "Some-Org" and args_[2] == "Some-Repo", \
                f"{M} called with non-canonical owner/repo: {args_}"

    def test_uses_canonical_owner_casing_from_repo_data(self, capsys):
        """fix should canonicalize owner/repo casing from the GET /repos response.

        `fix` accepts org/collaborator repos, so it cannot use ctx.owner — it must
        derive canonical casing from repo_data['owner']['login'] and repo_data['name'].
        """
        with patch("sys.argv", [
            "gh-safe-repo", "fix", "some-org/some-repo", "--dry-run",
        ]):
            with patch("gh_safe_repo.commands.fix.build_context") as mock_ctx:
                mock_client = MagicMock()
                mock_client.get_repo_data.return_value = {
                    "id": 123,
                    "full_name": "Some-Org/Some-Repo",
                    "name": "Some-Repo",
                    "owner": {"login": "Some-Org", "type": "Organization"},
                    "private": False,
                    "default_branch": "main",
                    "permissions": {"admin": True, "push": True, "pull": True},
                }
                mock_client.call_api.return_value = (200, "{}")
                mock_ctx.return_value = MagicMock(
                    client=mock_client, owner="myuser", plan_name="free",
                    is_paid_plan=False, config=make_config(),
                )
                try:
                    main()
                except SystemExit:
                    pass
        out = capsys.readouterr().out
        assert "Some-Org/Some-Repo" in out
        assert "some-org/some-repo" not in out

    def test_admin_permission_proceeds(self):
        """fix should allow repos where the user has admin permissions (no exit at permission check)."""
        with patch("sys.argv", ["gh-safe-repo", "fix", "some-org/some-repo", "--dry-run"]):
            with patch("gh_safe_repo.commands.fix.build_context") as mock_ctx:
                mock_client = MagicMock()
                mock_client.get_repo_data.return_value = {
                    "id": 123,
                    "full_name": "some-org/some-repo",
                    "owner": {"login": "some-org", "type": "Organization"},
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


class TestPrintSuccessProtocolOrdering:
    """Success banner orders remote suggestions by the user's git protocol (#19)."""

    def test_https_preference_lists_https_first(self, capsys):
        print_success("octocat", "demo", protocol="https")
        out = capsys.readouterr().out
        assert out.index("HTTPS: git remote add") < out.index("SSH  : git remote add")

    def test_ssh_preference_lists_ssh_first(self, capsys):
        print_success("octocat", "demo", protocol="ssh")
        out = capsys.readouterr().out
        assert out.index("SSH  : git remote add") < out.index("HTTPS: git remote add")

    def test_default_protocol_is_https_first(self, capsys):
        print_success("octocat", "demo")
        out = capsys.readouterr().out
        assert out.index("HTTPS: git remote add") < out.index("SSH  : git remote add")

    def test_local_push_banner_has_no_remote_lines(self, capsys):
        print_success("octocat", "demo", local_push=True, protocol="ssh")
        out = capsys.readouterr().out
        assert "git remote add origin" not in out
        assert "Set your tracking branch" in out
