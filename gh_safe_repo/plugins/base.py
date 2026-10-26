"""Abstract base class for all plugins."""

from abc import ABC, abstractmethod

from ..config_manager import setting_tier
from ..diff import Change, ChangeType, Plan
from ..github_client import GitHubClient


def ruleset_enforced(ruleset: dict) -> bool:
    """True only for an ``enforcement: "active"`` ruleset.

    ``disabled`` and ``evaluate`` rulesets keep their rules but block nothing,
    and a body without the field (the API always sends it) is not trusted.
    """
    return ruleset.get("enforcement") == "active"


class BasePlugin(ABC):
    # Merge metadata (fix/audit three-way merge). ``section`` is the config
    # section / ChangeCategory value this plugin's per-key rows belong to;
    # ``github_defaults`` is what a brand-new repo holds for each key.
    section: str = ""
    github_defaults: dict = {}

    def __init__(self, client: GitHubClient, owner: str, repo: str, config):
        self.client = client
        self.owner = owner
        self.repo = repo
        self.config = config

    def annotate(self, change: Change) -> Change:
        """Tag a per-key row with its tier and GitHub default.

        Plugins stay mechanical: they report what differs and attach the two
        facts the policy layer (commands/fix.py:apply_merge_policy) needs.
        Rows for keys with no GitHub default are left unannotated and pass
        through the policy untouched.
        """
        if change.key in self.github_defaults:
            change.tier = setting_tier(self.section, change.key)
            change.github_default = self.github_defaults[change.key]
        return change

    def preserved_values(self, plan: Plan) -> dict:
        """{key: current value} for this plugin's PRESERVE rows in ``plan``.

        Composite writes (a whole ruleset) must carry the human's value for a
        preserved key rather than the desired one.
        """
        return {
            c.key: c.old for c in plan.changes
            if c.type == ChangeType.PRESERVE and c.category.value == self.section
        }

    @abstractmethod
    def plan(self, current_state=None) -> Plan:
        """
        Compare desired state against a baseline and return a Plan.

        current_state=None  → compare against hardcoded GitHub defaults (create mode).
        current_state=dict  → compare against actual API-fetched values (audit mode).
        """

    @abstractmethod
    def fetch_current_state(self, strict: bool = False) -> dict:
        """Fetch this plugin's settings from the GitHub API. Used in audit mode.

        strict=False (planning): an unreadable setting may be reported as
        GitHub's default, so the plan proposes setting it.
        strict=True (post-apply verification, #77): a read that cannot tell
        "off" from "could not read" must raise APIError instead of guessing,
        so the verifier reports the setting as unknown, not as a mismatch.
        Only a definite answer (2xx, or the endpoint's documented 404 for
        "off") may be returned.
        """

    @abstractmethod
    def apply(self, plan: Plan) -> None:
        """Execute the actionable changes in the plan."""
