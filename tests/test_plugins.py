"""Tests for plugins — all API calls mocked."""

import json
from unittest.mock import MagicMock, patch

import pytest
from lib.config_manager import ConfigManager
from lib.diff import ChangeCategory, ChangeType
from lib.errors import APIError, RepoExistsError
from lib.plugins.actions import ActionsPlugin
from lib.plugins.branch_protection import BranchProtectionPlugin
from lib.plugins.repository import RepositoryPlugin
from lib.plugins.security import SecurityPlugin


def make_mock_client():
    client = MagicMock()
    client.repo_path.side_effect = lambda owner, repo, suffix="": (
        f"/repos/{owner}/{repo}/{suffix.lstrip('/')}" if suffix else f"/repos/{owner}/{repo}"
    )
    return client


def make_config(overrides=None):
    config = ConfigManager(config_path="/tmp/nonexistent-gh-safe-repo.ini")
    if overrides:
        config.apply_overrides(overrides)
    return config


class TestRepositoryPlugin:
    def test_plan_includes_add_for_repo(self):
        client = make_mock_client()
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        assert any(c.key == "repository" for c in adds)

    def test_plan_includes_update_for_has_wiki(self):
        client = make_mock_client()
        # Default is has_wiki=false, GitHub default is true → should be UPDATE
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        wiki_change = next((c for c in updates if c.key == "has_wiki"), None)
        assert wiki_change is not None
        assert wiki_change.old is True
        assert wiki_change.new is False

    def test_plan_includes_update_for_delete_branch_on_merge(self):
        client = make_mock_client()
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        dbom = next((c for c in updates if c.key == "delete_branch_on_merge"), None)
        assert dbom is not None
        assert dbom.old is False
        assert dbom.new is True

    def test_apply_posts_to_user_repos(self):
        client = make_mock_client()
        client.call_json.return_value = {"id": 1, "name": "my-repo"}
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        plugin.apply(plan)
        # First call should be POST /user/repos
        first_call = client.call_json.call_args_list[0]
        assert first_call.args[0] == "POST"
        assert first_call.args[1] == "/user/repos"
        assert first_call.args[2]["name"] == "my-repo"

    def test_apply_patches_settings(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        plugin.apply(plan)
        # Should have a PATCH call
        patch_calls = [
            c for c in client.call_json.call_args_list if c.args[0] == "PATCH"
        ]
        assert len(patch_calls) == 1
        patch_body = patch_calls[0].args[2]
        assert patch_body.get("has_wiki") is False
        assert patch_body.get("delete_branch_on_merge") is True

    def test_apply_raises_repo_exists_on_422(self):
        client = make_mock_client()
        client.call_json.side_effect = APIError("Unprocessable Entity", status_code=422)
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        with pytest.raises(RepoExistsError):
            plugin.apply(plan)


class TestActionsPlugin:
    def test_plan_includes_workflow_permissions_update(self):
        client = make_mock_client()
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        wp = next((c for c in updates if c.key == "default_workflow_permissions"), None)
        assert wp is not None
        assert wp.old == "write"
        assert wp.new == "read"

    def test_plan_includes_can_approve_update(self):
        client = make_mock_client()
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        cap = next((c for c in updates if c.key == "can_approve_pull_request_reviews"), None)
        assert cap is not None
        assert cap.old is True
        assert cap.new is False

    def test_apply_puts_workflow_permissions(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        plugin.apply(plan)
        assert client.call_json.called
        call = client.call_json.call_args
        assert call.args[0] == "PUT"
        body = call.args[2]
        assert body.get("default_workflow_permissions") == "read"
        assert body.get("can_approve_pull_request_reviews") is False

    def test_no_apply_when_using_github_defaults(self):
        client = make_mock_client()
        config = make_config({
            ("actions", "default_workflow_permissions"): "write",
            ("actions", "can_approve_pull_request_reviews"): "true",
        })
        plugin = ActionsPlugin(client, "alice", "my-repo", config)
        plan = plugin.plan()
        plugin.apply(plan)
        # No API calls since desired == GitHub default
        assert not client.call_json.called


class TestBranchProtectionPlugin:
    def test_plan_emits_skip(self):
        client = make_mock_client()
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        assert all(c.type == ChangeType.SKIP for c in plan.changes)

    def test_apply_does_nothing(self):
        client = make_mock_client()
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        plugin.apply(plan)
        client.call_json.assert_not_called()


class TestSecurityPlugin:
    def test_plan_emits_skips(self):
        client = make_mock_client()
        plugin = SecurityPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        assert len(plan.changes) == 2
        assert all(c.type == ChangeType.SKIP for c in plan.changes)

    def test_apply_does_nothing(self):
        client = make_mock_client()
        plugin = SecurityPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        plugin.apply(plan)
        client.call_json.assert_not_called()
