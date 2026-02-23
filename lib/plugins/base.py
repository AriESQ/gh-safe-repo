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
    def plan(self) -> Plan:
        """
        Compare desired state against GitHub defaults.
        Returns a Plan describing what changes will be made.
        No API calls that read state — for new repos the state is always GitHub defaults.
        """

    @abstractmethod
    def apply(self, plan: Plan) -> None:
        """Execute the actionable changes in the plan."""
