"""
Tag protection plugin — creates a Ruleset to make tags immutable.

Uses the Rulesets API (POST /repos/{owner}/{repo}/rulesets) with target=tag.
Same plan-level restrictions as branch protection: free+private repos cannot use rulesets.
"""

import json
import sys

from ..diff import Change, ChangeCategory, ChangeType, Plan
from ..errors import APIError
from .base import BasePlugin

# GitHub defaults: no tag protection at all
GITHUB_DEFAULTS = {
    "prevent_tag_deletion": False,
    "prevent_tag_update": False,
}

RULESET_NAME = "gh-safe-repo tag defaults"


class TagProtectionPlugin(BasePlugin):
    def __init__(self, client, owner, repo, config, is_public=False, is_paid_plan=False):
        super().__init__(client, owner, repo, config)
        self.is_public = is_public
        self.is_paid_plan = is_paid_plan

    def fetch_current_state(self) -> dict:
        path = self.client.repo_path(self.owner, self.repo, "rulesets")
        status, text = self.client.call_api("GET", path)
        if status == 404 or status == 403:
            return dict(GITHUB_DEFAULTS)
        if status and status >= 400:
            raise APIError(f"GET {path} returned {status}", status_code=status)
        try:
            rulesets = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            raise APIError(f"GET {path} returned non-JSON response")

        # Find our tag ruleset by name
        tag_ruleset = None
        for rs in rulesets:
            if rs.get("target") == "tag" and rs.get("name") == RULESET_NAME:
                tag_ruleset = rs
                break

        if tag_ruleset is None:
            return dict(GITHUB_DEFAULTS)

        # Fetch full ruleset details (list endpoint doesn't include rules)
        ruleset_id = tag_ruleset["id"]
        detail_path = self.client.repo_path(self.owner, self.repo, f"rulesets/{ruleset_id}")
        detail_status, detail_text = self.client.call_api("GET", detail_path)
        if detail_status and detail_status >= 400:
            return dict(GITHUB_DEFAULTS)
        try:
            detail = json.loads(detail_text)
        except (json.JSONDecodeError, ValueError):
            return dict(GITHUB_DEFAULTS)

        rules = detail.get("rules", [])
        rule_types = {r.get("type") for r in rules}

        return {
            "prevent_tag_deletion": "deletion" in rule_types,
            "prevent_tag_update": "update" in rule_types,
        }

    def plan(self, current_state=None) -> Plan:
        plan = Plan()

        if not self.is_public and not self.is_paid_plan:
            plan.add(Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.TAG_PROTECTION,
                key="tag_protection",
                reason="Tag protection rulesets require a public repo or paid GitHub plan",
            ))
            return plan

        desired = self._desired()
        is_audit = current_state is not None
        baseline = current_state if is_audit else GITHUB_DEFAULTS

        if not is_audit:
            patterns = self.config.get("tag_protection", "protected_tags", fallback="v*")
            plan.add(Change(
                type=ChangeType.ADD,
                category=ChangeCategory.TAG_PROTECTION,
                key="protected_tags",
                new=patterns,
            ))

        for key, desired_val in desired.items():
            current_val = baseline.get(key)
            if current_val is None:
                continue
            if desired_val != current_val:
                plan.add(Change(
                    type=ChangeType.UPDATE if is_audit else ChangeType.ADD,
                    category=ChangeCategory.TAG_PROTECTION,
                    key=key,
                    old=current_val if is_audit else None,
                    new=desired_val,
                ))
            elif is_audit:
                plan.add(Change(
                    type=ChangeType.SKIP,
                    category=ChangeCategory.TAG_PROTECTION,
                    key=key,
                    reason="Already at desired value",
                ))

        return plan

    def apply(self, plan: Plan) -> None:
        tag_changes = [
            c for c in plan.actionable_changes
            if c.category == ChangeCategory.TAG_PROTECTION
        ]
        if not tag_changes:
            return

        desired = self._desired()
        body = self._build_tag_ruleset_body(desired)
        path = self.client.repo_path(self.owner, self.repo, "rulesets")
        self.client.call_json("POST", path, body)

    def _build_tag_ruleset_body(self, desired: dict) -> dict:
        rules = []
        if desired["prevent_tag_deletion"]:
            rules.append({"type": "deletion"})
        if desired["prevent_tag_update"]:
            rules.append({"type": "update"})

        patterns = self.config.get("tag_protection", "protected_tags", fallback="v*")
        tag_patterns = [p.strip() for p in patterns.split(",") if p.strip()]

        # Allow repo admins to bypass (consistent with branch protection default)
        bypass_actors = [{
            "actor_id": 5,
            "actor_type": "RepositoryRole",
            "bypass_mode": "always",
        }]

        return {
            "name": RULESET_NAME,
            "target": "tag",
            "enforcement": "active",
            "conditions": {
                "ref_name": {
                    "include": [f"refs/tags/{p}" for p in tag_patterns],
                    "exclude": [],
                }
            },
            "rules": rules,
            "bypass_actors": bypass_actors,
        }

    def _desired(self) -> dict:
        c = self.config
        return {
            "prevent_tag_deletion": c.getbool("tag_protection", "prevent_tag_deletion", fallback=True),
            "prevent_tag_update": c.getbool("tag_protection", "prevent_tag_update", fallback=True),
        }
