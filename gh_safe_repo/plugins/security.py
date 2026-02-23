"""
Security plugin — enables Dependabot alerts and secret scanning.

Dependabot alerts: available for public repos on free plan; private repos need paid plan.
Secret scanning: automatically enabled by GitHub for all public repos (no API call needed);
    for private repos on paid plans, enabled via PATCH security_and_analysis.
"""

from ..diff import Change, ChangeCategory, ChangeType, Plan
from .base import BasePlugin


class SecurityPlugin(BasePlugin):
    def __init__(self, client, owner, repo, config, is_public=False, is_paid_plan=False):
        super().__init__(client, owner, repo, config)
        self.is_public = is_public
        self.is_paid_plan = is_paid_plan

    def plan(self) -> Plan:
        plan = Plan()

        if not self.is_public and not self.is_paid_plan:
            plan.add(Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.SECURITY,
                key="dependabot_alerts",
                reason="Dependabot requires a public repo or paid GitHub plan",
            ))
            plan.add(Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.SECURITY,
                key="secret_scanning",
                reason="Secret scanning requires a public repo or paid GitHub plan",
            ))
            return plan

        # Dependabot alerts can be enabled via API (public or private paid)
        if self.config.getbool("security", "enable_dependabot_alerts", fallback=True):
            plan.add(Change(
                type=ChangeType.ADD,
                category=ChangeCategory.SECURITY,
                key="dependabot_alerts",
                new=True,
            ))

        if self.is_public:
            # Secret scanning is automatic for public repos — no API call needed
            plan.add(Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.SECURITY,
                key="secret_scanning",
                reason="Automatically enabled for public repositories by GitHub",
            ))
        else:
            # Private repo on paid plan — enable via PATCH security_and_analysis
            plan.add(Change(
                type=ChangeType.ADD,
                category=ChangeCategory.SECURITY,
                key="secret_scanning",
                new=True,
            ))

        return plan

    def apply(self, plan: Plan) -> None:
        for change in plan.actionable_changes:
            if change.category != ChangeCategory.SECURITY:
                continue
            if change.key == "dependabot_alerts":
                path = self.client.repo_path(self.owner, self.repo, "vulnerability-alerts")
                self.client.call_json("PUT", path)
            elif change.key == "secret_scanning":
                path = self.client.repo_path(self.owner, self.repo)
                self.client.call_json("PATCH", path, {
                    "security_and_analysis": {"secret_scanning": {"status": "enabled"}}
                })
