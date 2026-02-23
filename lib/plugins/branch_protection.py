"""
Branch protection plugin — Phase 2 stub.
Emits a SKIP change explaining this is deferred.
"""

from ..diff import Change, ChangeCategory, ChangeType, Plan
from .base import BasePlugin


class BranchProtectionPlugin(BasePlugin):
    def plan(self) -> Plan:
        plan = Plan()
        plan.add(
            Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.BRANCH_PROTECTION,
                key="branch_protection",
                reason="Branch protection requires Phase 2 (public repos + paid plan)",
            )
        )
        return plan

    def apply(self, plan: Plan) -> None:
        # Nothing to apply — all changes are SKIP
        pass
