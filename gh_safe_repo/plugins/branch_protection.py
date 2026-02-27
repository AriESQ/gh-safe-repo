"""
Branch protection plugin — applies classic branch protection or Rulesets API.

Free plan: available for public repos only.
Paid plan (Pro/Team): available for private repos too.
Rulesets: opt-in via use_rulesets = true in config.
"""

import json
import sys

from ..diff import Change, ChangeCategory, ChangeType, Plan
from ..errors import APIError
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
    def __init__(self, client, owner, repo, config, is_public=False, is_paid_plan=False, branches=None):
        super().__init__(client, owner, repo, config)
        self.is_public = is_public
        self.is_paid_plan = is_paid_plan
        self.branches = branches or ["master", "main"]

    def fetch_current_state(self) -> dict:
        branch = self.branches[0]
        path = self.client.repo_path(self.owner, self.repo, f"branches/{branch}/protection")
        status, text = self.client.call_api("GET", path)
        if status == 404:
            # No protection set — return permissive defaults
            return dict(GITHUB_DEFAULTS)
        if status == 403:
            # Feature not available (private repo on free plan) — plan() will SKIP
            return dict(GITHUB_DEFAULTS)
        if status and status >= 400:
            raise APIError(f"GET {path} returned {status}", status_code=status)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            raise APIError(f"GET {path} returned non-JSON response")

        result = {}
        result["enforce_admins"] = (
            data.get("enforce_admins", {}).get("enabled", False)
            if isinstance(data.get("enforce_admins"), dict)
            else bool(data.get("enforce_admins", False))
        )
        result["allow_force_pushes"] = (
            data.get("allow_force_pushes", {}).get("enabled", True)
            if isinstance(data.get("allow_force_pushes"), dict)
            else bool(data.get("allow_force_pushes", True))
        )
        result["allow_deletions"] = (
            data.get("allow_deletions", {}).get("enabled", True)
            if isinstance(data.get("allow_deletions"), dict)
            else bool(data.get("allow_deletions", True))
        )

        rpr = data.get("required_pull_request_reviews")
        if rpr is not None:
            result["require_pull_request"] = True
            result["required_approving_reviews"] = rpr.get(
                "required_approving_review_count", 0
            )
            result["dismiss_stale_reviews"] = rpr.get("dismiss_stale_reviews", False)
        else:
            result["require_pull_request"] = False
            result["required_approving_reviews"] = 0
            result["dismiss_stale_reviews"] = False

        rcr = data.get("required_conversation_resolution")
        if isinstance(rcr, dict):
            result["require_conversation_resolution"] = rcr.get("enabled", False)
        else:
            result["require_conversation_resolution"] = bool(rcr) if rcr is not None else False

        return result

    def plan(self, current_state=None) -> Plan:
        plan = Plan()

        if not self.is_public and not self.is_paid_plan:
            plan.add(Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.BRANCH_PROTECTION,
                key="branch_protection",
                reason="Branch protection requires a public repo or paid GitHub plan",
            ))
            return plan

        desired = self._desired()
        is_audit = current_state is not None
        baseline = current_state if is_audit else GITHUB_DEFAULTS

        # Show which branches will be protected (create mode only)
        if not is_audit:
            plan.add(Change(
                type=ChangeType.ADD,
                category=ChangeCategory.BRANCH_PROTECTION,
                key="protected_branches",
                new=", ".join(self.branches),
            ))

        for key, desired_val in desired.items():
            current_val = baseline.get(key)
            if current_val is None:
                continue
            if desired_val != current_val:
                plan.add(Change(
                    type=ChangeType.UPDATE if is_audit else ChangeType.ADD,
                    category=ChangeCategory.BRANCH_PROTECTION,
                    key=key,
                    old=current_val if is_audit else None,
                    new=desired_val,
                ))
            elif is_audit:
                plan.add(Change(
                    type=ChangeType.SKIP,
                    category=ChangeCategory.BRANCH_PROTECTION,
                    key=key,
                    reason="Already at desired value",
                ))

        return plan

    def apply(self, plan: Plan) -> None:
        bp_changes = [
            c for c in plan.actionable_changes
            if c.category == ChangeCategory.BRANCH_PROTECTION
        ]
        if not bp_changes:
            return

        use_rulesets = self.config.getbool("branch_protection", "use_rulesets", fallback=False)
        desired = self._desired()

        if use_rulesets:
            body = self._build_ruleset_body(desired)
            path = self.client.repo_path(self.owner, self.repo, "rulesets")
            self.client.call_json("POST", path, body)
        else:
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
            for branch in self.branches:
                path = self.client.repo_path(self.owner, self.repo, f"branches/{branch}/protection")
                try:
                    self.client.call_json("PUT", path, body)
                except APIError as e:
                    if e.status_code in (404, 422):
                        print(
                            f"[skip] Branch '{branch}' not found — skipping protection",
                            file=sys.stderr,
                        )
                        continue
                    raise

    def _build_ruleset_body(self, desired: dict) -> dict:
        rules = []
        if not desired["allow_force_pushes"]:
            rules.append({"type": "non_fast_forward"})
        if not desired["allow_deletions"]:
            rules.append({"type": "deletion"})
        if desired["require_pull_request"]:
            rules.append({
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": desired["required_approving_reviews"],
                    "dismiss_stale_reviews_on_push": desired["dismiss_stale_reviews"],
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": desired["require_conversation_resolution"],
                },
            })
        # enforce_admins=False → allow repo Admin role (id=5) to bypass
        bypass_actors = []
        if not desired["enforce_admins"]:
            bypass_actors.append({
                "actor_id": 5,
                "actor_type": "RepositoryRole",
                "bypass_mode": "always",
            })
        return {
            "name": "gh-safe-repo defaults",
            "target": "branch",
            "enforcement": "active",
            "conditions": {
                "ref_name": {
                    "include": [f"refs/heads/{b}" for b in self.branches],
                    "exclude": [],
                }
            },
            "rules": rules,
            "bypass_actors": bypass_actors,
        }

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
