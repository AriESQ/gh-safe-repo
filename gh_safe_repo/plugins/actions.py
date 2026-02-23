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
    "default_workflow_permissions": "write",
    "can_approve_pull_request_reviews": True,
}


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes")


class ActionsPlugin(BasePlugin):
    def fetch_current_state(self) -> dict:
        path = self.client.repo_path(
            self.owner, self.repo, "actions/permissions/workflow"
        )
        data = self.client.get_json(path)
        return {
            "default_workflow_permissions": data.get(
                "default_workflow_permissions", "write"
            ),
            "can_approve_pull_request_reviews": data.get(
                "can_approve_pull_request_reviews", True
            ),
        }

    def plan(self, current_state=None) -> Plan:
        plan = Plan()
        settings = self.config.actions_settings()
        baseline = current_state if current_state is not None else GITHUB_DEFAULTS
        is_audit = current_state is not None

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
        # Build workflow permissions body from plan changes
        workflow_body = {}
        for change in plan.actionable_changes:
            if change.category != ChangeCategory.ACTIONS:
                continue
            if change.key == "default_workflow_permissions":
                workflow_body["default_workflow_permissions"] = change.new
            elif change.key == "can_approve_pull_request_reviews":
                workflow_body["can_approve_pull_request_reviews"] = change.new

        if workflow_body:
            path = self.client.repo_path(
                self.owner, self.repo, "actions/permissions/workflow"
            )
            self.client.call_json("PUT", path, workflow_body)
