"""
Actions plugin: GitHub Actions permissions and workflow permissions.

Three API calls:
  PUT actions/permissions           — enabled, allowed_actions, sha_pinning_required
  PUT actions/permissions/selected-actions — github_owned_allowed, verified_allowed, patterns_allowed
  PUT actions/permissions/workflow   — default_workflow_permissions, can_approve_pull_request_reviews
"""

from ..diff import Change, ChangeCategory, ChangeType, Plan
from .base import BasePlugin

# GitHub defaults for Actions on a new repo
GITHUB_DEFAULTS = {
    "enabled": True,
    "allowed_actions": "all",
    "sha_pinning_required": False,
    "default_workflow_permissions": "write",
    "can_approve_pull_request_reviews": True,
    # selected-actions sub-settings (only relevant when allowed_actions == "selected")
    "github_owned_allowed": True,
    "verified_allowed": False,
    "patterns_allowed": "",
}

VALID_ALLOWED_ACTIONS = {"all", "local_only", "selected"}


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes")


def _parse_patterns(value):
    """Parse a comma-separated patterns string into a sorted list."""
    if isinstance(value, list):
        return sorted(p.strip() for p in value if p.strip())
    if not value or not str(value).strip():
        return []
    return sorted(p.strip() for p in str(value).split(",") if p.strip())


class ActionsPlugin(BasePlugin):
    def fetch_current_state(self) -> dict:
        perms_path = self.client.repo_path(self.owner, self.repo, "actions/permissions")
        perms = self.client.get_json(perms_path)
        workflow_path = self.client.repo_path(
            self.owner, self.repo, "actions/permissions/workflow"
        )
        workflow = self.client.get_json(workflow_path)

        state = {
            "allowed_actions": perms.get("allowed_actions", "all"),
            "sha_pinning_required": perms.get("sha_pinning_required", False),
            "default_workflow_permissions": workflow.get(
                "default_workflow_permissions", "write"
            ),
            "can_approve_pull_request_reviews": workflow.get(
                "can_approve_pull_request_reviews", True
            ),
        }

        # Fetch selected-actions details when allowed_actions is "selected"
        if state["allowed_actions"] == "selected":
            selected_path = self.client.repo_path(
                self.owner, self.repo, "actions/permissions/selected-actions"
            )
            selected = self.client.get_json(selected_path)
            state["github_owned_allowed"] = selected.get("github_owned_allowed", True)
            state["verified_allowed"] = selected.get("verified_allowed", False)
            state["patterns_allowed"] = _parse_patterns(
                selected.get("patterns_allowed", [])
            )

        return state

    def plan(self, current_state=None) -> Plan:
        plan = Plan()
        settings = self.config.actions_settings()
        baseline = current_state if current_state is not None else GITHUB_DEFAULTS
        is_audit = current_state is not None

        # --- allowed_actions ---
        desired_allowed = settings.get(
            "allowed_actions", GITHUB_DEFAULTS["allowed_actions"]
        )
        current_allowed = baseline.get(
            "allowed_actions", GITHUB_DEFAULTS["allowed_actions"]
        )

        if desired_allowed != current_allowed:
            plan.add(
                Change(
                    type=ChangeType.UPDATE,
                    category=ChangeCategory.ACTIONS,
                    key="allowed_actions",
                    old=current_allowed,
                    new=desired_allowed,
                )
            )
        elif is_audit:
            plan.add(
                Change(
                    type=ChangeType.SKIP,
                    category=ChangeCategory.ACTIONS,
                    key="allowed_actions",
                    reason="Already at desired value",
                )
            )

        # --- selected-actions sub-settings (only when desired is "selected") ---
        if desired_allowed == "selected":
            self._plan_selected_actions(plan, settings, baseline, is_audit)

        # --- sha_pinning_required ---
        desired_sha_pinning = _parse_bool(
            settings.get(
                "sha_pinning_required",
                GITHUB_DEFAULTS["sha_pinning_required"],
            )
        )
        current_sha_pinning = baseline.get(
            "sha_pinning_required",
            GITHUB_DEFAULTS["sha_pinning_required"],
        )
        if isinstance(current_sha_pinning, str):
            current_sha_pinning = _parse_bool(current_sha_pinning)

        if desired_sha_pinning != current_sha_pinning:
            plan.add(
                Change(
                    type=ChangeType.UPDATE,
                    category=ChangeCategory.ACTIONS,
                    key="sha_pinning_required",
                    old=current_sha_pinning,
                    new=desired_sha_pinning,
                )
            )
        elif is_audit:
            plan.add(
                Change(
                    type=ChangeType.SKIP,
                    category=ChangeCategory.ACTIONS,
                    key="sha_pinning_required",
                    reason="Already at desired value",
                )
            )

        # --- default_workflow_permissions ---
        desired_workflow_perms = settings.get(
            "default_workflow_permissions",
            GITHUB_DEFAULTS["default_workflow_permissions"],
        )
        current_workflow_perms = baseline.get(
            "default_workflow_permissions",
            GITHUB_DEFAULTS["default_workflow_permissions"],
        )

        if desired_workflow_perms != current_workflow_perms:
            plan.add(
                Change(
                    type=ChangeType.UPDATE,
                    category=ChangeCategory.ACTIONS,
                    key="default_workflow_permissions",
                    old=current_workflow_perms,
                    new=desired_workflow_perms,
                )
            )
        elif is_audit:
            plan.add(
                Change(
                    type=ChangeType.SKIP,
                    category=ChangeCategory.ACTIONS,
                    key="default_workflow_permissions",
                    reason="Already at desired value",
                )
            )

        # --- can_approve_pull_request_reviews ---
        desired_can_approve = _parse_bool(
            settings.get(
                "can_approve_pull_request_reviews",
                GITHUB_DEFAULTS["can_approve_pull_request_reviews"],
            )
        )
        current_can_approve = baseline.get(
            "can_approve_pull_request_reviews",
            GITHUB_DEFAULTS["can_approve_pull_request_reviews"],
        )
        if isinstance(current_can_approve, str):
            current_can_approve = _parse_bool(current_can_approve)

        if desired_can_approve != current_can_approve:
            plan.add(
                Change(
                    type=ChangeType.UPDATE,
                    category=ChangeCategory.ACTIONS,
                    key="can_approve_pull_request_reviews",
                    old=current_can_approve,
                    new=desired_can_approve,
                )
            )
        elif is_audit:
            plan.add(
                Change(
                    type=ChangeType.SKIP,
                    category=ChangeCategory.ACTIONS,
                    key="can_approve_pull_request_reviews",
                    reason="Already at desired value",
                )
            )

        return plan

    def _plan_selected_actions(self, plan, settings, baseline, is_audit):
        """Add plan entries for selected-actions sub-settings."""
        # github_owned_allowed
        desired_gh_owned = _parse_bool(
            settings.get(
                "github_owned_allowed",
                GITHUB_DEFAULTS["github_owned_allowed"],
            )
        )
        current_gh_owned = baseline.get(
            "github_owned_allowed",
            GITHUB_DEFAULTS["github_owned_allowed"],
        )
        if isinstance(current_gh_owned, str):
            current_gh_owned = _parse_bool(current_gh_owned)

        if desired_gh_owned != current_gh_owned:
            plan.add(
                Change(
                    type=ChangeType.UPDATE,
                    category=ChangeCategory.ACTIONS,
                    key="github_owned_allowed",
                    old=current_gh_owned,
                    new=desired_gh_owned,
                )
            )
        elif is_audit:
            plan.add(
                Change(
                    type=ChangeType.SKIP,
                    category=ChangeCategory.ACTIONS,
                    key="github_owned_allowed",
                    reason="Already at desired value",
                )
            )

        # verified_allowed
        desired_verified = _parse_bool(
            settings.get(
                "verified_allowed",
                GITHUB_DEFAULTS["verified_allowed"],
            )
        )
        current_verified = baseline.get(
            "verified_allowed",
            GITHUB_DEFAULTS["verified_allowed"],
        )
        if isinstance(current_verified, str):
            current_verified = _parse_bool(current_verified)

        if desired_verified != current_verified:
            plan.add(
                Change(
                    type=ChangeType.UPDATE,
                    category=ChangeCategory.ACTIONS,
                    key="verified_allowed",
                    old=current_verified,
                    new=desired_verified,
                )
            )
        elif is_audit:
            plan.add(
                Change(
                    type=ChangeType.SKIP,
                    category=ChangeCategory.ACTIONS,
                    key="verified_allowed",
                    reason="Already at desired value",
                )
            )

        # patterns_allowed
        desired_patterns = _parse_patterns(
            settings.get("patterns_allowed", GITHUB_DEFAULTS["patterns_allowed"])
        )
        current_patterns = _parse_patterns(
            baseline.get("patterns_allowed", GITHUB_DEFAULTS["patterns_allowed"])
        )

        if desired_patterns != current_patterns:
            plan.add(
                Change(
                    type=ChangeType.UPDATE,
                    category=ChangeCategory.ACTIONS,
                    key="patterns_allowed",
                    old=current_patterns,
                    new=desired_patterns,
                )
            )
        elif is_audit:
            plan.add(
                Change(
                    type=ChangeType.SKIP,
                    category=ChangeCategory.ACTIONS,
                    key="patterns_allowed",
                    reason="Already at desired value",
                )
            )

    def apply(self, plan: Plan) -> None:
        perms_body = {}
        workflow_body = {}
        selected_body = {}
        for change in plan.actionable_changes:
            if change.category != ChangeCategory.ACTIONS:
                continue
            if change.key == "allowed_actions":
                perms_body["allowed_actions"] = change.new
            elif change.key == "sha_pinning_required":
                perms_body["sha_pinning_required"] = change.new
            elif change.key == "default_workflow_permissions":
                workflow_body["default_workflow_permissions"] = change.new
            elif change.key == "can_approve_pull_request_reviews":
                workflow_body["can_approve_pull_request_reviews"] = change.new
            elif change.key == "github_owned_allowed":
                selected_body["github_owned_allowed"] = change.new
            elif change.key == "verified_allowed":
                selected_body["verified_allowed"] = change.new
            elif change.key == "patterns_allowed":
                selected_body["patterns_allowed"] = change.new

        if perms_body:
            # enabled is required in the body for this endpoint
            perms_body["enabled"] = True
            path = self.client.repo_path(self.owner, self.repo, "actions/permissions")
            self.client.call_json("PUT", path, perms_body)

        if selected_body:
            path = self.client.repo_path(
                self.owner, self.repo, "actions/permissions/selected-actions"
            )
            self.client.call_json("PUT", path, selected_body)

        if workflow_body:
            path = self.client.repo_path(
                self.owner, self.repo, "actions/permissions/workflow"
            )
            self.client.call_json("PUT", path, workflow_body)
