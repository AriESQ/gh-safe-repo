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

    def fetch_current_state(self) -> dict:
        result = {}

        # Check Dependabot alerts: 204 = enabled, 404 = disabled
        path = self.client.repo_path(self.owner, self.repo, "vulnerability-alerts")
        status, _ = self.client.call_api("GET", path)
        result["dependabot_alerts"] = (status == 204)

        # Check secret scanning
        if self.is_public:
            # Always enabled by GitHub for public repos
            result["secret_scanning"] = True
        else:
            try:
                data = self.client.get_json(self.client.repo_path(self.owner, self.repo))
                sa = data.get("security_and_analysis") or {}
                ss = sa.get("secret_scanning") or {}
                result["secret_scanning"] = ss.get("status") == "enabled"
            except Exception:
                result["secret_scanning"] = False

        return result

    def plan(self, current_state=None) -> Plan:
        plan = Plan()
        is_audit = current_state is not None

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

        # Dependabot alerts
        if self.config.getbool("security", "enable_dependabot_alerts", fallback=True):
            if is_audit:
                if current_state.get("dependabot_alerts", False):
                    plan.add(Change(
                        type=ChangeType.SKIP,
                        category=ChangeCategory.SECURITY,
                        key="dependabot_alerts",
                        reason="Already at desired value",
                    ))
                else:
                    plan.add(Change(
                        type=ChangeType.UPDATE,
                        category=ChangeCategory.SECURITY,
                        key="dependabot_alerts",
                        old=False,
                        new=True,
                    ))
            else:
                plan.add(Change(
                    type=ChangeType.ADD,
                    category=ChangeCategory.SECURITY,
                    key="dependabot_alerts",
                    new=True,
                ))

        # Secret scanning
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
            if is_audit:
                if current_state.get("secret_scanning", False):
                    plan.add(Change(
                        type=ChangeType.SKIP,
                        category=ChangeCategory.SECURITY,
                        key="secret_scanning",
                        reason="Already at desired value",
                    ))
                else:
                    plan.add(Change(
                        type=ChangeType.UPDATE,
                        category=ChangeCategory.SECURITY,
                        key="secret_scanning",
                        old=False,
                        new=True,
                    ))
            else:
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
