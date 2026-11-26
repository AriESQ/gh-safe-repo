"""
Releases plugin — turns on immutable releases (#127).

With immutable releases on, a *published* release's assets cannot be
changed, its tag cannot be moved or deleted while the release exists, and
the tag name can never be reused (not even after the release, or the whole
repository, is deleted). Each published release also gets a release
attestation. Title and notes stay editable. Drafts are not protected: the
protection starts when a release is published, so releases should be made
draft → attach assets → publish (`gh release create` with asset files does
exactly that).

Complements the tag ruleset (plugins/tag_protection.py): the ruleset covers
every tag but lets admins bypass it and locks no assets; immutability covers
only released tags but has no bypass and locks assets too.

API (REST description 2022-11-28, operations repos/check-, enable- and
disable-immutable-releases):
    GET    /repos/{owner}/{repo}/immutable-releases
           200 {"enabled": bool, "enforced_by_owner": bool} when on,
           404 when off (the documented "disabled" answer, not an error).
    PUT    /repos/{owner}/{repo}/immutable-releases   204 (409 on conflict)
    DELETE /repos/{owner}/{repo}/immutable-releases   204 (never called here)

``enforced_by_owner: true`` means an organization policy forces the setting
on; that is compliant and the plugin never writes it. The docs list the
feature for every plan (data/features/immutable-releases.yml: fpt '*'), so
nothing is SKIPped by plan; a write a plan refuses fails the apply loudly and
verification reports it.
"""

import json

from ..diff import Change, ChangeCategory, ChangeType, Plan
from ..errors import APIError
from .base import BasePlugin

# A new repository has immutable releases off.
GITHUB_DEFAULTS = {
    "immutable": False,
}

ENDPOINT = "immutable-releases"


class ReleasesPlugin(BasePlugin):
    section = "releases"
    github_defaults = GITHUB_DEFAULTS

    def fetch_current_state(self, strict: bool = False) -> dict:
        """``{"immutable": bool, "enforced_by_owner": bool}``.

        2xx: read ``enabled`` from the body (a 2xx with no parseable body
        counts as on: only "on" answers 2xx). 404: off. Anything else (403,
        5xx, no status) is ambiguous: off when planning, APIError when
        verifying (strict), per the plugin contract.
        """
        path = self.client.repo_path(self.owner, self.repo, ENDPOINT)
        status, text = self.client.call_api("GET", path)
        if status is not None and 200 <= status < 300:
            try:
                body = json.loads(text) if text and text.strip() else None
            except (json.JSONDecodeError, ValueError, TypeError):
                body = None
            if not isinstance(body, dict):
                body = {}
            enabled = body.get("enabled")
            return {
                "immutable": enabled if isinstance(enabled, bool) else True,
                "enforced_by_owner": body.get("enforced_by_owner") is True,
            }
        if status == 404 or not strict:
            return {"immutable": False, "enforced_by_owner": False}
        raise APIError(f"GET {path} returned {status}", status_code=status)

    def plan(self, current_state=None) -> Plan:
        plan = Plan()
        if not self.config.getbool("releases", "immutable", fallback=True):
            return plan  # turned off in config: leave the repo as it is

        if current_state is None:
            plan.add(Change(
                type=ChangeType.ADD,
                category=ChangeCategory.RELEASES,
                key="immutable",
                new=True,
            ))
            return plan

        if current_state.get("enforced_by_owner"):
            plan.add(Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.RELEASES,
                key="immutable",
                reason="Enforced by the repository owner's organization policy",
            ))
        elif current_state.get("immutable"):
            plan.add(self.annotate(Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.RELEASES,
                key="immutable",
                reason="Already at desired value",
            )))
        else:
            plan.add(self.annotate(Change(
                type=ChangeType.UPDATE,
                category=ChangeCategory.RELEASES,
                key="immutable",
                old=False,
                new=True,
            )))
        return plan

    def apply(self, plan: Plan) -> None:
        for change in plan.actionable_changes:
            if change.category == ChangeCategory.RELEASES and change.key == "immutable":
                path = self.client.repo_path(self.owner, self.repo, ENDPOINT)
                self.client.call_json("PUT", path)
