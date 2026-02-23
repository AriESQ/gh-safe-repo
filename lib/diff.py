"""
Diff model: Change dataclass and Plan container.
Pattern adapted from gh-repo-settings/internal/diff/domain/model/change.go.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional


class ChangeType(Enum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    SKIP = "skip"  # Feature unavailable at this plan level


class ChangeCategory(Enum):
    REPO = "repo"
    ACTIONS = "actions"
    BRANCH_PROTECTION = "branch_protection"
    SECURITY = "security"
    FILE = "file"


@dataclass
class Change:
    type: ChangeType
    category: ChangeCategory
    key: str
    old: Any = None
    new: Any = None
    reason: Optional[str] = None  # Used for SKIP to explain why

    def describe(self):
        if self.type == ChangeType.SKIP:
            return f"SKIP {self.category.value}/{self.key}: {self.reason}"
        if self.type == ChangeType.ADD:
            return f"ADD {self.category.value}/{self.key}: {self.new!r}"
        if self.type == ChangeType.UPDATE:
            return f"UPDATE {self.category.value}/{self.key}: {self.old!r} → {self.new!r}"
        if self.type == ChangeType.DELETE:
            return f"DELETE {self.category.value}/{self.key}: {self.old!r}"
        return f"{self.type.value} {self.category.value}/{self.key}"


@dataclass
class Plan:
    changes: List[Change] = field(default_factory=list)

    def add(self, change: Change):
        self.changes.append(change)

    def has_changes(self):
        return any(c.type != ChangeType.SKIP for c in self.changes)

    @property
    def actionable_changes(self):
        return [c for c in self.changes if c.type != ChangeType.SKIP]

    @property
    def skipped_changes(self):
        return [c for c in self.changes if c.type == ChangeType.SKIP]

    def count_by_type(self):
        counts = {}
        for c in self.changes:
            counts[c.type] = counts.get(c.type, 0) + 1
        return counts

    def merge(self, other: "Plan"):
        """Merge another plan's changes into this one."""
        self.changes.extend(other.changes)
