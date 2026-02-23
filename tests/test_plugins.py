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


# ── Audit mode: fetch_current_state() and plan(current_state=...) ─────────────


class TestRepositoryPluginAudit:
    def test_fetch_current_state_calls_get_json(self):
        client = make_mock_client()
        client.get_json.return_value = {
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
        client.get_json.assert_called_once_with("/repos/alice/my-repo")
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
        client.get_json.return_value = {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        }
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        state = plugin.fetch_current_state()
        client.get_json.assert_called_once_with(
            "/repos/alice/my-repo/actions/permissions/workflow"
        )
        assert state["default_workflow_permissions"] == "read"
        assert state["can_approve_pull_request_reviews"] is False

    def test_plan_audit_emits_skip_when_already_desired(self):
        client = make_mock_client()
        # current already matches safe defaults
        current_state = {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        }
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan(current_state=current_state)
        skips = [c for c in plan.changes if c.type == ChangeType.SKIP]
        assert any(c.key == "default_workflow_permissions" for c in skips)
        assert any(c.key == "can_approve_pull_request_reviews" for c in skips)

    def test_plan_audit_emits_update_when_differs(self):
        client = make_mock_client()
        # current is GitHub default (write), desired is read
        current_state = {
            "default_workflow_permissions": "write",
            "can_approve_pull_request_reviews": True,
        }
        plugin = ActionsPlugin(client, "alice", "my-repo", make_config())
        plan = plugin.plan(current_state=current_state)
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
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
    def test_fetch_current_state_dependabot_enabled(self):
        client = make_mock_client()
        # 204 = Dependabot enabled; public repo so secret scanning always true
        client.call_api.return_value = (204, "")
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=True
        )
        state = plugin.fetch_current_state()
        assert state["dependabot_alerts"] is True
        assert state["secret_scanning"] is True

    def test_fetch_current_state_dependabot_disabled(self):
        client = make_mock_client()
        client.call_api.return_value = (404, "")
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=True
        )
        state = plugin.fetch_current_state()
        assert state["dependabot_alerts"] is False

    def test_fetch_current_state_private_secret_scanning(self):
        import json as _json
        client = make_mock_client()
        client.call_api.return_value = (204, "")
        client.get_json.return_value = {
            "security_and_analysis": {"secret_scanning": {"status": "enabled"}}
        }
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=False, is_paid_plan=True
        )
        state = plugin.fetch_current_state()
        assert state["dependabot_alerts"] is True
        assert state["secret_scanning"] is True

    def test_plan_audit_emits_skip_when_already_enabled(self):
        client = make_mock_client()
        current_state = {"dependabot_alerts": True, "secret_scanning": True}
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=True
        )
        plan = plugin.plan(current_state=current_state)
        # dependabot should be SKIP; secret_scanning is always SKIP for public
        skips = [c for c in plan.changes if c.type == ChangeType.SKIP]
        assert any(c.key == "dependabot_alerts" for c in skips)
        assert any(c.key == "secret_scanning" for c in skips)

    def test_plan_audit_emits_update_when_dependabot_disabled(self):
        client = make_mock_client()
        current_state = {"dependabot_alerts": False, "secret_scanning": True}
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
        current_state = {"dependabot_alerts": True, "secret_scanning": False}
        plugin = SecurityPlugin(
            client, "alice", "my-repo", make_config(), is_public=False, is_paid_plan=True
        )
        plan = plugin.plan(current_state=current_state)
        updates = [c for c in plan.changes if c.type == ChangeType.UPDATE]
        ss = next((c for c in updates if c.key == "secret_scanning"), None)
        assert ss is not None
        assert ss.old is False
        assert ss.new is True
