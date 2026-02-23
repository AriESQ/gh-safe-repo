"""Tests for diff.py — Change and Plan."""

import pytest
from lib.diff import Change, ChangeCategory, ChangeType, Plan


def make_change(type=ChangeType.ADD, category=ChangeCategory.REPO, key="test"):
    return Change(type=type, category=category, key=key, new="value")


class TestChange:
    def test_describe_add(self):
        c = Change(type=ChangeType.ADD, category=ChangeCategory.REPO, key="repository", new="owner/repo")
        assert "ADD" in c.describe()
        assert "repository" in c.describe()

    def test_describe_update(self):
        c = Change(type=ChangeType.UPDATE, category=ChangeCategory.REPO, key="has_wiki", old=True, new=False)
        assert "UPDATE" in c.describe()
        assert "→" in c.describe()

    def test_describe_skip(self):
        c = Change(type=ChangeType.SKIP, category=ChangeCategory.SECURITY, key="dependabot", reason="Phase 2")
        assert "SKIP" in c.describe()
        assert "Phase 2" in c.describe()


class TestPlan:
    def test_empty_plan_has_no_changes(self):
        plan = Plan()
        assert not plan.has_changes()

    def test_only_skips_has_no_changes(self):
        plan = Plan()
        plan.add(Change(type=ChangeType.SKIP, category=ChangeCategory.SECURITY, key="x", reason="deferred"))
        assert not plan.has_changes()

    def test_add_triggers_has_changes(self):
        plan = Plan()
        plan.add(make_change(ChangeType.ADD))
        assert plan.has_changes()

    def test_actionable_excludes_skips(self):
        plan = Plan()
        plan.add(make_change(ChangeType.ADD))
        plan.add(Change(type=ChangeType.SKIP, category=ChangeCategory.SECURITY, key="x", reason="r"))
        assert len(plan.actionable_changes) == 1

    def test_skipped_changes(self):
        plan = Plan()
        plan.add(make_change(ChangeType.ADD))
        plan.add(Change(type=ChangeType.SKIP, category=ChangeCategory.SECURITY, key="x", reason="r"))
        assert len(plan.skipped_changes) == 1

    def test_count_by_type(self):
        plan = Plan()
        plan.add(make_change(ChangeType.ADD))
        plan.add(make_change(ChangeType.ADD))
        plan.add(make_change(ChangeType.UPDATE))
        counts = plan.count_by_type()
        assert counts[ChangeType.ADD] == 2
        assert counts[ChangeType.UPDATE] == 1

    def test_merge(self):
        p1 = Plan()
        p1.add(make_change(ChangeType.ADD))
        p2 = Plan()
        p2.add(make_change(ChangeType.UPDATE))
        p1.merge(p2)
        assert len(p1.changes) == 2
