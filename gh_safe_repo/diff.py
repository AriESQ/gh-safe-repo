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
    # fix/audit three-way merge: the setting differs from what we want, but a
    # human changed it from GitHub's default and the policy keeps their value.
    # Never actionable, never applied, never verified.
    PRESERVE = "preserve"


class ChangeCategory(Enum):
    REPO = "repo"
    ACTIONS = "actions"
    BRANCH_PROTECTION = "branch_protection"
    SECURITY = "security"
    TAG_PROTECTION = "tag_protection"
    FILE = "file"
    SCAN = "scan"


@dataclass
class Change:
    type: ChangeType
    category: ChangeCategory
    key: str
    old: Any = None
    new: Any = None
    reason: Optional[str] = None  # Used for SKIP/PRESERVE to explain why
    # Merge context, set by plugins in audit mode (fix/audit) and read by
    # commands/fix.py:apply_merge_policy(). ``tier`` is "security" or
    # "preference" (config_manager.SETTING_TIERS); ``github_default`` is what
    # a fresh repo would hold for this key (the plugin's GITHUB_DEFAULTS).
    # None on both means "not a per-key setting", and the policy passes the
    # row through untouched.
    tier: Optional[str] = None
    github_default: Any = None
    # UPDATE only: the current value is a *weaker* security posture than
    # GitHub's default — someone relaxed a control. Rendered as ⚠ weakened.
    weakened: bool = False

    def describe(self):
        if self.type == ChangeType.SKIP:
            return f"SKIP {self.category.value}/{self.key}: {self.reason}"
        if self.type == ChangeType.PRESERVE:
            return f"PRESERVE {self.category.value}/{self.key}: {self.old!r} ({self.reason})"
        if self.type == ChangeType.ADD:
            return f"ADD {self.category.value}/{self.key}: {self.new!r}"
        if self.type == ChangeType.UPDATE:
            return f"UPDATE {self.category.value}/{self.key}: {self.old!r} → {self.new!r}"
        if self.type == ChangeType.DELETE:
            return f"DELETE {self.category.value}/{self.key}: {self.old!r}"
        return f"{self.type.value} {self.category.value}/{self.key}"


@dataclass
class PlanWarning:
    """Advisory note attached to a plan; never changes what is applied.

    ``kind`` is a stable identifier (``allowlist_pattern_removed``,
    ``actions_blocked``, ``actions_unknown``, ``actions_scan_skipped``);
    ``message`` is the human line; ``details`` holds the structured fields
    emitted alongside them in ``--json``.
    """
    kind: str
    message: str
    details: dict = field(default_factory=dict)

    def to_dict(self):
        return {"kind": self.kind, "message": self.message, **self.details}


# Rows that never produce an API call.
_INERT = frozenset({ChangeType.SKIP, ChangeType.PRESERVE})


@dataclass
class Plan:
    changes: List[Change] = field(default_factory=list)
    # Advisory warnings (#111): shown before the confirmation prompt and in
    # --json, never applied.
    warnings: List[PlanWarning] = field(default_factory=list)

    def add(self, change: Change):
        self.changes.append(change)

    def has_changes(self):
        return bool(self.actionable_changes)

    @property
    def actionable_changes(self):
        """ADD / UPDATE / DELETE rows: what apply() writes and verify() reads back."""
        return [c for c in self.changes if c.type not in _INERT]

    @property
    def skipped_changes(self):
        return [c for c in self.changes if c.type == ChangeType.SKIP]

    @property
    def preserved_changes(self):
        return [c for c in self.changes if c.type == ChangeType.PRESERVE]

    def count_by_type(self):
        counts = {}
        for c in self.changes:
            counts[c.type] = counts.get(c.type, 0) + 1
        return counts

    def merge(self, other: "Plan"):
        """Merge another plan's changes into this one."""
        self.changes.extend(other.changes)
        self.warnings.extend(other.warnings)
