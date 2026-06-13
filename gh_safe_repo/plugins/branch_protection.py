"""
Branch protection plugin — applies classic branch protection or Rulesets API.

Free plan: available for public repos only.
Paid plan (Pro/Team): available for private repos too.

Rulesets are the default (use_rulesets = true). A single ruleset covers all
configured branches via conditions.ref_name.include, supports bypass actors,
and is GitHub's forward direction (new rule types are Rulesets-only). The
classic per-branch protection path is kept for one release cycle via
use_rulesets = false.

Migrating a repo that already has classic protection requires the explicit
--migrate-branch-protection flag: classic rules like required_status_checks and
push restrictions have no ruleset equivalent here and would be dropped, so the
migration is never silent.
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

RULESET_NAME = "gh-safe-repo defaults"


class BranchProtectionPlugin(BasePlugin):
    def __init__(self, client, owner, repo, config, is_public=False, is_paid_plan=False,
                 branches=None, migrate=False):
        super().__init__(client, owner, repo, config)
        self.is_public = is_public
        self.is_paid_plan = is_paid_plan
        self.branches = branches or ["master", "main"]
        self.migrate = migrate
        # Set during fetch_current_state() in rulesets mode; gates migration.
        self._classic_present = False

    def _use_rulesets(self) -> bool:
        return self.config.getbool("branch_protection", "use_rulesets", fallback=True)

    # ── State reads ───────────────────────────────────────────────────────────

    def fetch_current_state(self) -> dict:
        if self._use_rulesets():
            # Record any pre-existing classic protection so plan() can gate the
            # migration behind --migrate-branch-protection.
            self._classic_present = self._classic_protection_present()
            return self._fetch_ruleset_state()
        return self._fetch_classic_state()

    def _classic_protection_present(self) -> bool:
        """True if any configured branch has classic protection configured.

        The classic endpoint 404s when no protection is set, so a 200 means
        classic rules exist. 403 (free+private) is treated as absent — plan()
        SKIPs on plan-availability grounds in that case anyway.
        """
        for branch in self.branches:
            path = self.client.repo_path(
                self.owner, self.repo, f"branches/{branch}/protection"
            )
            status, _ = self.client.call_api("GET", path)
            if status == 200:
                return True
        return False

    def _fetch_classic_state(self) -> dict:
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

    def _fetch_ruleset_state(self) -> dict:
        """Read the gh-safe-repo branch ruleset and map it to the canonical dict.

        Inverse of _build_ruleset_body(). Falls back to permissive GITHUB_DEFAULTS
        when no matching ruleset exists, or when rulesets are inaccessible
        (404/403 on free+private).
        """
        detail = self._fetch_ruleset_detail()
        if detail is None:
            return dict(GITHUB_DEFAULTS)

        rules = detail.get("rules", [])
        rule_by_type = {r.get("type"): r for r in rules}

        result = {
            "allow_force_pushes": "non_fast_forward" not in rule_by_type,
            "allow_deletions": "deletion" not in rule_by_type,
        }

        pr = rule_by_type.get("pull_request")
        if pr is not None:
            params = pr.get("parameters", {})
            result["require_pull_request"] = True
            result["required_approving_reviews"] = params.get(
                "required_approving_review_count", 0
            )
            result["dismiss_stale_reviews"] = params.get(
                "dismiss_stale_reviews_on_push", False
            )
            result["require_conversation_resolution"] = params.get(
                "required_review_thread_resolution", False
            )
        else:
            result["require_pull_request"] = False
            result["required_approving_reviews"] = 0
            result["dismiss_stale_reviews"] = False
            result["require_conversation_resolution"] = False

        # enforce_admins=False is expressed as a RepositoryRole(Admin, id=5) bypass.
        bypass = detail.get("bypass_actors", [])
        admin_bypass = any(
            a.get("actor_type") == "RepositoryRole" and a.get("actor_id") == 5
            for a in bypass
        )
        result["enforce_admins"] = not admin_bypass

        return result

    def _fetch_ruleset_detail(self) -> dict | None:
        """Return the full detail of our branch ruleset, or None if absent."""
        ruleset_id = self._find_ruleset_id()
        if ruleset_id is None:
            return None
        detail_path = self.client.repo_path(self.owner, self.repo, f"rulesets/{ruleset_id}")
        status, text = self.client.call_api("GET", detail_path)
        if status and status >= 400:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None

    def _find_ruleset_id(self):
        """Return the id of our branch ruleset, or None if it doesn't exist."""
        path = self.client.repo_path(self.owner, self.repo, "rulesets")
        status, text = self.client.call_api("GET", path)
        if status == 404 or status == 403:
            return None
        if status and status >= 400:
            raise APIError(f"GET {path} returned {status}", status_code=status)
        try:
            rulesets = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            raise APIError(f"GET {path} returned non-JSON response")
        for rs in rulesets:
            if rs.get("target") == "branch" and rs.get("name") == RULESET_NAME:
                return rs.get("id")
        return None

    # ── Plan ────────────────────────────────────────────────────────────────

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

        # Migration gate: never silently convert classic protection to a ruleset.
        if self._use_rulesets() and self._classic_present and not self.migrate:
            plan.add(Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.BRANCH_PROTECTION,
                key="branch_protection",
                reason=(
                    "Existing classic branch protection detected. Re-run with "
                    "--migrate-branch-protection to migrate it to a ruleset "
                    "(classic-only rules such as required_status_checks and push "
                    "restrictions have no ruleset equivalent and will be dropped)."
                ),
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

    # ── Apply ─────────────────────────────────────────────────────────────────

    def apply(self, plan: Plan) -> None:
        bp_changes = [
            c for c in plan.actionable_changes
            if c.category == ChangeCategory.BRANCH_PROTECTION
        ]
        if not bp_changes:
            return

        desired = self._desired()

        if self._use_rulesets():
            self._apply_ruleset(desired)
        else:
            self._apply_classic(desired)

    def _apply_ruleset(self, desired: dict) -> None:
        body = self._build_ruleset_body(desired)
        existing_id = self._find_ruleset_id()
        if existing_id is not None:
            path = self.client.repo_path(self.owner, self.repo, f"rulesets/{existing_id}")
            self.client.call_json("PATCH", path, body)
        else:
            path = self.client.repo_path(self.owner, self.repo, "rulesets")
            self.client.call_json("POST", path, body)

        # Migration: remove the classic protection now that the ruleset is in place.
        # Gated by plan() — we only reach here with migrate=True when classic exists.
        if self.migrate and self._classic_present:
            for branch in self.branches:
                del_path = self.client.repo_path(
                    self.owner, self.repo, f"branches/{branch}/protection"
                )
                try:
                    self.client.call_json("DELETE", del_path)
                except APIError as e:
                    if e.status_code in (404, 422):
                        continue
                    raise

    def _apply_classic(self, desired: dict) -> None:
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
                    "require_code_owner_review": False,
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
            "name": RULESET_NAME,
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
