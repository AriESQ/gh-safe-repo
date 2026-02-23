"""
Security plugin — Phase 2 stub.
Emits SKIP changes for Dependabot and secret scanning features.
"""

from ..diff import Change, ChangeCategory, ChangeType, Plan
from .base import BasePlugin


class SecurityPlugin(BasePlugin):
    def plan(self) -> Plan:
        plan = Plan()
        plan.add(
            Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.SECURITY,
                key="dependabot_alerts",
                reason="Dependabot requires a public repo or paid GitHub plan (Phase 2)",
            )
        )
        plan.add(
            Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.SECURITY,
                key="secret_scanning",
                reason="Secret scanning requires a public repo or paid GitHub plan (Phase 2)",
            )
        )
        return plan

    def apply(self, plan: Plan) -> None:
        # Nothing to apply — all changes are SKIP
        pass
