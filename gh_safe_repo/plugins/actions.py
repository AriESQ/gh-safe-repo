"""
Actions plugin: GitHub Actions permissions and workflow permissions.
Two API calls: PUT actions/permissions + PUT actions/permissions/workflow.
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
}


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes")


class ActionsPlugin(BasePlugin):
    def fetch_current_state(self) -> dict:
        perms_path = self.client.repo_path(self.owner, self.repo, "actions/permissions")
        perms = self.client.get_json(perms_path)
        workflow_path = self.client.repo_path(
            self.owner, self.repo, "actions/permissions/workflow"
        )
        workflow = self.client.get_json(workflow_path)
        return {
            "sha_pinning_required": perms.get("sha_pinning_required", False),
            "default_workflow_permissions": workflow.get(
                "default_workflow_permissions", "write"
            ),
            "can_approve_pull_request_reviews": workflow.get(
                "can_approve_pull_request_reviews", True
            ),
        }

    def plan(self, current_state=None) -> Plan:
        plan = Plan()
        settings = self.config.actions_settings()
        baseline = current_state if current_state is not None else GITHUB_DEFAULTS
        is_audit = current_state is not None

        desired_sha_pinning = _parse_bool(
            settings.get(
                "sha_pinning_required",
                GITHUB_DEFAULTS["sha_pinning_required"],
            )
        )
        desired_workflow_perms = settings.get(
            "default_workflow_permissions",
            GITHUB_DEFAULTS["default_workflow_permissions"],
        )
        desired_can_approve = _parse_bool(
            settings.get(
                "can_approve_pull_request_reviews",
                GITHUB_DEFAULTS["can_approve_pull_request_reviews"],
            )
        )

        current_sha_pinning = baseline.get(
            "sha_pinning_required",
            GITHUB_DEFAULTS["sha_pinning_required"],
        )
        if isinstance(current_sha_pinning, str):
            current_sha_pinning = _parse_bool(current_sha_pinning)
        current_workflow_perms = baseline.get(
            "default_workflow_permissions",
            GITHUB_DEFAULTS["default_workflow_permissions"],
        )
        current_can_approve = baseline.get(
            "can_approve_pull_request_reviews",
            GITHUB_DEFAULTS["can_approve_pull_request_reviews"],
        )
        if isinstance(current_can_approve, str):
            current_can_approve = _parse_bool(current_can_approve)

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

    def apply(self, plan: Plan) -> None:
        perms_body = {}
        workflow_body = {}
        for change in plan.actionable_changes:
            if change.category != ChangeCategory.ACTIONS:
                continue
            if change.key == "sha_pinning_required":
                perms_body["sha_pinning_required"] = change.new
            elif change.key == "default_workflow_permissions":
                workflow_body["default_workflow_permissions"] = change.new
            elif change.key == "can_approve_pull_request_reviews":
                workflow_body["can_approve_pull_request_reviews"] = change.new

        if perms_body:
            # sha_pinning_required requires enabled to be present in the body
            perms_body["enabled"] = True
            path = self.client.repo_path(self.owner, self.repo, "actions/permissions")
            self.client.call_json("PUT", path, perms_body)

        if workflow_body:
            path = self.client.repo_path(
                self.owner, self.repo, "actions/permissions/workflow"
            )
            self.client.call_json("PUT", path, workflow_body)
