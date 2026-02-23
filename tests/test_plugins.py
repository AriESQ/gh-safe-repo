"""Tests for plugins — all API calls mocked."""

import json
from unittest.mock import MagicMock, patch

import pytest
from gh_safe_repo.config_manager import ConfigManager
from gh_safe_repo.diff import ChangeCategory, ChangeType
from gh_safe_repo.errors import APIError, RepoExistsError
from gh_safe_repo.plugins.actions import ActionsPlugin
from gh_safe_repo.plugins.branch_protection import BranchProtectionPlugin
from gh_safe_repo.plugins.repository import RepositoryPlugin
from gh_safe_repo.plugins.security import SecurityPlugin


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
    # --- Private repo (default) — all changes should be SKIP ---

    def test_plan_private_repo_emits_skip(self):
        client = make_mock_client()
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        assert all(c.type == ChangeType.SKIP for c in plan.changes)

    def test_apply_private_repo_does_nothing(self):
        client = make_mock_client()
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        plugin.apply(plan)
        client.call_json.assert_not_called()

    # --- Public repo — should add protection rules ---

    def test_plan_public_repo_emits_add_changes(self):
        client = make_mock_client()
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        assert len(adds) > 0

    def test_plan_public_repo_includes_force_push_change(self):
        client = make_mock_client()
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        fp_change = next((c for c in adds if c.key == "allow_force_pushes"), None)
        assert fp_change is not None
        assert fp_change.new is False  # we disable force pushes (GH default is True)

    def test_plan_public_repo_includes_require_pr_change(self):
        client = make_mock_client()
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        pr_change = next((c for c in adds if c.key == "require_pull_request"), None)
        assert pr_change is not None
        assert pr_change.new is True

    def test_apply_public_repo_puts_branch_protection(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        plugin.apply(plan)
        assert client.call_json.called
        call = client.call_json.call_args
        assert call.args[0] == "PUT"
        assert "branches/main/protection" in call.args[1]

    def test_apply_public_repo_body_has_correct_fields(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        plugin.apply(plan)
        body = client.call_json.call_args.args[2]
        assert body["allow_force_pushes"] is False
        assert body["allow_deletions"] is False
        assert body["enforce_admins"] is False
        assert body["required_pull_request_reviews"]["dismiss_stale_reviews"] is True
        assert body["required_pull_request_reviews"]["required_approving_review_count"] == 1

    def test_apply_public_repo_uses_configured_branch(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        config = make_config({("branch_protection", "protected_branch"): "master"})
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", config, is_public=True)
        plan = plugin.plan()
        plugin.apply(plan)
        assert "branches/master/protection" in client.call_json.call_args.args[1]

    # --- Paid plan private repo ---

    def test_plan_private_paid_repo_emits_add_changes(self):
        client = make_mock_client()
        plugin = BranchProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=False, is_paid_plan=True
        )
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        assert len(adds) > 0

    # --- Rulesets API ---

    def test_apply_uses_rulesets_endpoint_when_configured(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        config = make_config({("branch_protection", "use_rulesets"): "true"})
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", config, is_public=True)
        plan = plugin.plan()
        plugin.apply(plan)
        assert client.call_json.called
        call = client.call_json.call_args
        assert call.args[0] == "POST"
        assert call.args[1].endswith("/rulesets")

    def test_apply_uses_classic_endpoint_by_default(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        plugin.apply(plan)
        assert client.call_json.called
        call = client.call_json.call_args
        assert call.args[0] == "PUT"
        assert "branches/main/protection" in call.args[1]

    def test_ruleset_body_includes_pr_rule(self):
        client = make_mock_client()
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        desired = plugin._desired()
        body = plugin._build_ruleset_body(desired)
        rule_types = [r["type"] for r in body["rules"]]
        assert "pull_request" in rule_types

    def test_ruleset_body_admin_bypass_when_enforce_admins_false(self):
        client = make_mock_client()
        # enforce_admins defaults to false in safe defaults
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        desired = plugin._desired()
        body = plugin._build_ruleset_body(desired)
        assert len(body["bypass_actors"]) == 1
        assert body["bypass_actors"][0]["actor_id"] == 5
        assert body["bypass_actors"][0]["actor_type"] == "RepositoryRole"


class TestSecurityPlugin:
    # --- Private repo (default) — all changes should be SKIP ---

    def test_plan_private_repo_emits_skips(self):
        client = make_mock_client()
        plugin = SecurityPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        assert len(plan.changes) == 2
        assert all(c.type == ChangeType.SKIP for c in plan.changes)

    def test_apply_private_repo_does_nothing(self):
        client = make_mock_client()
        plugin = SecurityPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        plugin.apply(plan)
        client.call_json.assert_not_called()

    # --- Public repo — should enable Dependabot ---

    def test_plan_public_repo_has_dependabot_add(self):
        client = make_mock_client()
        plugin = SecurityPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        dep = next((c for c in adds if c.key == "dependabot_alerts"), None)
        assert dep is not None
        assert dep.new is True

    def test_plan_public_repo_secret_scanning_is_skip(self):
        # Secret scanning is automatic for public repos — no API action needed
        client = make_mock_client()
        plugin = SecurityPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        skips = [c for c in plan.changes if c.type == ChangeType.SKIP]
        ss = next((c for c in skips if c.key == "secret_scanning"), None)
        assert ss is not None
        assert "automatic" in ss.reason.lower()

    def test_apply_public_repo_enables_dependabot(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = SecurityPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        plugin.apply(plan)
        assert client.call_json.called
        call = client.call_json.call_args
        assert call.args[0] == "PUT"
        assert "vulnerability-alerts" in call.args[1]

    def test_plan_public_repo_no_dependabot_when_disabled(self):
        client = make_mock_client()
        config = make_config({("security", "enable_dependabot_alerts"): "false"})
        plugin = SecurityPlugin(client, "alice", "my-repo", config, is_public=True)
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        assert not any(c.key == "dependabot_alerts" for c in adds)

    # --- Private paid plan repo ---

    def test_plan_private_paid_repo_has_dependabot_add(self):
        client = make_mock_client()
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=False, is_paid_plan=True
        )
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        dep = next((c for c in adds if c.key == "dependabot_alerts"), None)
        assert dep is not None
        assert dep.new is True

    def test_plan_private_paid_repo_has_secret_scanning_add(self):
        client = make_mock_client()
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=False, is_paid_plan=True
        )
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        ss = next((c for c in adds if c.key == "secret_scanning"), None)
        assert ss is not None
        assert ss.new is True

    def test_apply_private_paid_repo_enables_secret_scanning(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=False, is_paid_plan=True
        )
        plan = plugin.plan()
        plugin.apply(plan)
        # Find the PATCH call for secret scanning
        patch_calls = [
            c for c in client.call_json.call_args_list if c.args[0] == "PATCH"
        ]
        assert len(patch_calls) == 1
        body = patch_calls[0].args[2]
        assert body == {"security_and_analysis": {"secret_scanning": {"status": "enabled"}}}
