"""
Repository plugin: creation (POST) and basic settings (PATCH).
Compares desired config against known GitHub defaults for new repos.
"""

from ..diff import Change, ChangeCategory, ChangeType, Plan
from ..errors import RepoExistsError
from .base import BasePlugin

# GitHub's own defaults for a newly created repo
GITHUB_DEFAULTS = {
    "private": False,
    "has_wiki": True,
    "has_issues": True,
    "has_projects": True,
    "delete_branch_on_merge": False,
    "allow_squash_merge": True,
    "allow_merge_commit": True,
    "allow_rebase_merge": True,
}

# Fields that must go in the POST /user/repos body (create-time only)
CREATE_FIELDS = {"private", "auto_init"}

# Fields that go in PATCH /repos/{owner}/{repo} (can be set after creation too)
PATCH_FIELDS = {
    "has_wiki",
    "has_issues",
    "has_projects",
    "delete_branch_on_merge",
    "allow_squash_merge",
    "allow_merge_commit",
    "allow_rebase_merge",
}


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes")


class RepositoryPlugin(BasePlugin):
    def plan(self) -> Plan:
        plan = Plan()
        settings = self.config.repo_settings()

        # Repo creation is always an ADD
        plan.add(
            Change(
                type=ChangeType.ADD,
                category=ChangeCategory.REPO,
                key="repository",
                new=f"{self.owner}/{self.repo}",
            )
        )

        # Compare each PATCH-able setting against GitHub defaults
        for key in PATCH_FIELDS:
            if key not in settings:
                continue
            desired = _parse_bool(settings[key])
            github_default = GITHUB_DEFAULTS.get(key)
            if github_default is None:
                continue
            if desired != github_default:
                plan.add(
                    Change(
                        type=ChangeType.UPDATE,
                        category=ChangeCategory.REPO,
                        key=key,
                        old=github_default,
                        new=desired,
                    )
                )

        return plan

    def apply(self, plan: Plan) -> None:
        settings = self.config.repo_settings()

        # Build POST body
        create_body = {"name": self.repo}
        for key in CREATE_FIELDS:
            if key in settings:
                create_body[key] = _parse_bool(settings[key])

        # Create the repo
        try:
            self.client.call_json("POST", "/user/repos", create_body)
        except Exception as e:
            from ..errors import APIError
            if isinstance(e, APIError) and e.status_code == 422:
                raise RepoExistsError(self.owner, self.repo)
            raise

        # Build PATCH body from actionable changes
        patch_body = {}
        for change in plan.actionable_changes:
            if change.category == ChangeCategory.REPO and change.key in PATCH_FIELDS:
                patch_body[change.key] = change.new

        if patch_body:
            path = self.client.repo_path(self.owner, self.repo)
            self.client.call_json("PATCH", path, patch_body)
