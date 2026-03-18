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
from gh_safe_repo.plugins.tag_protection import TagProtectionPlugin


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

    def test_plan_includes_description_when_source_description_set(self):
        client = make_mock_client()
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config(),
                                  source_description="A cool tool")
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        desc = next((c for c in adds if c.key == "description"), None)
        assert desc is not None
        assert desc.new == "A cool tool"

    def test_plan_no_description_change_when_source_description_empty(self):
        client = make_mock_client()
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config(),
                                  source_description="")
        plan = plugin.plan()
        assert not any(c.key == "description" for c in plan.changes)

    def test_plan_includes_topics_when_source_topics_set(self):
        client = make_mock_client()
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config(),
                                  source_topics=["go", "cli"])
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        topics = next((c for c in adds if c.key == "topics"), None)
        assert topics is not None
        assert topics.new == "go, cli"

    def test_apply_patches_description(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config(),
                                  source_description="My desc")
        plan = plugin.plan()
        plugin.apply(plan)
        patch_calls = [c for c in client.call_json.call_args_list if c.args[0] == "PATCH"]
        assert len(patch_calls) == 1
        assert patch_calls[0].args[2].get("description") == "My desc"

    def test_apply_puts_topics(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config(),
                                  source_topics=["python", "cli"])
        plan = plugin.plan()
        plugin.apply(plan)
        put_calls = [
            c for c in client.call_json.call_args_list
            if c.args[0] == "PUT" and "topics" in c.args[1]
        ]
        assert len(put_calls) == 1
        assert put_calls[0].args[2] == {"names": ["python", "cli"]}

    def test_apply_no_topics_call_when_no_topics(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config(),
                                  source_topics=[])
        plan = plugin.plan()
        plugin.apply(plan)
        topics_puts = [
            c for c in client.call_json.call_args_list
            if c.args[0] == "PUT" and "topics" in c.args[1]
        ]
        assert len(topics_puts) == 0


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

    def test_plan_includes_sha_pinning_update(self):
        client = make_mock_client()
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        sha = next((c for c in updates if c.key == "sha_pinning_required"), None)
        assert sha is not None
        assert sha.old is False
        assert sha.new is True

    def test_apply_puts_sha_pinning_to_permissions_endpoint(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        plugin.apply(plan)
        calls = client.call_json.call_args_list
        perms_calls = [c for c in calls if c.args[1].endswith("actions/permissions")]
        assert len(perms_calls) == 1
        body = perms_calls[0].args[2]
        assert body.get("sha_pinning_required") is True
        assert body.get("enabled") is True

    def test_apply_puts_workflow_permissions(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        plugin.apply(plan)
        calls = client.call_json.call_args_list
        workflow_calls = [c for c in calls if c.args[1].endswith("actions/permissions/workflow")]
        assert len(workflow_calls) == 1
        body = workflow_calls[0].args[2]
        assert body.get("default_workflow_permissions") == "read"
        assert body.get("can_approve_pull_request_reviews") is False

    def test_plan_includes_fork_pr_approval_update(self):
        client = make_mock_client()
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        fp = next((c for c in updates if c.key == "fork_pr_approval_policy"), None)
        assert fp is not None
        assert fp.old == "first_time_contributors_new_to_github"
        assert fp.new == "all_external_contributors"

    def test_apply_puts_fork_pr_approval_policy(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        plugin.apply(plan)
        calls = client.call_json.call_args_list
        fork_calls = [c for c in calls if c.args[1].endswith("fork-pr-contributor-approval")]
        assert len(fork_calls) == 1
        body = fork_calls[0].args[2]
        assert body == {"approval_policy": "all_external_contributors"}

    def test_no_apply_when_using_github_defaults(self):
        client = make_mock_client()
        config = make_config({
            ("actions", "allowed_actions"): "all",
            ("actions", "default_workflow_permissions"): "write",
            ("actions", "can_approve_pull_request_reviews"): "true",
            ("actions", "sha_pinning_required"): "false",
            ("actions", "fork_pr_approval_policy"): "first_time_contributors_new_to_github",
        })
        plugin = ActionsPlugin(client, "alice", "my-repo", config)
        plan = plugin.plan()
        plugin.apply(plan)
        # No API calls since desired == GitHub default
        assert not client.call_json.called

    def test_plan_includes_allowed_actions_update(self):
        client = make_mock_client()
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        aa = next((c for c in updates if c.key == "allowed_actions"), None)
        assert aa is not None
        assert aa.old == "all"
        assert aa.new == "selected"

    def test_plan_selected_includes_sub_settings(self):
        client = make_mock_client()
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        keys = {c.key for c in updates}
        assert "github_owned_allowed" not in keys  # default true == desired true
        assert "verified_allowed" in keys  # default false -> desired true

    def test_plan_no_sub_settings_when_allowed_all(self):
        """When allowed_actions is 'all', selected-actions sub-settings are not planned."""
        client = make_mock_client()
        config = make_config({("actions", "allowed_actions"): "all"})
        plugin = ActionsPlugin(client, "alice", "my-repo", config)
        plan = plugin.plan()
        keys = {c.key for c in plan.changes}
        assert "github_owned_allowed" not in keys
        assert "verified_allowed" not in keys
        assert "patterns_allowed" not in keys

    def test_apply_puts_allowed_actions_to_permissions_endpoint(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        plugin.apply(plan)
        calls = client.call_json.call_args_list
        perms_calls = [c for c in calls if c.args[1].endswith("actions/permissions")]
        assert len(perms_calls) == 1
        body = perms_calls[0].args[2]
        assert body.get("allowed_actions") == "selected"

    def test_apply_puts_selected_actions_to_selected_endpoint(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        plugin.apply(plan)
        calls = client.call_json.call_args_list
        selected_calls = [c for c in calls if c.args[1].endswith("selected-actions")]
        assert len(selected_calls) == 1
        body = selected_calls[0].args[2]
        assert body.get("verified_allowed") is True

    def test_apply_patterns_allowed(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        config = make_config({("actions", "patterns_allowed"): "myorg/*, actions/setup-node@*"})
        plugin = ActionsPlugin(client, "alice", "my-repo", config)
        plan = plugin.plan()
        plugin.apply(plan)
        calls = client.call_json.call_args_list
        selected_calls = [c for c in calls if c.args[1].endswith("selected-actions")]
        assert len(selected_calls) == 1
        body = selected_calls[0].args[2]
        assert body.get("patterns_allowed") == ["actions/setup-node@*", "myorg/*"]


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

    def test_apply_public_repo_uses_explicit_branches(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = BranchProtectionPlugin(client, "alice", "my-repo", make_config(), is_public=True, branches=["master"])
        plan = plugin.plan()
        plugin.apply(plan)
        assert "branches/master/protection" in client.call_json.call_args.args[1]

    def test_apply_multi_branch_calls_put_for_each(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = BranchProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True, branches=["master", "main"]
        )
        plan = plugin.plan()
        plugin.apply(plan)
        put_calls = [c for c in client.call_json.call_args_list if c.args[0] == "PUT"]
        urls = [c.args[1] for c in put_calls]
        assert any("branches/master/protection" in u for u in urls)
        assert any("branches/main/protection" in u for u in urls)

    def test_apply_404_branch_not_found_skips_gracefully(self):
        from gh_safe_repo.errors import APIError as _APIError
        client = make_mock_client()
        # master → 404, main → success
        def side_effect(method, path, body=None):
            if "master" in path:
                raise _APIError("not found", status_code=404)
            return {}
        client.call_json.side_effect = side_effect
        plugin = BranchProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True, branches=["master", "main"]
        )
        plan = plugin.plan()
        plugin.apply(plan)  # should not raise
        put_calls = [c for c in client.call_json.call_args_list if c.args[0] == "PUT"]
        assert len(put_calls) == 2  # both attempted

    def test_plan_public_repo_includes_protected_branches_key(self):
        client = make_mock_client()
        plugin = BranchProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True, branches=["main"]
        )
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        pb = next((c for c in adds if c.key == "protected_branches"), None)
        assert pb is not None
        assert pb.new == "main"

    def test_plan_multi_branch_protected_branches_value(self):
        client = make_mock_client()
        plugin = BranchProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True, branches=["master", "main"]
        )
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        pb = next((c for c in adds if c.key == "protected_branches"), None)
        assert pb is not None
        assert pb.new == "master, main"

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

    def test_ruleset_body_includes_all_branches(self):
        client = make_mock_client()
        plugin = BranchProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True, branches=["master", "main"]
        )
        desired = plugin._desired()
        body = plugin._build_ruleset_body(desired)
        include = body["conditions"]["ref_name"]["include"]
        assert "refs/heads/master" in include
        assert "refs/heads/main" in include

    def test_ruleset_body_single_branch(self):
        client = make_mock_client()
        plugin = BranchProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True, branches=["develop"]
        )
        desired = plugin._desired()
        body = plugin._build_ruleset_body(desired)
        include = body["conditions"]["ref_name"]["include"]
        assert include == ["refs/heads/develop"]


class TestSecurityPlugin:
    # --- Private repo (default) — all changes should be SKIP ---

    def test_plan_private_repo_emits_skips(self):
        client = make_mock_client()
        plugin = SecurityPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()
        assert all(c.type == ChangeType.SKIP for c in plan.changes)
        # Should have skips for all security features
        skip_keys = {c.key for c in plan.changes}
        assert "dependabot_alerts" in skip_keys
        assert "secret_scanning" in skip_keys
        assert "dependabot_security_updates" in skip_keys

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

    def test_plan_public_repo_dependency_graph_is_skip(self):
        # Dependency graph is automatic for public repos
        client = make_mock_client()
        plugin = SecurityPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        skips = [c for c in plan.changes if c.type == ChangeType.SKIP]
        dg = next((c for c in skips if c.key == "enable_dependency_graph"), None)
        assert dg is not None
        assert "automatic" in dg.reason.lower()

    def test_plan_private_paid_repo_dependency_graph_is_skip_no_api(self):
        # Dependency graph has no writable REST API for private repos
        client = make_mock_client()
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=False, is_paid_plan=True
        )
        plan = plugin.plan()
        skips = [c for c in plan.changes if c.type == ChangeType.SKIP]
        dg = next((c for c in skips if c.key == "enable_dependency_graph"), None)
        assert dg is not None
        assert "no rest api" in dg.reason.lower()

    def test_apply_public_repo_enables_dependabot(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = SecurityPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        plugin.apply(plan)
        assert client.call_json.called
        put_calls = [c for c in client.call_json.call_args_list if c.args[0] == "PUT"]
        vuln_calls = [c for c in put_calls if "vulnerability-alerts" in c.args[1]]
        assert len(vuln_calls) == 1

    def test_plan_public_repo_no_dependabot_when_disabled(self):
        client = make_mock_client()
        config = make_config({("security", "enable_dependabot_alerts"): "false"})
        plugin = SecurityPlugin(client, "alice", "my-repo", config, is_public=True)
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        assert not any(c.key == "dependabot_alerts" for c in adds)

    # --- New security features: public repo ---

    def test_plan_public_repo_has_security_updates_add(self):
        client = make_mock_client()
        plugin = SecurityPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        dep_sec = next((c for c in adds if c.key == "dependabot_security_updates"), None)
        assert dep_sec is not None
        assert dep_sec.new is True

    def test_plan_public_repo_has_private_vuln_reporting_add(self):
        client = make_mock_client()
        plugin = SecurityPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        pvr = next((c for c in adds if c.key == "private_vulnerability_reporting"), None)
        assert pvr is not None
        assert pvr.new is True

    def test_plan_public_repo_has_push_protection_add(self):
        client = make_mock_client()
        plugin = SecurityPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        pp = next((c for c in adds if c.key == "enable_secret_scanning_push_protection"), None)
        assert pp is not None
        assert pp.new is True

    def test_apply_public_repo_enables_all_features(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = SecurityPlugin(client, "alice", "my-repo", make_config(), is_public=True)
        plan = plugin.plan()
        plugin.apply(plan)

        put_calls = [c for c in client.call_json.call_args_list if c.args[0] == "PUT"]
        patch_calls = [c for c in client.call_json.call_args_list if c.args[0] == "PATCH"]

        # PUT calls: vulnerability-alerts, automated-security-fixes, private-vulnerability-reporting
        vuln = [c for c in put_calls if "vulnerability-alerts" in c.args[1]]
        auto_fix = [c for c in put_calls if "automated-security-fixes" in c.args[1]]
        pvr = [c for c in put_calls if "private-vulnerability-reporting" in c.args[1]]
        assert len(vuln) == 1
        assert len(auto_fix) == 1
        assert len(pvr) == 1

        # PATCH call: batched security_and_analysis (push protection only)
        assert len(patch_calls) == 1
        sa_body = patch_calls[0].args[2]["security_and_analysis"]
        assert "secret_scanning_push_protection" in sa_body

    def test_plan_public_repo_no_security_updates_when_disabled(self):
        client = make_mock_client()
        config = make_config({("security", "enable_dependabot_security_updates"): "false"})
        plugin = SecurityPlugin(client, "alice", "my-repo", config, is_public=True)
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        assert not any(c.key == "dependabot_security_updates" for c in adds)

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

    def test_plan_private_paid_repo_has_all_new_features(self):
        client = make_mock_client()
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=False, is_paid_plan=True
        )
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        add_keys = {c.key for c in adds}
        assert "dependabot_security_updates" in add_keys
        assert "private_vulnerability_reporting" in add_keys
        assert "enable_secret_scanning_push_protection" in add_keys
        # dependency_graph should be SKIP (no REST API)
        skips = [c for c in plan.changes if c.type == ChangeType.SKIP]
        assert any(c.key == "enable_dependency_graph" for c in skips)

    def test_apply_private_paid_repo_batches_security_analysis(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=False, is_paid_plan=True
        )
        plan = plugin.plan()
        plugin.apply(plan)
        # PATCH call: secret_scanning + push_protection batched
        patch_calls = [
            c for c in client.call_json.call_args_list if c.args[0] == "PATCH"
        ]
        assert len(patch_calls) == 1
        sa_body = patch_calls[0].args[2]["security_and_analysis"]
        assert sa_body["secret_scanning"] == {"status": "enabled"}
        assert sa_body["secret_scanning_push_protection"] == {"status": "enabled"}

        # PUT calls: dependabot alerts, security updates, private vuln reporting
        put_calls = [c for c in client.call_json.call_args_list if c.args[0] == "PUT"]
        assert any("vulnerability-alerts" in c.args[1] for c in put_calls)
        assert any("automated-security-fixes" in c.args[1] for c in put_calls)
        assert any("private-vulnerability-reporting" in c.args[1] for c in put_calls)


# ── Audit mode: fetch_current_state() and plan(current_state=...) ─────────────


class TestRepositoryPluginAudit:
    def test_fetch_current_state_calls_get_repo_data(self):
        client = make_mock_client()
        client.get_repo_data.return_value = {
            "private": True,
            "has_wiki": False,
            "has_issues": True,
            "has_projects": False,
            "delete_branch_on_merge": True,
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": True,
        }
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config())
        state = plugin.fetch_current_state()
        client.get_repo_data.assert_called_once_with("alice", "my-repo")
        assert state["has_wiki"] is False
        assert state["delete_branch_on_merge"] is True

    def test_plan_audit_no_create_entry(self):
        client = make_mock_client()
        # current_state matches desired → all SKIP, no CREATE
        current_state = {
            "private": True,
            "has_wiki": False,
            "has_issues": True,
            "has_projects": False,
            "delete_branch_on_merge": True,
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": True,
        }
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan(current_state=current_state)
        assert not any(c.key == "repository" for c in plan.changes)

    def test_plan_audit_emits_update_for_diff(self):
        client = make_mock_client()
        # has_wiki differs: current=True, desired=False
        current_state = {
            "private": True,
            "has_wiki": True,
            "has_issues": True,
            "has_projects": False,
            "delete_branch_on_merge": True,
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": True,
        }
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan(current_state=current_state)
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        wiki_change = next((c for c in updates if c.key == "has_wiki"), None)
        assert wiki_change is not None
        assert wiki_change.old is True
        assert wiki_change.new is False

    def test_plan_audit_emits_skip_for_match(self):
        client = make_mock_client()
        # delete_branch_on_merge already True (matches desired)
        current_state = {
            "private": True,
            "has_wiki": False,
            "has_issues": True,
            "has_projects": False,
            "delete_branch_on_merge": True,
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": True,
        }
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan(current_state=current_state)
        skips = [c for c in plan.changes if c.type == ChangeType.SKIP]
        assert any(c.key == "delete_branch_on_merge" for c in skips)
        skip = next(c for c in skips if c.key == "delete_branch_on_merge")
        assert skip.reason == "Already at desired value"

    def test_apply_audit_skips_post(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        # In audit mode plan has no CREATE entry → POST should be skipped
        current_state = {
            "private": True,
            "has_wiki": True,  # differs → UPDATE
            "has_issues": True,
            "has_projects": False,
            "delete_branch_on_merge": True,
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": True,
        }
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan(current_state=current_state)
        plugin.apply(plan)
        post_calls = [c for c in client.call_json.call_args_list if c.args[0] == "POST"]
        assert len(post_calls) == 0
        patch_calls = [c for c in client.call_json.call_args_list if c.args[0] == "PATCH"]
        assert len(patch_calls) == 1

    def test_apply_create_mode_still_posts(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = RepositoryPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan()  # create mode
        plugin.apply(plan)
        post_calls = [c for c in client.call_json.call_args_list if c.args[0] == "POST"]
        assert len(post_calls) == 1


class TestActionsPluginAudit:
    def test_fetch_current_state_calls_get_json(self):
        client = make_mock_client()
        client.get_json.side_effect = [
            {"sha_pinning_required": True, "allowed_actions": "all"},  # /actions/permissions
            {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False},  # /workflow
            {"approval_policy": "all_external_contributors"},  # /fork-pr-contributor-approval
        ]
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        state = plugin.fetch_current_state()
        assert client.get_json.call_count == 3
        assert state["sha_pinning_required"] is True
        assert state["allowed_actions"] == "all"
        assert state["default_workflow_permissions"] == "read"
        assert state["can_approve_pull_request_reviews"] is False
        assert state["fork_pr_approval_policy"] == "all_external_contributors"

    def test_fetch_current_state_selected_fetches_sub_settings(self):
        client = make_mock_client()
        client.get_json.side_effect = [
            {"sha_pinning_required": True, "allowed_actions": "selected"},
            {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False},
            {"github_owned_allowed": True, "verified_allowed": True, "patterns_allowed": ["myorg/*"]},
            {"approval_policy": "first_time_contributors"},
        ]
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        state = plugin.fetch_current_state()
        assert client.get_json.call_count == 4
        assert state["allowed_actions"] == "selected"
        assert state["github_owned_allowed"] is True
        assert state["verified_allowed"] is True
        assert state["patterns_allowed"] == ["myorg/*"]
        assert state["fork_pr_approval_policy"] == "first_time_contributors"

    def test_plan_audit_emits_skip_when_already_desired(self):
        client = make_mock_client()
        # current already matches safe defaults
        current_state = {
            "allowed_actions": "selected",
            "github_owned_allowed": True,
            "verified_allowed": True,
            "patterns_allowed": [],
            "sha_pinning_required": True,
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
            "fork_pr_approval_policy": "all_external_contributors",
        }
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan(current_state=current_state)
        skips = [c for c in plan.changes if c.type == ChangeType.SKIP]
        assert any(c.key == "allowed_actions" for c in skips)
        assert any(c.key == "sha_pinning_required" for c in skips)
        assert any(c.key == "default_workflow_permissions" for c in skips)
        assert any(c.key == "can_approve_pull_request_reviews" for c in skips)
        assert any(c.key == "github_owned_allowed" for c in skips)
        assert any(c.key == "verified_allowed" for c in skips)
        assert any(c.key == "patterns_allowed" for c in skips)
        assert any(c.key == "fork_pr_approval_policy" for c in skips)

    def test_plan_audit_emits_update_when_differs(self):
        client = make_mock_client()
        # current is GitHub defaults, desired is our safe defaults
        current_state = {
            "allowed_actions": "all",
            "sha_pinning_required": False,
            "default_workflow_permissions": "write",
            "can_approve_pull_request_reviews": True,
        }
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan(current_state=current_state)
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        aa = next((c for c in updates if c.key == "allowed_actions"), None)
        assert aa is not None
        assert aa.old == "all"
        assert aa.new == "selected"
        sha = next((c for c in updates if c.key == "sha_pinning_required"), None)
        assert sha is not None
        assert sha.old is False
        assert sha.new is True
        wp = next((c for c in updates if c.key == "default_workflow_permissions"), None)
        assert wp is not None
        assert wp.old == "write"
        assert wp.new == "read"


class TestBranchProtectionPluginAudit:
    def test_fetch_current_state_404_returns_defaults(self):
        client = make_mock_client()
        client.call_api.return_value = (404, "")
        plugin = BranchProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True
        )
        state = plugin.fetch_current_state()
        # 404 = no protection set → permissive defaults
        assert state["allow_force_pushes"] is True
        assert state["allow_deletions"] is True
        assert state["require_pull_request"] is False

    def test_fetch_current_state_200_parses_response(self):
        import json as _json
        client = make_mock_client()
        api_response = {
            "enforce_admins": {"enabled": False},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
            "required_pull_request_reviews": {
                "required_approving_review_count": 1,
                "dismiss_stale_reviews": True,
            },
            "required_conversation_resolution": {"enabled": True},
        }
        client.call_api.return_value = (200, _json.dumps(api_response))
        plugin = BranchProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True
        )
        state = plugin.fetch_current_state()
        assert state["require_pull_request"] is True
        assert state["required_approving_reviews"] == 1
        assert state["dismiss_stale_reviews"] is True
        assert state["allow_force_pushes"] is False
        assert state["allow_deletions"] is False
        assert state["require_conversation_resolution"] is True

    def test_plan_audit_emits_skip_when_already_protected(self):
        client = make_mock_client()
        # current already matches safe defaults
        current_state = {
            "require_pull_request": True,
            "required_approving_reviews": 1,
            "dismiss_stale_reviews": True,
            "require_conversation_resolution": True,
            "enforce_admins": False,
            "allow_force_pushes": False,
            "allow_deletions": False,
        }
        plugin = BranchProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True
        )
        plan = plugin.plan(current_state=current_state)
        assert all(c.type == ChangeType.SKIP for c in plan.changes)

    def test_plan_audit_emits_update_for_diff(self):
        client = make_mock_client()
        # force pushes still allowed → should become UPDATE
        current_state = {
            "require_pull_request": True,
            "required_approving_reviews": 1,
            "dismiss_stale_reviews": True,
            "require_conversation_resolution": True,
            "enforce_admins": False,
            "allow_force_pushes": True,  # differs from desired (False)
            "allow_deletions": False,
        }
        plugin = BranchProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True
        )
        plan = plugin.plan(current_state=current_state)
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        fp = next((c for c in updates if c.key == "allow_force_pushes"), None)
        assert fp is not None
        assert fp.old is True
        assert fp.new is False

    def test_plan_audit_private_free_still_skips(self):
        client = make_mock_client()
        current_state = dict(
            require_pull_request=False,
            required_approving_reviews=0,
            dismiss_stale_reviews=False,
            require_conversation_resolution=False,
            enforce_admins=False,
            allow_force_pushes=True,
            allow_deletions=True,
        )
        plugin = BranchProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=False, is_paid_plan=False
        )
        plan = plugin.plan(current_state=current_state)
        assert all(c.type == ChangeType.SKIP for c in plan.changes)


class TestSecurityPluginAudit:
    def _full_current_state(self, **overrides):
        """Build a current_state dict with all keys defaulting to True."""
        state = {
            "dependabot_alerts": True,
            "secret_scanning": True,
            "dependabot_security_updates": True,
            "private_vulnerability_reporting": True,
            "enable_dependency_graph": True,
            "enable_secret_scanning_push_protection": True,
        }
        state.update(overrides)
        return state

    def test_fetch_current_state_dependabot_enabled(self):
        client = make_mock_client()
        # call_api: vulnerability-alerts (204), automated-security-fixes (200),
        # private-vulnerability-reporting (204)
        client.call_api.side_effect = [(204, ""), (200, ""), (204, "")]
        client.get_repo_data.return_value = {
            "security_and_analysis": {
                "secret_scanning_push_protection": {"status": "enabled"},
            }
        }
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=True
        )
        state = plugin.fetch_current_state()
        assert state["dependabot_alerts"] is True
        assert state["secret_scanning"] is True
        assert state["dependabot_security_updates"] is True
        assert state["private_vulnerability_reporting"] is True

    def test_fetch_current_state_dependabot_disabled(self):
        client = make_mock_client()
        client.call_api.side_effect = [(404, ""), (404, ""), (404, "")]
        client.get_repo_data.return_value = {"security_and_analysis": {}}
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=True
        )
        state = plugin.fetch_current_state()
        assert state["dependabot_alerts"] is False

    def test_fetch_current_state_private_secret_scanning(self):
        client = make_mock_client()
        # vulnerability-alerts (204), automated-security-fixes (200),
        # private-vulnerability-reporting (404)
        client.call_api.side_effect = [(204, ""), (200, ""), (404, "")]
        client.get_repo_data.return_value = {
            "security_and_analysis": {
                "secret_scanning": {"status": "enabled"},
                "secret_scanning_push_protection": {"status": "enabled"},
            }
        }
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=False, is_paid_plan=True
        )
        state = plugin.fetch_current_state()
        assert state["dependabot_alerts"] is True
        assert state["secret_scanning"] is True
        assert state["enable_secret_scanning_push_protection"] is True
        assert state["private_vulnerability_reporting"] is False

    def test_plan_audit_emits_skip_when_already_enabled(self):
        client = make_mock_client()
        current_state = self._full_current_state()
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=True
        )
        plan = plugin.plan(current_state=current_state)
        # dependabot should be SKIP; secret_scanning is always SKIP for public
        skips = [c for c in plan.changes if c.type == ChangeType.SKIP]
        assert any(c.key == "dependabot_alerts" for c in skips)
        assert any(c.key == "secret_scanning" for c in skips)
        assert any(c.key == "dependabot_security_updates" for c in skips)

    def test_plan_audit_emits_update_when_dependabot_disabled(self):
        client = make_mock_client()
        current_state = self._full_current_state(dependabot_alerts=False)
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=True
        )
        plan = plugin.plan(current_state=current_state)
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        dep = next((c for c in updates if c.key == "dependabot_alerts"), None)
        assert dep is not None
        assert dep.old is False
        assert dep.new is True

    def test_plan_audit_private_paid_secret_scanning_update(self):
        client = make_mock_client()
        current_state = self._full_current_state(secret_scanning=False)
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=False, is_paid_plan=True
        )
        plan = plugin.plan(current_state=current_state)
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        ss = next((c for c in updates if c.key == "secret_scanning"), None)
        assert ss is not None
        assert ss.old is False
        assert ss.new is True

    def test_plan_audit_emits_update_for_push_protection(self):
        client = make_mock_client()
        current_state = self._full_current_state(
            enable_secret_scanning_push_protection=False
        )
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=True
        )
        plan = plugin.plan(current_state=current_state)
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        pp = next((c for c in updates if c.key == "enable_secret_scanning_push_protection"), None)
        assert pp is not None
        assert pp.old is False
        assert pp.new is True


# ── TagProtectionPlugin ──────────────────────────────────────────────────────


class TestTagProtectionPlugin:
    def test_plan_private_free_repo_emits_skip(self):
        client = make_mock_client()
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", make_config(),
            is_public=False, is_paid_plan=False,
        )
        plan = plugin.plan()
        assert all(c.type == ChangeType.SKIP for c in plan.changes)
        assert any("paid" in (c.reason or "").lower() or "public" in (c.reason or "").lower()
                    for c in plan.changes)

    def test_plan_public_repo_emits_add_changes(self):
        client = make_mock_client()
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True,
        )
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        assert len(adds) > 0
        assert any(c.key == "protected_tags" for c in adds)
        assert any(c.key == "prevent_tag_deletion" for c in adds)
        assert any(c.key == "prevent_tag_update" for c in adds)

    def test_plan_paid_private_repo_emits_add_changes(self):
        client = make_mock_client()
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", make_config(),
            is_public=False, is_paid_plan=True,
        )
        plan = plugin.plan()
        adds = [c for c in plan.changes if c.type == ChangeType.ADD]
        assert len(adds) > 0

    def test_apply_posts_tag_ruleset(self):
        client = make_mock_client()
        client.call_json.return_value = {}
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True,
        )
        plan = plugin.plan()
        plugin.apply(plan)
        assert client.call_json.called
        call = client.call_json.call_args
        assert call.args[0] == "POST"
        assert call.args[1].endswith("/rulesets")
        body = call.args[2]
        assert body["target"] == "tag"
        assert body["name"] == "gh-safe-repo tag defaults"

    def test_ruleset_body_includes_deletion_and_update_rules(self):
        client = make_mock_client()
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True,
        )
        desired = plugin._desired()
        body = plugin._build_tag_ruleset_body(desired)
        rule_types = [r["type"] for r in body["rules"]]
        assert "deletion" in rule_types
        assert "update" in rule_types

    def test_ruleset_body_tag_pattern_from_config(self):
        client = make_mock_client()
        config = make_config({("tag_protection", "protected_tags"): "v*, release-*"})
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", config, is_public=True,
        )
        desired = plugin._desired()
        body = plugin._build_tag_ruleset_body(desired)
        includes = body["conditions"]["ref_name"]["include"]
        assert "refs/tags/v*" in includes
        assert "refs/tags/release-*" in includes

    def test_ruleset_body_admin_bypass(self):
        client = make_mock_client()
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True,
        )
        desired = plugin._desired()
        body = plugin._build_tag_ruleset_body(desired)
        assert len(body["bypass_actors"]) == 1
        assert body["bypass_actors"][0]["actor_id"] == 5

    def test_apply_noop_when_no_actionable_changes(self):
        client = make_mock_client()
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", make_config(),
            is_public=False, is_paid_plan=False,
        )
        plan = plugin.plan()  # All SKIP
        plugin.apply(plan)
        assert not client.call_json.called

    def test_config_disable_deletion_protection(self):
        client = make_mock_client()
        config = make_config({("tag_protection", "prevent_tag_deletion"): "false"})
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", config, is_public=True,
        )
        desired = plugin._desired()
        body = plugin._build_tag_ruleset_body(desired)
        rule_types = [r["type"] for r in body["rules"]]
        assert "deletion" not in rule_types
        assert "update" in rule_types

    def test_category_is_tag_protection(self):
        client = make_mock_client()
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True,
        )
        plan = plugin.plan()
        assert all(c.category == ChangeCategory.TAG_PROTECTION for c in plan.changes)


class TestTagProtectionPluginAudit:
    def test_fetch_current_state_no_rulesets(self):
        client = make_mock_client()
        client.call_api.return_value = (200, "[]")
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True,
        )
        state = plugin.fetch_current_state()
        assert state["prevent_tag_deletion"] is False
        assert state["prevent_tag_update"] is False

    def test_fetch_current_state_404(self):
        client = make_mock_client()
        client.call_api.return_value = (404, "")
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True,
        )
        state = plugin.fetch_current_state()
        assert state["prevent_tag_deletion"] is False

    def test_fetch_current_state_with_existing_ruleset(self):
        client = make_mock_client()
        # First call: list rulesets
        list_response = json.dumps([
            {"id": 42, "name": "gh-safe-repo tag defaults", "target": "tag"},
        ])
        # Second call: ruleset detail
        detail_response = json.dumps({
            "id": 42,
            "name": "gh-safe-repo tag defaults",
            "target": "tag",
            "rules": [{"type": "deletion"}, {"type": "update"}],
        })
        client.call_api.side_effect = [
            (200, list_response),
            (200, detail_response),
        ]
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True,
        )
        state = plugin.fetch_current_state()
        assert state["prevent_tag_deletion"] is True
        assert state["prevent_tag_update"] is True

    def test_plan_audit_emits_skip_when_already_protected(self):
        client = make_mock_client()
        current_state = {"prevent_tag_deletion": True, "prevent_tag_update": True}
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True,
        )
        plan = plugin.plan(current_state=current_state)
        skips = [c for c in plan.changes if c.type == ChangeType.SKIP]
        assert len(skips) == 2

    def test_plan_audit_emits_update_when_not_protected(self):
        client = make_mock_client()
        current_state = {"prevent_tag_deletion": False, "prevent_tag_update": False}
        plugin = TagProtectionPlugin(
            client, "alice", "my-repo", make_config(), is_public=True,
        )
        plan = plugin.plan(current_state=current_state)
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        assert len(updates) == 2
        assert any(c.key == "prevent_tag_deletion" for c in updates)
        assert any(c.key == "prevent_tag_update" for c in updates)
