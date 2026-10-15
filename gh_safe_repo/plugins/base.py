"""Abstract base class for all plugins."""

from abc import ABC, abstractmethod

from ..diff import Plan
from ..github_client import GitHubClient


class BasePlugin(ABC):
    def __init__(self, client: GitHubClient, owner: str, repo: str, config):
        self.client = client
        self.owner = owner
        self.repo = repo
        self.config = config

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
