"""Tests for post-apply verification (gh_safe_repo/verify.py, #77)."""

from unittest.mock import MagicMock

import pytest

from gh_safe_repo.diff import Change, ChangeCategory, ChangeType, Plan
from gh_safe_repo.errors import APIError
from gh_safe_repo.verify import (
    MISMATCH,
    OK,
    UNKNOWN,
    Check,
    Probe,
    Verification,
    create_probes,
    verify,
    verify_create,
    verify_plugins,
)


def upd(key, old, new, cat=ChangeCategory.SECURITY):
    return Change(type=ChangeType.UPDATE, category=cat, key=key, old=old, new=new)


def skip(key, cat=ChangeCategory.SECURITY, reason="Already at desired value"):
    return Change(type=ChangeType.SKIP, category=cat, key=key, reason=reason)


def plan_of(*changes):
    p = Plan()
    for c in changes:
        p.add(c)
    return p


def make_plugin(*audits):
    """A plugin whose successive post-apply audits return `audits` in order.

    An Exception instance in `audits` is raised by fetch_current_state().
    """
    plugin = MagicMock()
    plugin.owner, plugin.repo = "alice", "demo"
    fetches = []
    plans = []
    for a in audits:
        if isinstance(a, Exception):
            fetches.append(a)
        else:
            fetches.append({})
            plans.append(a)
    plugin.fetch_current_state.side_effect = fetches
    plugin.plan.side_effect = plans
    return plugin


class TestModel:
    def test_empty_verification_is_ok(self):
        v = Verification()
        assert v.ok and v.exit_code() == 0
        assert v.to_dict() == {
            "ok": True, "checks": [],
            "summary": {"ok": 0, "mismatch": 0, "unknown": 0},
        }

    def test_exit_codes(self):
        assert Verification([Check("repo", "a", UNKNOWN)]).exit_code() == 0
        assert Verification([Check("repo", "a", MISMATCH)]).exit_code() == 3
        assert Verification([
            Check("repo", "a", MISMATCH),
            Check("code", "refs", MISMATCH, fatal=True),
        ]).exit_code() == 1

    def test_unknown_is_not_ok(self):
        v = Verification([Check("repo", "a", OK), Check("repo", "b", UNKNOWN)])
        assert not v.ok
        assert v.summary() == {"ok": 1, "mismatch": 0, "unknown": 1}

    def test_invalid_status_rejected(self):
        with pytest.raises(ValueError):
            Check("repo", "a", "maybe")

    def test_to_dict_shape(self):
        d = Check("security", "x", MISMATCH, expected=True, actual=False,
                  remediation="r").to_dict()
        assert d == {
            "category": "security", "key": "x", "status": "mismatch",
            "expected": True, "actual": False, "fatal": False, "remediation": "r",
        }


class TestVerifyPlugins:
    def test_clean_audit_maps_to_ok(self):
        applied = plan_of(upd("dependabot_alerts", False, True))
        plugin = make_plugin(plan_of(skip("dependabot_alerts")))
        sleep = MagicMock()
        v = verify_plugins([(plugin, applied)], sleep=sleep)
        assert [(c.name, c.status, c.expected, c.actual) for c in v.checks] == [
            ("security/dependabot_alerts", OK, True, True),
        ]
        sleep.assert_not_called()
        plugin.fetch_current_state.assert_called_once_with(strict=True)
        plugin.client.forget_repo.assert_called_with("alice", "demo")

    def test_persistent_mismatch_maps_expected_and_actual(self):
        applied = plan_of(upd("dependabot_alerts", False, True))
        still = plan_of(upd("dependabot_alerts", False, True))
        plugin = make_plugin(still, still, still, still)
        sleep = MagicMock()
        v = verify_plugins([(plugin, applied)], sleep=sleep, remediation="fix it")
        (c,) = v.checks
        assert (c.status, c.expected, c.actual, c.remediation) == (
            MISMATCH, True, False, "fix it",
        )
        assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4]
        assert v.exit_code() == 3

    def test_unrelated_regression_is_mismatch(self):
        applied = plan_of(upd("a", False, True))
        audit = plan_of(skip("a"), upd("b", False, True))
        plugin = make_plugin(audit, audit, audit, audit)
        v = verify_plugins([(plugin, applied)], sleep=MagicMock())
        assert [(c.key, c.status) for c in v.checks] == [("a", OK), ("b", MISMATCH)]

    def test_retry_then_success(self):
        applied = plan_of(upd("a", False, True))
        plugin = make_plugin(
            plan_of(upd("a", False, True)),   # eventual consistency: not yet
            APIError("boom", status_code=502),
            plan_of(skip("a")),
        )
        sleep = MagicMock()
        v = verify_plugins([(plugin, applied)], sleep=sleep)
        assert v.ok
        assert [call.args[0] for call in sleep.call_args_list] == [1, 2]

    def test_retry_then_unknown(self):
        applied = plan_of(upd("a", False, True), upd("b", False, True))
        err = APIError("GET x returned 403", status_code=403)
        plugin = make_plugin(err, err, err, err)
        v = verify_plugins([(plugin, applied)], sleep=MagicMock())
        assert [(c.key, c.status) for c in v.checks] == [("a", UNKNOWN), ("b", UNKNOWN)]
        assert v.exit_code() == 0
        assert "403" in v.checks[0].remediation
        assert plugin.fetch_current_state.call_count == 4

    def test_all_skip_plugin_is_never_fetched(self):
        applied = plan_of(skip("branch_protection", reason="Requires paid plan"))
        plugin = make_plugin()
        v = verify_plugins([(plugin, applied), (make_plugin(), Plan())],
                           sleep=MagicMock())
        assert v.checks == []
        plugin.fetch_current_state.assert_not_called()

    def test_rounds_share_one_backoff_budget(self):
        """Two slow plugins retry together: total sleep is 1+2+4, not doubled."""
        applied = plan_of(upd("a", False, True))
        bad = plan_of(upd("a", False, True))
        p1 = make_plugin(bad, bad, bad, bad)
        p2 = make_plugin(bad, bad, bad, bad)
        good = make_plugin(plan_of(skip("a")))
        sleep = MagicMock()
        verify_plugins([(p1, applied), (good, applied), (p2, applied)], sleep=sleep)
        assert sum(c.args[0] for c in sleep.call_args_list) == 7
        good.fetch_current_state.assert_called_once()

    def test_create_only_rows_are_not_reported(self):
        applied = plan_of(
            Change(type=ChangeType.ADD, category=ChangeCategory.REPO,
                   key="repository", new="alice/demo"),
            Change(type=ChangeType.UPDATE, category=ChangeCategory.REPO,
                   key="has_wiki", old=True, new=False),
        )
        plugin = make_plugin(plan_of(skip("has_wiki", cat=ChangeCategory.REPO)))
        v = verify_plugins([(plugin, applied)], sleep=MagicMock())
        assert [c.name for c in v.checks] == ["repo/has_wiki"]


def add(cat, key, new=None, type_=ChangeType.ADD):
    return Change(type=type_, category=cat, key=key, new=new)


class TestProbes:
    def test_probe_ok_mismatch_unknown(self):
        reads = iter(["no", "yes"])
        flaky = Probe("x", "flaky", "yes", lambda: next(reads))
        wrong = Probe("x", "wrong", 1, lambda: 2, fatal=True, remediation="do it")

        def boom():
            raise APIError("GET y returned 500", status_code=500)
        broken = Probe("x", "broken", True, boom, fatal=True)
        sleep = MagicMock()
        v = verify(probes=[flaky, wrong, broken], sleep=sleep)
        assert [(c.key, c.status, c.fatal) for c in v.checks] == [
            ("flaky", OK, False),
            ("wrong", MISMATCH, True),
            ("broken", UNKNOWN, False),  # unreadable is never fatal
        ]
        assert v.checks[1].remediation == "do it"
        assert "500" in v.checks[2].remediation
        assert v.exit_code() == 1
        assert sum(c.args[0] for c in sleep.call_args_list) == 7

    def test_probes_and_plugins_share_rounds(self):
        bad = plan_of(upd("a", False, True))
        plugin = make_plugin(bad, bad, bad, bad)
        probe = Probe("x", "p", 1, lambda: 0)
        sleep = MagicMock()
        v = verify([(plugin, bad)], [probe], sleep=sleep)
        assert [c.key for c in v.checks] == ["p", "a"]  # probes first
        assert sum(c.args[0] for c in sleep.call_args_list) == 7


def make_client(repo=None, readme_status=404, topics=()):
    client = MagicMock()
    client.repo_path.side_effect = lambda o, r, s="": f"/repos/{o}/{r}" + (f"/{s}" if s else "")
    if isinstance(repo, Exception):
        client.get_repo_data.side_effect = repo
    else:
        client.get_repo_data.return_value = {"private": True, **(repo or {})}
    client.call_api.return_value = (readme_status, "{}")
    client.get_json.return_value = {"names": list(topics)}
    return client


def owned(plugin, client):
    plugin.owner, plugin.repo, plugin.client = "alice", "demo", client
    return plugin


class TestCreateProbes:
    R = ChangeCategory.REPO

    def test_one_probe_per_create_only_row(self):
        client = make_client()
        repo = owned(MagicMock(), client)
        bp = owned(MagicMock(branches=["main"]), client)
        tag = owned(MagicMock(), client)
        pairs = [
            (repo, plan_of(
                add(self.R, "description", "d"),
                add(self.R, "repository", "alice/demo"),
                add(self.R, "topics", "a, b"),
                add(ChangeCategory.FILE, "readme", type_=ChangeType.DELETE),
                upd("has_wiki", True, False, cat=self.R),
            )),
            (bp, plan_of(add(ChangeCategory.BRANCH_PROTECTION, "protected_branches", "master, main"))),
            (tag, plan_of(add(ChangeCategory.TAG_PROTECTION, "protected_tags", "v*, release-*"))),
            (owned(MagicMock(), client), plan_of(skip("branch_protection"))),
        ]
        probes = create_probes(pairs, private=True)
        assert [(p.name, p.expected) for p in probes] == [
            ("repo/repository", "exists"),
            ("repo/description", "d"),
            ("repo/visibility", "private"),
            ("repo/topics", ["a", "b"]),
            ("file/readme", "absent"),
            # the narrowed list on the instance, not the plan's "master, main"
            ("branch_protection/protected_branches", ["main"]),
            ("tag_protection/protected_tags", ["v*", "release-*"]),
        ]
        assert [p.fatal for p in probes[:3]] == [True, False, True]

    def test_reads(self):
        client = make_client(repo={"description": "d"}, topics=("b", "a", "extra"))
        repo = owned(MagicMock(), client)
        pairs = [(repo, plan_of(
            add(self.R, "repository", "alice/demo"),
            add(self.R, "description", "d"),
            add(self.R, "topics", "a, b"),
            add(ChangeCategory.FILE, "readme", type_=ChangeType.DELETE),
        ))]
        v = verify_create(pairs, private=True, sleep=MagicMock())
        assert v.ok, v.to_dict()
        client.get_repo_data.assert_called_with("alice", "demo", refresh=True)
        client.get_json.assert_called_with("/repos/alice/demo/topics")
        client.call_api.assert_called_with("GET", "/repos/alice/demo/contents/README.md")

    def test_readme_status_mapping(self):
        pairs = lambda c: [(owned(MagicMock(), c), plan_of(
            add(ChangeCategory.FILE, "readme", type_=ChangeType.DELETE)))]
        v = verify_create(pairs(make_client(readme_status=200)), private=True, sleep=MagicMock())
        assert [(c.status, c.actual) for c in v.checks] == [(MISMATCH, "present")]
        v = verify_create(pairs(make_client(readme_status=403)), private=True, sleep=MagicMock())
        assert v.checks[0].status == UNKNOWN

    def test_missing_repo_short_circuits(self):
        client = make_client(repo=APIError("GET /repos/alice/demo returned 404", status_code=404))
        sec = make_plugin()
        pairs = [
            (owned(MagicMock(), client), plan_of(add(self.R, "repository", "alice/demo"))),
            (sec, plan_of(upd("dependabot_alerts", False, True))),
        ]
        sleep = MagicMock()
        v = verify_create(pairs, private=True, sleep=sleep)
        assert [(c.name, c.status, c.actual) for c in v.checks] == [
            ("repo/repository", MISMATCH, "missing"),
        ]
        assert v.exit_code() == 1
        sec.fetch_current_state.assert_not_called()
        assert sum(c.args[0] for c in sleep.call_args_list) == 7

    def test_public_repo_meant_private_is_fatal(self):
        client = make_client(repo={"private": False})
        pairs = [(owned(MagicMock(), client), plan_of(add(self.R, "repository", "alice/demo")))]
        v = verify_create(pairs, private=True, sleep=MagicMock())
        assert [(c.key, c.status) for c in v.checks] == [
            ("repository", OK), ("visibility", MISMATCH),
        ]
        assert v.exit_code() == 1
        assert "--visibility private" in v.checks[1].remediation

    def test_extra_probes_hook(self):
        client = make_client()
        pairs = [(owned(MagicMock(), client), plan_of(add(self.R, "repository", "alice/demo")))]
        extra = Probe("code", "refs", ["main"], lambda: ["main"], fatal=True)
        v = verify_create(pairs, private=True, extra_probes=[extra], sleep=MagicMock())
        assert [c.name for c in v.checks] == ["repo/repository", "repo/visibility", "code/refs"]
