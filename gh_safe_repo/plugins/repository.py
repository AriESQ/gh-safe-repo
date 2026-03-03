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
    "description",
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
    def __init__(self, client, owner, repo, config, auto_init: bool = None,
                 source_description="", source_topics=None):
        super().__init__(client, owner, repo, config)
        self._auto_init_override = auto_init
        self._source_description = source_description or ""
        self._source_topics = source_topics or []

    def fetch_current_state(self) -> dict:
        data = self.client.get_repo_data(self.owner, self.repo)
        return {
            "private": data.get("private", False),
            "has_wiki": data.get("has_wiki", True),
            "has_issues": data.get("has_issues", True),
            "has_projects": data.get("has_projects", True),
            "delete_branch_on_merge": data.get("delete_branch_on_merge", False),
            "allow_squash_merge": data.get("allow_squash_merge", True),
            "allow_merge_commit": data.get("allow_merge_commit", True),
            "allow_rebase_merge": data.get("allow_rebase_merge", True),
        }

    def plan(self, current_state=None) -> Plan:
        plan = Plan()
        settings = self.config.repo_settings()
        baseline = current_state if current_state is not None else GITHUB_DEFAULTS
        is_audit = current_state is not None

        if not is_audit:
            plan.add(
                Change(
                    type=ChangeType.ADD,
                    category=ChangeCategory.REPO,
                    key="repository",
                    new=f"{self.owner}/{self.repo}",
                )
            )

        for key in PATCH_FIELDS:
            if key not in settings:
                continue
            desired = _parse_bool(settings[key])
            current = baseline.get(key)
            if current is None:
                continue
            if desired != current:
                plan.add(
                    Change(
                        type=ChangeType.UPDATE,
                        category=ChangeCategory.REPO,
                        key=key,
                        old=current,
                        new=desired,
                    )
                )
            elif is_audit:
                plan.add(
                    Change(
                        type=ChangeType.SKIP,
                        category=ChangeCategory.REPO,
                        key=key,
                        reason="Already at desired value",
                    )
                )

        if not is_audit and self._source_description:
            plan.add(
                Change(
                    type=ChangeType.ADD,
                    category=ChangeCategory.REPO,
                    key="description",
                    new=self._source_description,
                )
            )

        if not is_audit and self._source_topics:
            plan.add(
                Change(
                    type=ChangeType.ADD,
                    category=ChangeCategory.REPO,
                    key="topics",
                    new=", ".join(self._source_topics),
                )
            )

        return plan

    def apply(self, plan: Plan) -> None:
        settings = self.config.repo_settings()

        # Only POST to create the repo if the plan has a CREATE entry for it
        has_create = any(
            c.type == ChangeType.ADD and c.key == "repository"
            for c in plan.actionable_changes
        )

        self.created_default_branch = None

        if has_create:
            create_body = {"name": self.repo}
            for key in CREATE_FIELDS:
                if key in settings:
                    create_body[key] = _parse_bool(settings[key])
            if self._auto_init_override is not None:
                create_body["auto_init"] = self._auto_init_override

            try:
                response = self.client.call_json("POST", "/user/repos", create_body)
                if isinstance(response, dict):
                    self.created_default_branch = response.get("default_branch")
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

        topics_change = next(
            (c for c in plan.actionable_changes
             if c.category == ChangeCategory.REPO and c.key == "topics"),
            None,
        )
        if topics_change:
            path = self.client.repo_path(self.owner, self.repo)
            self.client.call_json("PUT", f"{path}/topics", {"names": self._source_topics})
