"""
Branch protection plugin — applies classic branch protection to public repos.

Free plan: available for public repos only.
Paid plan (Pro/Team): available for private repos too (Phase 4 enhancement).
"""

from ..diff import Change, ChangeCategory, ChangeType, Plan
from .base import BasePlugin

# GitHub's defaults for a newly created repo — no branch protection at all
GITHUB_DEFAULTS = {
    "require_pull_request": False,
    "required_approving_reviews": 0,
    "dismiss_stale_reviews": False,
    "require_conversation_resolution": False,
    "enforce_admins": False,
    "allow_force_pushes": True,
    "allow_deletions": True,
}


class BranchProtectionPlugin(BasePlugin):
    def __init__(self, client, owner, repo, config, is_public=False):
        super().__init__(client, owner, repo, config)
        self.is_public = is_public

    def plan(self) -> Plan:
        plan = Plan()

        if not self.is_public:
            plan.add(Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.BRANCH_PROTECTION,
                key="branch_protection",
                reason="Branch protection requires a public repo or paid GitHub plan",
            ))
            return plan

        desired = self._desired()
        for key, desired_val in desired.items():
            github_default = GITHUB_DEFAULTS.get(key)
            if desired_val != github_default:
                plan.add(Change(
                    type=ChangeType.ADD,
                    category=ChangeCategory.BRANCH_PROTECTION,
                    key=key,
                    new=desired_val,
                ))

        return plan

    def apply(self, plan: Plan) -> None:
        bp_changes = [
            c for c in plan.actionable_changes
            if c.category == ChangeCategory.BRANCH_PROTECTION
        ]
        if not bp_changes:
            return

        branch = self.config.get("branch_protection", "protected_branch", fallback="main")
        desired = self._desired()

        body = {
            "required_status_checks": None,
            "enforce_admins": desired["enforce_admins"],
            "required_pull_request_reviews": {
                "dismiss_stale_reviews": desired["dismiss_stale_reviews"],
                "require_code_owner_reviews": False,
                "required_approving_review_count": desired["required_approving_reviews"],
            },
            "restrictions": None,
            "allow_force_pushes": desired["allow_force_pushes"],
            "allow_deletions": desired["allow_deletions"],
            "required_conversation_resolution": desired["require_conversation_resolution"],
        }

        path = self.client.repo_path(self.owner, self.repo, f"branches/{branch}/protection")
        self.client.call_json("PUT", path, body)

    def _desired(self) -> dict:
        c = self.config
        return {
            "require_pull_request": c.getbool("branch_protection", "require_pull_request", fallback=True),
            "required_approving_reviews": int(
                c.get("branch_protection", "required_approving_reviews", fallback="1")
            ),
            "dismiss_stale_reviews": c.getbool("branch_protection", "dismiss_stale_reviews", fallback=True),
            "require_conversation_resolution": c.getbool(
                "branch_protection", "require_conversation_resolution", fallback=True
            ),
            "enforce_admins": c.getbool("branch_protection", "enforce_admins", fallback=False),
            "allow_force_pushes": c.getbool("branch_protection", "allow_force_pushes", fallback=False),
            "allow_deletions": c.getbool("branch_protection", "allow_deletions", fallback=False),
        }
