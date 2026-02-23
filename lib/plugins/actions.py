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
    def plan(self) -> Plan:
        plan = Plan()
        settings = self.config.actions_settings()

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

        if desired_workflow_perms != GITHUB_DEFAULTS["default_workflow_permissions"]:
            plan.add(
                Change(
                    type=ChangeType.UPDATE,
                    category=ChangeCategory.ACTIONS,
                    key="default_workflow_permissions",
                    old=GITHUB_DEFAULTS["default_workflow_permissions"],
                    new=desired_workflow_perms,
                )
            )

        if desired_can_approve != GITHUB_DEFAULTS["can_approve_pull_request_reviews"]:
            plan.add(
                Change(
                    type=ChangeType.UPDATE,
                    category=ChangeCategory.ACTIONS,
                    key="can_approve_pull_request_reviews",
                    old=GITHUB_DEFAULTS["can_approve_pull_request_reviews"],
                    new=desired_can_approve,
                )
            )

        return plan

    def apply(self, plan: Plan) -> None:
        settings = self.config.actions_settings()

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
