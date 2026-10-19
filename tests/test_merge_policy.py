"""Tests for the fix/audit three-way merge (docs/2026-03-28-user-stories.md).

Metadata (SETTING_TIERS / SAFE_DIRECTIONS in config_manager.py), the policy
function (commands/fix.py:apply_merge_policy) and the plugins' part of the
contract (annotated rows, PRESERVE values carried into composite writes).
"""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from gh_safe_repo.commands.fix import apply_merge_policy
from gh_safe_repo.config_manager import (
    MERGE_SECTIONS,
    PREFERENCE,
    SAFE_DEFAULTS,
    SAFE_DIRECTIONS,
    SECURITY,
    SETTING_TIERS,
    ConfigManager,
    has_direction,
    is_stricter,
    setting_tier,
)
from gh_safe_repo.diff import Change, ChangeCategory, ChangeType, Plan
from gh_safe_repo.plugins import actions, branch_protection, repository, security, tag_protection
from gh_safe_repo.plugins.actions import ActionsPlugin
from gh_safe_repo.plugins.branch_protection import BranchProtectionPlugin
from gh_safe_repo.plugins.repository import RepositoryPlugin
from gh_safe_repo.plugins.security import SecurityPlugin
from gh_safe_repo.plugins.tag_protection import TagProtectionPlugin

PLUGIN_MODULES = {
    "repo": repository,
    "actions": actions,
    "branch_protection": branch_protection,
    "security": security,
    "tag_protection": tag_protection,
}

# The one security-tier key that is scope (which tags), not strictness.
_UNDIRECTED = {("tag_protection", "protected_tags")}


def make_config(ini_text=None, tmp_path=None, overrides=None):
    if ini_text is None:
        config = ConfigManager(config_path="/tmp/nonexistent-gh-safe-repo.ini")
    else:
        path = tmp_path / "gh-safe-repo.ini"
        path.write_text(ini_text)
        config = ConfigManager(config_path=str(path))
    if overrides:
        config.apply_overrides(overrides)
    return config


def make_client():
    client = MagicMock()
    client.repo_path.side_effect = lambda owner, repo, suffix="": (
        f"/repos/{owner}/{repo}/{suffix.lstrip('/')}" if suffix else f"/repos/{owner}/{repo}"
    )
    client.call_api.return_value = (404, "")
    client.call_json.return_value = {}
    return client


def update(category, key, old, new, *, tier, default, **kw):
    return Change(
        type=ChangeType.UPDATE, category=category, key=key, old=old, new=new,
        tier=tier, github_default=default, **kw,
    )


def by_key(plan):
    return {c.key: c for c in plan.changes}


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

class TestTierCoverage:
    """Every managed setting has a tier; tool-run sections have none."""

    @pytest.mark.parametrize(
        "section,key",
        [(s, k) for s in MERGE_SECTIONS for k in SAFE_DEFAULTS[s]],
    )
    def test_every_safe_default_has_an_explicit_tier(self, section, key):
        assert (section, key) in SETTING_TIERS
        assert SETTING_TIERS[(section, key)] in (SECURITY, PREFERENCE)

    @pytest.mark.parametrize("section", ["pre_flight_scan", "git_transport"])
    def test_tool_run_sections_are_outside_the_merge(self, section):
        assert section in SAFE_DEFAULTS
        assert section not in MERGE_SECTIONS
        assert not any(s == section for s, _ in SETTING_TIERS)
        for key in SAFE_DEFAULTS[section]:
            assert setting_tier(section, key) is None

    @pytest.mark.parametrize("section,module", list(PLUGIN_MODULES.items()))
    def test_every_plugin_github_default_has_a_tier(self, section, module):
        for key in module.GITHUB_DEFAULTS:
            assert setting_tier(section, key) in (SECURITY, PREFERENCE), (section, key)

    @pytest.mark.parametrize("section,module", list(PLUGIN_MODULES.items()))
    def test_plugin_section_matches_module(self, section, module):
        cls = {
            "repo": RepositoryPlugin, "actions": ActionsPlugin,
            "branch_protection": BranchProtectionPlugin,
            "security": SecurityPlugin, "tag_protection": TagProtectionPlugin,
        }[section]
        assert cls.section == section
        assert cls.github_defaults is module.GITHUB_DEFAULTS

    def test_doc_classification(self):
        assert setting_tier("repo", "has_wiki") == PREFERENCE
        assert setting_tier("actions", "enabled") == PREFERENCE
        assert setting_tier("actions", "allowed_actions") == SECURITY
        assert setting_tier("branch_protection", "enforce_admins") == PREFERENCE
        assert setting_tier("branch_protection", "require_conversation_resolution") == PREFERENCE
        assert setting_tier("branch_protection", "use_rulesets") == PREFERENCE
        assert setting_tier("branch_protection", "allow_force_pushes") == SECURITY
        assert setting_tier("tag_protection", "prevent_tag_update") == SECURITY
        assert setting_tier("security", "dependabot_alerts") == SECURITY

    def test_unknown_key_falls_back_to_section_tier(self):
        assert setting_tier("repo", "brand_new_key") == PREFERENCE
        assert setting_tier("security", "brand_new_key") == SECURITY


class TestDirectionCoverage:
    @pytest.mark.parametrize(
        "section,key",
        [sk for sk, tier in SETTING_TIERS.items() if tier == SECURITY],
    )
    def test_every_security_setting_has_a_direction(self, section, key):
        if (section, key) in _UNDIRECTED:
            assert not has_direction(section, key)
        else:
            assert has_direction(section, key), (section, key)

    def test_directions_only_for_security_settings(self):
        for sk in SAFE_DIRECTIONS:
            assert SETTING_TIERS.get(sk) == SECURITY, sk


class TestIsStricter:
    @pytest.mark.parametrize("value,than,expected", [
        (3, 1, True), (1, 3, False), (1, 1, False), ("2", 1, True), ("x", 1, False),
    ])
    def test_numeric_higher_is_stricter(self, value, than, expected):
        assert is_stricter("branch_protection", "required_approving_reviews", value, than) is expected

    @pytest.mark.parametrize("value,than,expected", [
        (True, False, True), (False, True, False), (True, True, False), ("true", False, True),
    ])
    def test_boolean_true_is_strict(self, value, than, expected):
        assert is_stricter("branch_protection", "require_pull_request", value, than) is expected

    @pytest.mark.parametrize("value,than,expected", [
        (False, True, True), (True, False, False), (False, False, False),
    ])
    def test_boolean_false_is_strict(self, value, than, expected):
        assert is_stricter("branch_protection", "allow_force_pushes", value, than) is expected

    @pytest.mark.parametrize("value,than,expected", [
        ("selected", "all", True), ("local_only", "selected", True),
        ("all", "selected", False), ("selected", "selected", False), ("bogus", "all", False),
    ])
    def test_allowed_actions_ordering(self, value, than, expected):
        assert is_stricter("actions", "allowed_actions", value, than) is expected

    def test_workflow_permissions_read_is_stricter(self):
        assert is_stricter("actions", "default_workflow_permissions", "read", "write")
        assert not is_stricter("actions", "default_workflow_permissions", "write", "read")

    def test_fork_policy_ordering(self):
        assert is_stricter(
            "actions", "fork_pr_approval_policy",
            "all_external_contributors", "first_time_contributors",
        )

    def test_patterns_subset_is_stricter(self):
        assert is_stricter("actions", "patterns_allowed", [], ["a/*"])
        assert is_stricter("actions", "patterns_allowed", ["a/*"], ["a/*", "b/*"])
        assert not is_stricter("actions", "patterns_allowed", ["a/*"], ["a/*"])
        assert not is_stricter("actions", "patterns_allowed", ["a/*", "b/*"], ["a/*"])

    def test_no_direction_is_never_stricter(self):
        assert not is_stricter("repo", "has_wiki", False, True)
        assert not is_stricter("tag_protection", "protected_tags", "v*", "*")


class TestDeclaredSections:
    def test_no_file_declares_nothing(self):
        config = make_config()
        assert config.declared_sections == frozenset()
        assert not config.is_declared("repo")

    def test_sections_in_the_user_file_are_declared(self, tmp_path):
        config = make_config("[repo]\nhas_wiki = true\n\n[actions]\n", tmp_path)
        assert config.declared_sections == {"repo", "actions"}
        assert config.is_declared("repo")
        assert config.is_declared("actions")
        assert not config.is_declared("branch_protection")

    def test_cli_overrides_do_not_declare(self):
        config = make_config(overrides={("repo", "private"): "false"})
        assert not config.is_declared("repo")

    def test_defaults_still_seeded_for_undeclared_sections(self, tmp_path):
        config = make_config("[repo]\nhas_wiki = true\n", tmp_path)
        assert config.get("actions", "allowed_actions") == "selected"


# ---------------------------------------------------------------------------
# apply_merge_policy: the merge table
# ---------------------------------------------------------------------------

class TestMergeTable:
    """One test per row of the doc's merge table, both tiers, both directions."""

    def _one(self, change, config=None, all_=False):
        plan = Plan()
        plan.add(change)
        out = apply_merge_policy(plan, config or make_config(), all_=all_)
        assert len(out.changes) == 1
        return out.changes[0]

    def test_at_github_default_is_applied(self):
        c = self._one(update(ChangeCategory.REPO, "has_wiki", True, False,
                             tier=PREFERENCE, default=True))
        assert c.type == ChangeType.UPDATE
        assert c.weakened is False

    def test_preference_conflict_is_preserved(self):
        c = self._one(update(ChangeCategory.REPO, "has_wiki", False, True,
                             tier=PREFERENCE, default=True))
        assert c.type == ChangeType.PRESERVE
        assert c.reason == "changed from default"
        assert c.old is False and c.new is True  # values kept for the table

    def test_security_stricter_is_preserved(self):
        c = self._one(update(ChangeCategory.BRANCH_PROTECTION, "required_approving_reviews",
                             3, 1, tier=SECURITY, default=0))
        assert c.type == ChangeType.PRESERVE
        assert c.reason == "stricter than 1"

    def test_security_weaker_is_applied_and_flagged(self):
        # GitHub default for verified_allowed is False (strict); a human
        # allowed verified creators, the config says not to.
        c = self._one(update(ChangeCategory.ACTIONS, "verified_allowed", True, False,
                             tier=SECURITY, default=False))
        assert c.type == ChangeType.UPDATE
        assert c.weakened is True

    def test_security_weaker_enum_is_flagged(self):
        c = self._one(update(ChangeCategory.ACTIONS, "allowed_actions", "selected", "local_only",
                             tier=SECURITY, default="all"))
        assert c.type == ChangeType.UPDATE and c.weakened is True

    def test_security_without_direction_is_applied_unflagged(self):
        c = self._one(update(ChangeCategory.TAG_PROTECTION, "protected_tags", "v*", "*",
                             tier=SECURITY, default="*"))
        assert c.type == ChangeType.UPDATE and c.weakened is False

    def test_declared_section_overrides_preference_preserve(self, tmp_path):
        config = make_config("[repo]\nhas_wiki = true\n", tmp_path)
        c = self._one(update(ChangeCategory.REPO, "has_wiki", False, True,
                             tier=PREFERENCE, default=True), config)
        assert c.type == ChangeType.UPDATE and c.weakened is False

    def test_declared_section_overrides_stricter_security_preserve(self, tmp_path):
        config = make_config("[branch_protection]\nrequired_approving_reviews = 1\n", tmp_path)
        c = self._one(update(ChangeCategory.BRANCH_PROTECTION, "required_approving_reviews",
                             3, 1, tier=SECURITY, default=0), config)
        assert c.type == ChangeType.UPDATE and c.weakened is False

    def test_declared_section_keeps_weakened_flag(self, tmp_path):
        config = make_config("[actions]\nverified_allowed = false\n", tmp_path)
        c = self._one(update(ChangeCategory.ACTIONS, "verified_allowed", True, False,
                             tier=SECURITY, default=False), config)
        assert c.type == ChangeType.UPDATE and c.weakened is True

    def test_declaring_another_section_does_not_override(self, tmp_path):
        config = make_config("[actions]\n", tmp_path)
        c = self._one(update(ChangeCategory.REPO, "has_wiki", False, True,
                             tier=PREFERENCE, default=True), config)
        assert c.type == ChangeType.PRESERVE

    def test_all_flag_skips_the_policy(self):
        plan = Plan()
        plan.add(update(ChangeCategory.REPO, "has_wiki", False, True, tier=PREFERENCE, default=True))
        plan.add(update(ChangeCategory.BRANCH_PROTECTION, "required_approving_reviews", 3, 1,
                        tier=SECURITY, default=0))
        plan.add(update(ChangeCategory.ACTIONS, "verified_allowed", True, False,
                        tier=SECURITY, default=False))
        out = apply_merge_policy(plan, make_config(), all_=True)
        assert [c.type for c in out.changes] == [ChangeType.UPDATE] * 3
        assert not any(c.weakened for c in out.changes)

    @pytest.mark.parametrize("change", [
        Change(type=ChangeType.SKIP, category=ChangeCategory.SECURITY, key="x", reason="r"),
        Change(type=ChangeType.ADD, category=ChangeCategory.REPO, key="repository", new="a/r"),
        Change(type=ChangeType.DELETE, category=ChangeCategory.FILE, key="readme", old="README.md"),
        # UPDATE with no merge context (a mock plan, or a key without a default)
        Change(type=ChangeType.UPDATE, category=ChangeCategory.REPO, key="has_wiki", old=False, new=True),
    ])
    def test_rows_without_context_pass_through(self, change):
        c = self._one(change)
        assert c is change

    def test_input_plan_is_not_mutated(self):
        original = update(ChangeCategory.REPO, "has_wiki", False, True, tier=PREFERENCE, default=True)
        plan = Plan()
        plan.add(original)
        apply_merge_policy(plan, make_config())
        assert original.type == ChangeType.UPDATE
        assert plan.changes[0] is original

    def test_preserve_is_not_actionable(self):
        plan = Plan()
        plan.add(update(ChangeCategory.REPO, "has_wiki", False, True, tier=PREFERENCE, default=True))
        plan.add(update(ChangeCategory.REPO, "has_issues", True, False, tier=PREFERENCE, default=True))
        out = apply_merge_policy(plan, make_config())
        assert [c.key for c in out.actionable_changes] == ["has_issues"]
        assert [c.key for c in out.preserved_changes] == ["has_wiki"]
        assert out.has_changes()
        assert out.count_by_type() == {ChangeType.PRESERVE: 1, ChangeType.UPDATE: 1}


# ---------------------------------------------------------------------------
# Plugins: annotated rows and composite writes
# ---------------------------------------------------------------------------

class TestPluginAnnotations:
    def test_repository_rows_carry_tier_and_default(self):
        plugin = RepositoryPlugin(make_client(), "a", "r", make_config(
            overrides={("repo", "has_wiki"): "false"}))
        plan = plugin.plan(current_state={
            "has_wiki": True, "delete_branch_on_merge": True,
            "allow_squash_merge": True, "allow_merge_commit": True, "allow_rebase_merge": True,
        })
        rows = by_key(plan)
        assert rows["has_wiki"].tier == PREFERENCE and rows["has_wiki"].github_default is True
        assert rows["delete_branch_on_merge"].github_default is False

    def test_create_mode_rows_are_annotated_but_never_preserved(self):
        plugin = RepositoryPlugin(make_client(), "a", "r", make_config(
            overrides={("repo", "has_wiki"): "false"}))
        plan = plugin.plan()  # create mode: baseline is GITHUB_DEFAULTS
        out = apply_merge_policy(plan, make_config())
        assert not out.preserved_changes
        assert by_key(out)["has_wiki"].type == ChangeType.UPDATE

    def test_actions_rows_carry_tier_and_default(self):
        plugin = ActionsPlugin(make_client(), "a", "r", make_config(), is_public=False)
        plan = plugin.plan(current_state={
            "allowed_actions": "all", "sha_pinning_required": False,
            "default_workflow_permissions": "write",
            "can_approve_pull_request_reviews": True, "fork_pr_approval_policy": None,
        })
        rows = by_key(plan)
        assert rows["allowed_actions"].tier == SECURITY
        assert rows["allowed_actions"].github_default == "all"
        assert rows["default_workflow_permissions"].github_default == "write"
        assert rows["fork_pr_approval_policy"].type == ChangeType.SKIP

    def test_branch_protection_rows_carry_tier_and_default(self):
        plugin = BranchProtectionPlugin(make_client(), "a", "r", make_config(), is_public=True)
        plan = plugin.plan(current_state=dict(branch_protection.GITHUB_DEFAULTS))
        rows = by_key(plan)
        assert rows["required_approving_reviews"].tier == SECURITY
        assert rows["required_approving_reviews"].github_default == 0
        assert rows["enforce_admins"].tier == PREFERENCE

    def test_tag_protection_rows_carry_tier_and_default(self):
        plugin = TagProtectionPlugin(make_client(), "a", "r", make_config(), is_public=True)
        plan = plugin.plan(current_state=dict(tag_protection.GITHUB_DEFAULTS))
        rows = by_key(plan)
        assert rows["prevent_tag_deletion"].tier == SECURITY
        assert rows["prevent_tag_deletion"].github_default is False

    def test_security_rows_carry_tier_and_default(self):
        plugin = SecurityPlugin(make_client(), "a", "r", make_config(), is_public=True)
        plan = plugin.plan(current_state={
            "dependabot_alerts": False, "dependabot_security_updates": True,
            "private_vulnerability_reporting": False,
            "enable_secret_scanning_push_protection": False,
        })
        rows = by_key(plan)
        assert rows["dependabot_alerts"].type == ChangeType.UPDATE
        assert rows["dependabot_alerts"].tier == SECURITY
        assert rows["dependabot_alerts"].github_default is False
        assert rows["dependabot_security_updates"].type == ChangeType.SKIP

    def test_security_disabled_toggle_is_never_weakened(self):
        plugin = SecurityPlugin(make_client(), "a", "r", make_config(), is_public=True)
        plan = plugin.plan(current_state={"dependabot_alerts": False})
        out = apply_merge_policy(plan, make_config())
        row = by_key(out)["dependabot_alerts"]
        assert row.type == ChangeType.UPDATE and row.weakened is False


class TestCompositeWrites:
    """A ruleset is one body: PRESERVEd keys must carry the human's value."""

    BP_STATE = {
        "require_pull_request": True, "required_approving_reviews": 3,
        "dismiss_stale_reviews": True, "require_conversation_resolution": True,
        "enforce_admins": True, "allow_force_pushes": True, "allow_deletions": False,
    }

    def _merged_bp(self, config=None, all_=False):
        client = make_client()
        plugin = BranchProtectionPlugin(client, "a", "r", config or make_config(), is_public=True)
        plan = apply_merge_policy(plugin.plan(current_state=dict(self.BP_STATE)),
                                  config or make_config(), all_=all_)
        return client, plugin, plan

    def test_policy_outcome(self):
        _, _, plan = self._merged_bp()
        rows = by_key(plan)
        assert rows["required_approving_reviews"].type == ChangeType.PRESERVE
        assert rows["enforce_admins"].type == ChangeType.PRESERVE
        assert rows["allow_force_pushes"].type == ChangeType.UPDATE  # at GH default
        assert rows["allow_force_pushes"].weakened is False

    def test_ruleset_body_carries_preserved_values(self):
        client, plugin, plan = self._merged_bp()
        plugin.apply(plan)
        method, path, body = client.call_json.call_args.args
        assert method == "POST" and path.endswith("/rulesets")
        pr = next(r for r in body["rules"] if r["type"] == "pull_request")
        assert pr["parameters"]["required_approving_review_count"] == 3  # not 1
        assert body["bypass_actors"] == []  # enforce_admins=True kept
        assert any(r["type"] == "non_fast_forward" for r in body["rules"])  # the fix

    def test_all_writes_desired_values(self):
        client, plugin, plan = self._merged_bp(all_=True)
        assert not plan.preserved_changes
        plugin.apply(plan)
        body = client.call_json.call_args.args[2]
        pr = next(r for r in body["rules"] if r["type"] == "pull_request")
        assert pr["parameters"]["required_approving_review_count"] == 1
        assert body["bypass_actors"][0]["actor_id"] == 5

    def test_classic_body_carries_preserved_values(self):
        config = make_config(overrides={("branch_protection", "use_rulesets"): "false"})
        client, plugin, plan = self._merged_bp(config)
        plugin.apply(plan)
        body = client.call_json.call_args.args[2]
        assert body["required_pull_request_reviews"]["required_approving_review_count"] == 3
        assert body["enforce_admins"] is True
        assert body["allow_force_pushes"] is False

    def test_only_preserved_rows_means_no_write(self):
        client = make_client()
        plugin = BranchProtectionPlugin(client, "a", "r", make_config(), is_public=True)
        state = dict(self.BP_STATE, allow_force_pushes=False)  # only reviews/admins differ
        plan = apply_merge_policy(plugin.plan(current_state=state), make_config())
        assert not plan.actionable_changes and plan.preserved_changes
        plugin.apply(plan)
        client.call_json.assert_not_called()

    def test_tag_ruleset_body_carries_preserved_values(self):
        config = make_config(overrides={("tag_protection", "prevent_tag_update"): "false"})
        client = make_client()
        plugin = TagProtectionPlugin(client, "a", "r", config, is_public=True)
        # A human made tags immutable (update rule present); config says only deletion.
        plan = apply_merge_policy(
            plugin.plan(current_state={"prevent_tag_deletion": False, "prevent_tag_update": True}),
            config,
        )
        rows = by_key(plan)
        assert rows["prevent_tag_update"].type == ChangeType.PRESERVE
        assert rows["prevent_tag_deletion"].type == ChangeType.UPDATE
        plugin.apply(plan)
        body = client.call_json.call_args.args[2]
        assert {r["type"] for r in body["rules"]} == {"deletion", "update"}

    def test_repository_patch_omits_preserved_keys(self):
        client = make_client()
        config = make_config(overrides={("repo", "has_wiki"): "false"})
        plugin = RepositoryPlugin(client, "a", "r", config)
        plan = apply_merge_policy(plugin.plan(current_state={
            "has_wiki": True, "delete_branch_on_merge": True,
            "allow_squash_merge": True, "allow_merge_commit": True, "allow_rebase_merge": True,
        }), config)
        plugin.apply(plan)
        body = client.call_json.call_args.args[2]
        assert body == {"has_wiki": False}
