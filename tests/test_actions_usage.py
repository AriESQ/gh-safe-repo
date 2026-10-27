"""Tests for actions_usage.py: what an Actions-policy change would block (#111)."""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from gh_safe_repo import actions_usage as au
from gh_safe_repo.cli import main
from gh_safe_repo.commands._common import format_plan_json, print_plan
from gh_safe_repo.config_manager import ConfigManager
from gh_safe_repo.diff import Change, ChangeCategory, ChangeType, Plan, PlanWarning
from gh_safe_repo.errors import APIError
from gh_safe_repo.github_client import GitHubClient
from gh_safe_repo.security_scanner import SecurityScanner

SHA = "0123456789abcdef0123456789abcdef01234567"
SHA2 = "fedcba9876543210fedcba9876543210fedcba98"


def make_config(overrides=None):
    config = ConfigManager(config_path="/tmp/nonexistent-gh-safe-repo.ini")
    if overrides:
        config.apply_overrides(overrides)
    return config


def policy(**kw):
    base = {
        "allowed_actions": "selected",
        "github_owned_allowed": True,
        "verified_allowed": False,
        "patterns_allowed": [],
        "sha_pinning_required": False,
    }
    base.update(kw)
    return base


ALL = policy(allowed_actions="all")


def ref(uses):
    return au.classify(uses)


def update(key, old, new, type_=ChangeType.UPDATE):
    return Change(type=type_, category=ChangeCategory.ACTIONS, key=key, old=old, new=new)


def plan_of(*changes):
    p = Plan()
    for c in changes:
        p.add(c)
    return p


# ── parsing ─────────────────────────────────────────────────────────────


class TestParseUses:
    def test_step_forms_quotes_and_comments(self):
        text = "\n".join([
            "jobs:",
            "  build:",
            "    steps:",
            "      - uses: actions/checkout@v4",
            "      - name: setup",
            "        uses: 'astral-sh/setup-uv@v5'  # comment",
            f'      - uses: "foo/bar@{SHA}" # v1.2.3',
            f"      - uses: foo/baz@{SHA} # v2",
            "  call:",
            "    uses: octo/wf/.github/workflows/ci.yml@main",
            "      # - uses: commented/out@v1",
            "      - run: echo uses: not/this@v1",
        ])
        assert au.parse_uses(text) == [
            (4, "actions/checkout@v4"),
            (6, "astral-sh/setup-uv@v5"),
            (7, f"foo/bar@{SHA}"),
            (8, f"foo/baz@{SHA}"),
            (10, "octo/wf/.github/workflows/ci.yml@main"),
        ]

    def test_expressions_and_empty_skipped(self):
        text = "- uses: ${{ matrix.action }}\n- uses:\n"
        assert au.parse_uses(text) == []


class TestClassify:
    @pytest.mark.parametrize("uses,kind", [
        ("./.github/actions/setup", "local"),
        ("$/local", "local"),
        ("docker://alpine:3.20", "docker"),
        ("actions/checkout@v4", "action"),
        ("github/codeql-action/init@v3", "action"),
        ("octo/repo/.github/workflows/build.yml@v1", "reusable_workflow"),
        ("octo/repo/.github/workflows/build.yaml@v1", "reusable_workflow"),
        ("actions/checkout", "invalid"),
        ("checkout@v4", "invalid"),
    ])
    def test_kinds(self, uses, kind):
        assert ref(uses).kind == kind

    def test_parts(self):
        r = ref("github/codeql-action/init@v3")
        assert (r.owner, r.repo, r.name, r.ref) == ("github", "codeql-action", "github/codeql-action/init", "v3")
        assert not r.pinned
        assert ref(f"a/b@{SHA}").pinned
        assert ref(f"a/b@{SHA.upper()}").pinned
        assert not ref(f"a/b@{SHA[:7]}").pinned


class TestPatternMatches:
    @pytest.mark.parametrize("pattern,uses,expected", [
        # GitHub matches against name@ref: without `@` no ref matches (live).
        ("astral-sh/setup-uv", "astral-sh/setup-uv@v5", False),
        ("astral-sh/setup-uv@*", "astral-sh/setup-uv@v5", True),
        ("gradle/*", "gradle/actions/setup-gradle@v4", True),
        ("astral-sh/setup-uv", "astral-sh/setup-uv-extra@v5", False),
        ("zizmorcore/zizmor-action@*", "zizmorcore/zizmor-action@v1", True),
        ("foo/bar@v1", "foo/bar@v2", False),
        ("foo/bar@v1", "foo/bar@v1", True),
        ("octocat/*", "octocat/hello@v1", True),
        ("octocat/*", "octocat/repo/.github/workflows/x.yml@v1", True),
        ("space-org*/*", "space-org-two/thing@main", True),
        ("space-org*/*", "other/thing@main", False),
        ("*/octocat**@*", "anyone/octocat-tools@v1", True),
        ("Foo/Bar@*", "foo/bar@v1", True),                           # case-insensitive
        ("*", "anything/at-all@v9", True),
    ])
    def test_matrix(self, pattern, uses, expected):
        assert au.pattern_matches(pattern, ref(uses)) is expected


# ── policy evaluation ──────────────────────────────────────────────────


class TestEvaluate:
    def status(self, uses, pol, owner="alice"):
        return au.evaluate(ref(uses), pol, owner)

    def test_all_allows_everything(self):
        assert self.status("rando/thing@v1", ALL) == (au.ALLOWED, [])

    def test_selected_github_owned(self):
        assert self.status("actions/checkout@v4", policy())[0] == au.ALLOWED
        assert self.status("github/codeql-action/init@v3", policy())[0] == au.ALLOWED
        s, reasons = self.status("actions/checkout@v4", policy(github_owned_allowed=False))
        assert s == au.BLOCKED and reasons == ["not in patterns_allowed"]

    def test_selected_third_party_blocked_unless_pattern(self):
        s, reasons = self.status("astral-sh/setup-uv@v5", policy())
        assert s == au.BLOCKED
        assert reasons == ["not GitHub-owned or in patterns_allowed"]
        assert self.status(
            "astral-sh/setup-uv@v5", policy(patterns_allowed=["astral-sh/setup-uv@*"])
        )[0] == au.ALLOWED

    def test_verified_is_unknown_not_blocked(self):
        s, reasons = self.status("astral-sh/setup-uv@v5", policy(verified_allowed=True))
        assert s == au.UNKNOWN
        assert "verified" in reasons[0]

    def test_bang_pattern_blocks_even_github_owned(self):
        pol = policy(patterns_allowed=["*", "!actions/cache@*"])
        assert self.status("actions/cache@v4", pol)[0] == au.BLOCKED
        assert self.status("rando/thing@v1", pol)[0] == au.ALLOWED

    def test_repo_owner_actions_allowed(self):
        assert self.status("Alice/my-action@v1", policy())[0] == au.ALLOWED
        assert self.status("alice/my-action@v1", policy(allowed_actions="local_only"))[0] == au.ALLOWED

    def test_local_only_blocks_github_owned(self):
        s, reasons = self.status("actions/checkout@v4", policy(allowed_actions="local_only"))
        assert s == au.BLOCKED and reasons == ["allowed_actions = local_only"]

    def test_local_and_invalid_always_allowed(self):
        pol = policy(allowed_actions="local_only", sha_pinning_required=True)
        assert self.status("./.github/actions/x", pol) == (au.ALLOWED, [])
        assert self.status("nonsense", pol) == (au.ALLOWED, [])

    def test_docker_blocked_unless_all(self):
        # Live: blocked under selected (verified allowed too) and local_only.
        assert self.status("docker://alpine", ALL)[0] == au.ALLOWED
        assert self.status("docker://alpine", policy())[0] == au.BLOCKED
        assert self.status("docker://alpine", policy(verified_allowed=True))[0] == au.BLOCKED
        assert self.status("docker://alpine", policy(allowed_actions="local_only"))[0] == au.BLOCKED

    def test_sha_pinning_applies_to_github_owned(self):
        pol = policy(sha_pinning_required=True)
        s, reasons = self.status("actions/checkout@v4", pol)
        assert s == au.BLOCKED
        assert reasons == ["not pinned to a full-length commit SHA"]
        assert self.status(f"actions/checkout@{SHA}", pol)[0] == au.ALLOWED

    def test_sha_pinning_exempts_reusable_workflows(self):
        pol = policy(allowed_actions="all", sha_pinning_required=True)
        assert self.status("octo/r/.github/workflows/ci.yml@v1", pol)[0] == au.ALLOWED

    def test_sha_pinning_beats_verified_unknown(self):
        pol = policy(verified_allowed=True, sha_pinning_required=True)
        s, reasons = self.status("astral-sh/setup-uv@v5", pol)
        assert s == au.BLOCKED
        assert len(reasons) == 2


class TestPolicyAfter:
    def test_none_without_policy_rows(self):
        p = plan_of(update("default_workflow_permissions", "write", "read"))
        assert au.policy_after({}, p) is None

    def test_preserve_and_skip_rows_ignored(self):
        p = plan_of(
            update("allowed_actions", "local_only", "selected", ChangeType.PRESERVE),
            Change(type=ChangeType.SKIP, category=ChangeCategory.ACTIONS,
                   key="sha_pinning_required", reason="Already at desired value"),
        )
        assert au.policy_after({"allowed_actions": "local_only"}, p) is None

    def test_overlays_actionable_rows(self):
        p = plan_of(
            update("allowed_actions", "all", "selected"),
            update("patterns_allowed", [], ["a/b"]),
            update("sha_pinning_required", False, True),
        )
        after = au.policy_after(dict(au.GITHUB_DEFAULTS), p)
        assert after == policy(patterns_allowed=["a/b"], sha_pinning_required=True)


# ── warnings ────────────────────────────────────────────────────────────


class TestWhitelistWarnings:
    def test_each_dropped_pattern(self):
        p = plan_of(update(
            "patterns_allowed",
            ["astral-sh/setup-uv", "keep/me", "zizmorcore/zizmor-action@*"],
            ["keep/me"],
        ))
        ws = au.whitelist_warnings(p)
        assert [w.message for w in ws] == [
            "removes whitelisted action astral-sh/setup-uv",
            "removes whitelisted action zizmorcore/zizmor-action@*",
        ]
        assert ws[0].to_dict() == {
            "kind": "whitelist_pattern_removed",
            "message": "removes whitelisted action astral-sh/setup-uv",
            "pattern": "astral-sh/setup-uv",
        }

    def test_preserved_row_is_not_a_removal(self):
        p = plan_of(update("patterns_allowed", ["x/y"], [], ChangeType.PRESERVE))
        assert au.whitelist_warnings(p) == []


def usages(*pairs):
    return [au.Usage(ref(u), wf, n) for n, (u, wf) in enumerate(pairs, 1)]


class TestUsageWarnings:
    def test_counts_actions_and_workflows(self):
        us = usages(
            ("actions/checkout@v4", ".github/workflows/ci.yml"),
            ("actions/checkout@v4", ".github/workflows/release.yml"),
            ("astral-sh/setup-uv@v5", ".github/workflows/ci.yml"),
            (f"actions/cache@{SHA}", ".github/workflows/ci.yml"),
            ("./.github/actions/local", ".github/workflows/ci.yml"),
        )
        ws = au.usage_warnings(us, 2, ALL, policy(sha_pinning_required=True), "alice",
                               branch="main", source="alice/demo")
        assert len(ws) == 1
        w = ws[0]
        assert w.kind == "actions_blocked"
        assert w.message == "would block 2 actions in 2 workflows"
        d = w.to_dict()
        assert d["branch"] == "main" and d["source"] == "alice/demo"
        assert d["workflows_scanned"] == 2
        assert [a["uses"] for a in d["actions"]] == [
            "actions/checkout@v4", "actions/checkout@v4", "astral-sh/setup-uv@v5",
        ]
        assert d["actions"][2]["reasons"] == [
            "not GitHub-owned or in patterns_allowed",
            "not pinned to a full-length commit SHA",
        ]

    def test_singular_message(self):
        us = usages(("actions/checkout@v4", "w.yml"))
        ws = au.usage_warnings(us, 1, ALL, policy(sha_pinning_required=True), "alice")
        assert ws[0].message == "would block 1 action in 1 workflow"

    def test_already_blocked_is_not_reported(self):
        us = usages(("rando/thing@v1", "w.yml"))
        before = policy()                      # already blocked: not whitelisted
        after = policy(sha_pinning_required=True)
        assert au.usage_warnings(us, 1, before, after, "alice") == []

    def test_unknown_reported_separately(self):
        us = usages(("astral-sh/setup-uv@v5", "w.yml"), ("hashicorp/setup-terraform@v3", "w.yml"))
        ws = au.usage_warnings(us, 1, ALL, policy(verified_allowed=True), "alice")
        assert [w.kind for w in ws] == ["actions_unknown"]
        assert ws[0].message == "cannot confirm 2 actions in 1 workflow will still run"

    def test_pin_hint_for_unpinned_refs_once_per_ref(self):
        client = MagicMock()
        client.repo_path.side_effect = GitHubClient.repo_path
        client.get_json.return_value = {"sha": SHA2}
        us = usages(("actions/checkout@v4", "w.yml"), ("actions/checkout@v4", "x.yml"),
                    (f"rando/thing@{SHA}", "w.yml"))
        ws = au.usage_warnings(us, 2, ALL, policy(sha_pinning_required=True), "alice",
                               client=client)
        acts = ws[0].to_dict()["actions"]
        assert acts[0]["pin"] == f"actions/checkout@{SHA2}"
        assert acts[1]["pin"] == f"actions/checkout@{SHA2}"
        assert "pin" not in acts[2]                       # pinned, but not whitelisted
        client.get_json.assert_called_once_with("/repos/actions/checkout/commits/v4")

    def test_pin_lookup_failure_is_silent(self):
        client = MagicMock()
        client.repo_path.side_effect = GitHubClient.repo_path
        client.get_json.side_effect = APIError("nope", status_code=404)
        us = usages(("actions/checkout@v4", "w.yml"))
        ws = au.usage_warnings(us, 1, ALL, policy(sha_pinning_required=True), "alice",
                               client=client)
        assert "pin" not in ws[0].to_dict()["actions"][0]

    def test_pin_lookups_capped(self):
        client = MagicMock()
        client.repo_path.side_effect = GitHubClient.repo_path
        client.get_json.return_value = {"sha": SHA2}
        us = usages(*[(f"actions/a{i}@v1", "w.yml") for i in range(au.MAX_PIN_LOOKUPS + 5)])
        au.usage_warnings(us, 1, ALL, policy(sha_pinning_required=True), "alice", client=client)
        assert client.get_json.call_count == au.MAX_PIN_LOOKUPS


# ── sources ─────────────────────────────────────────────────────────────


def b64(text):
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


CI_YML = f"""\
jobs:
  test:
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/setup
      - uses: astral-sh/setup-uv@{SHA}
"""
SETUP_ACTION = """\
runs:
  using: composite
  steps:
    - uses: actions/cache@v4
"""


def api_client(files, workflows_status=200):
    """MagicMock client serving `files` ({path: text}) via the contents API."""
    client = MagicMock()
    client.repo_path.side_effect = GitHubClient.repo_path

    def get_json(endpoint):
        path, _, query = endpoint.partition("?")
        assert query == "ref=main", endpoint
        rel = path.split("/contents/", 1)[1]
        if rel == ".github/workflows":
            if workflows_status != 200:
                raise APIError("x", status_code=workflows_status)
            return [
                {"type": "file", "path": p, "name": p.rsplit("/", 1)[1]}
                for p in files if p.startswith(".github/workflows/")
            ] + [{"type": "dir", "path": ".github/workflows/sub"}]
        if rel in files:
            return b64(files[rel])
        raise APIError("missing", status_code=404)

    client.get_json.side_effect = get_json
    return client


class TestApiSource:
    def test_collects_workflows_and_local_composite(self):
        client = api_client({
            ".github/workflows/ci.yml": CI_YML,
            ".github/workflows/notes.md": "uses: ignored/file@v1",
            ".github/actions/setup/action.yml": SETUP_ACTION,
        })
        src = au.ApiSource(client, "alice", "demo", "main")
        us, count = au.collect_usages(src)
        assert count == 1
        ci, act = ".github/workflows/ci.yml", ".github/actions/setup/action.yml"
        assert [(u.ref.uses, u.workflow, u.file, u.line) for u in us] == [
            ("actions/checkout@v4", ci, ci, 4),
            ("./.github/actions/setup", ci, ci, 5),
            ("actions/cache@v4", ci, act, 4),
            (f"astral-sh/setup-uv@{SHA}", ci, ci, 6),
        ]
        # Read-only: only GETs.
        assert not client.call_json.called and not client.call_api.called

    def test_local_action_counted_for_every_workflow_and_read_once(self):
        client = api_client({
            ".github/workflows/a.yml": "steps:\n  - uses: ./.github/actions/setup\n",
            ".github/workflows/b.yml": "steps:\n  - uses: ./.github/actions/setup\n",
            ".github/actions/setup/action.yml": SETUP_ACTION,
        })
        us, _ = au.collect_usages(au.ApiSource(client, "alice", "demo", "main"))
        nested = [u.workflow for u in us if u.ref.uses == "actions/cache@v4"]
        assert nested == [".github/workflows/a.yml", ".github/workflows/b.yml"]
        reads = [c.args[0] for c in client.get_json.call_args_list if "action.yml" in c.args[0]]
        assert len(reads) == 1

    def test_self_referencing_local_action_terminates(self):
        client = api_client({
            ".github/workflows/a.yml": "steps:\n  - uses: ./loop\n",
            "loop/action.yml": "runs:\n  steps:\n    - uses: ./loop\n",
        })
        us, _ = au.collect_usages(au.ApiSource(client, "alice", "demo", "main"))
        assert [u.ref.uses for u in us] == ["./loop", "./loop"]

    def test_no_workflow_dir(self):
        src = au.ApiSource(api_client({}, workflows_status=404), "alice", "demo", "main")
        assert au.collect_usages(src) == ([], 0)

    def test_read_error_becomes_skip_warning(self):
        src = au.ApiSource(api_client({}, workflows_status=403), "alice", "demo", "main")
        p = plan_of(update("sha_pinning_required", False, True))
        ws = au.plan_warnings(p, dict(au.GITHUB_DEFAULTS), "alice", source=src)
        assert [w.kind for w in ws] == ["actions_scan_skipped"]
        assert "could not read workflows" in ws[0].message


class TestGitSource:
    def test_reads_branch_with_git(self):
        def run(cmd, **kw):
            r = MagicMock(returncode=0)
            if cmd[3] == "ls-tree":
                assert cmd[4:] == ["--name-only", "main", ".github/workflows/"]
                r.stdout = ".github/workflows/ci.yml\n"
            elif cmd[3] == "show" and cmd[4] == "main:.github/workflows/ci.yml":
                r.stdout = "steps:\n  - uses: actions/checkout@v4\n"
            else:
                r.returncode, r.stdout = 128, ""
            return r

        with patch("gh_safe_repo.actions_usage.subprocess.run", side_effect=run):
            us, count = au.collect_usages(au.GitSource("/src", "main"))
        assert count == 1
        assert [u.ref.uses for u in us] == ["actions/checkout@v4"]

    def test_git_failure_is_empty(self):
        with patch("gh_safe_repo.actions_usage.subprocess.run", side_effect=OSError):
            assert au.collect_usages(au.GitSource("/src", None)) == ([], 0)


class TestPlanWarnings:
    def test_skip_reason_only_when_policy_changes(self):
        p = plan_of(update("sha_pinning_required", False, True))
        ws = au.plan_warnings(p, dict(au.GITHUB_DEFAULTS), "alice", skip_reason="dry run")
        assert [w.kind for w in ws] == ["actions_scan_skipped"]
        assert au.plan_warnings(Plan(), {}, "alice", skip_reason="dry run") == []

    def test_no_source_no_reason_is_silent(self):
        p = plan_of(update("sha_pinning_required", False, True))
        assert au.plan_warnings(p, dict(au.GITHUB_DEFAULTS), "alice") == []


# ── output ──────────────────────────────────────────────────────────────


class TestOutput:
    def _plan(self):
        p = plan_of(update("sha_pinning_required", False, True))
        p.warnings.append(PlanWarning(
            kind="actions_blocked",
            message="would block 1 action in 1 workflow",
            details={"branch": "main", "source": "alice/demo", "workflows_scanned": 1,
                     "actions": [{"uses": "actions/checkout@v4", "kind": "action",
                                  "workflow": ".github/workflows/ci.yml", "line": 4,
                                  "reasons": ["not pinned to a full-length commit SHA"],
                                  "pin": f"actions/checkout@{SHA}"}]},
        ))
        return p

    def test_json_always_has_warnings(self):
        assert json.loads(format_plan_json(Plan()))["warnings"] == []
        doc = json.loads(format_plan_json(self._plan()))
        assert doc["warnings"][0]["kind"] == "actions_blocked"
        assert doc["warnings"][0]["actions"][0]["pin"] == f"actions/checkout@{SHA}"

    def test_table_prints_warnings_section(self, capsys):
        print_plan(self._plan())
        out = capsys.readouterr().out
        assert "Warnings" in out
        assert "⚠ would block 1 action in 1 workflow (branch main):" in out
        assert "actions/checkout@v4  .github/workflows/ci.yml:4" in out
        assert f"→ pin: actions/checkout@{SHA}" in out

    def test_merge_carries_warnings(self):
        a = self._plan()
        b = Plan()
        b.merge(a)
        assert b.warnings == a.warnings


# ── commands ────────────────────────────────────────────────────────────


REPO = {
    "id": 1, "full_name": "alice/demo", "name": "demo",
    "owner": {"login": "alice", "type": "User"},
    "private": False, "default_branch": "main",
    "permissions": {"admin": True},
}


def fix_client(actions_state, files):
    """Client for a real ActionsPlugin + workflow reads; everything else mocked."""
    client = api_client(files)
    serve_files = client.get_json.side_effect

    def get_json(endpoint):
        if endpoint.endswith("/actions/permissions"):
            return {"allowed_actions": actions_state.get("allowed_actions", "all"),
                    "sha_pinning_required": actions_state.get("sha_pinning_required", False)}
        if endpoint.endswith("/actions/permissions/workflow"):
            return {"default_workflow_permissions": "read",
                    "can_approve_pull_request_reviews": False}
        if endpoint.endswith("/actions/permissions/selected-actions"):
            return {"github_owned_allowed": True, "verified_allowed": False,
                    "patterns_allowed": actions_state.get("patterns_allowed", [])}
        if endpoint.endswith("/actions/permissions/fork-pr-contributor-approval"):
            return {"approval_policy": "all_external_contributors"}
        if "/commits/" in endpoint:
            return {"sha": SHA2}
        return serve_files(endpoint)

    client.get_json.side_effect = get_json
    client.get_repo_data.return_value = dict(REPO)
    return client


def run_fix(argv, client, config):
    names = ["RepositoryPlugin", "BranchProtectionPlugin", "SecurityPlugin", "TagProtectionPlugin"]
    patches = [patch(f"gh_safe_repo.commands.fix.{n}") for n in names]
    try:
        for p in patches:
            M = p.start()
            M.return_value.plan.return_value = Plan()
            M.return_value.fetch_current_state.return_value = {}
        with patch("sys.argv", ["gh-safe-repo", *argv]), \
             patch("gh_safe_repo.commands.fix.build_context") as ctx, \
             patch("gh_safe_repo.verify.time"), \
             patch("builtins.input") as prompt:
            ctx.return_value = MagicMock(
                client=client, owner="alice", plan_name="free",
                is_paid_plan=False, config=config,
            )
            try:
                main()
                code = 0
            except SystemExit as e:
                code = e.code
        prompt.assert_not_called()
        return code
    finally:
        for p in patches:
            p.stop()


WF = {".github/workflows/ci.yml": "steps:\n  - uses: actions/checkout@v4\n  - uses: astral-sh/setup-uv@v5\n"}


class TestFixIntegration:
    def test_audit_lists_blocked_actions_and_removed_patterns(self, capsys):
        client = fix_client(
            {"allowed_actions": "selected", "patterns_allowed": ["astral-sh/setup-uv@*"]}, WF,
        )
        # Declared [actions]: the config wins, so the weaker whitelist is replaced.
        config = make_config()
        config._declared_sections = frozenset({"actions"})
        code = run_fix(["audit", "alice/demo"], client, config)
        assert code == 3
        out = capsys.readouterr().out
        assert "⚠ removes whitelisted action astral-sh/setup-uv@*" in out
        assert "⚠ would block 2 actions in 1 workflow (branch main):" in out
        assert f"→ pin: actions/checkout@{SHA2}" in out
        # Reads only.
        assert not client.call_json.called

    def test_fix_json_yes_carries_warnings_and_still_applies(self, capsys):
        client = fix_client({"allowed_actions": "all"}, WF)
        client.call_json.return_value = {}
        code = run_fix(["fix", "alice/demo", "--yes", "--json"], client, make_config())
        captured = capsys.readouterr()
        doc = json.loads(captured.out)
        kinds = [w["kind"] for w in doc["warnings"]]
        assert kinds == ["actions_blocked"]
        assert {a["uses"] for a in doc["warnings"][0]["actions"]} == {
            "actions/checkout@v4", "astral-sh/setup-uv@v5",
        }
        assert "would block 2 actions" in captured.err
        # Replace-and-warn: the policy change was still applied.
        puts = [c.args[1] for c in client.call_json.call_args_list if c.args[0] == "PUT"]
        assert "/repos/alice/demo/actions/permissions" in puts
        assert code in (0, 3)

    def test_no_policy_change_no_scan(self, capsys):
        client = fix_client({"allowed_actions": "selected", "sha_pinning_required": True,
                             "patterns_allowed": []}, WF)
        config = make_config({("actions", "verified_allowed"): "false"})
        run_fix(["audit", "alice/demo", "--json"], client, config)
        doc = json.loads(capsys.readouterr().out)
        assert doc["warnings"] == []
        assert not any("/contents/" in c.args[0] for c in client.get_json.call_args_list)


class TestCreateIntegration:
    def _run(self, argv, client):
        with patch("sys.argv", ["gh-safe-repo", *argv]), \
             patch("gh_safe_repo.commands.create.build_context") as ctx:
            ctx.return_value = MagicMock(
                client=client, owner="alice", plan_name="free",
                is_paid_plan=False, config=make_config(),
            )
            with pytest.raises(SystemExit) as exc:
                main()
        return exc.value.code

    def test_copy_dry_run_skips_scan_with_zero_api_calls(self, capsys):
        client = MagicMock()
        code = self._run(["copy", "alice/src", "alice/dest", "--dry-run", "--json"], client)
        assert code == 0
        doc = json.loads(capsys.readouterr().out)
        assert [w["kind"] for w in doc["warnings"]] == ["actions_scan_skipped"]
        assert not client.get_json.called and not client.call_api.called

    def test_push_dry_run_reads_local_workflows(self, tmp_path, capsys):
        (tmp_path / ".git").mkdir()
        client = MagicMock()
        wf = "steps:\n  - uses: actions/checkout@v4\n"

        def fake_git(self, *args):
            # Patch the source's own runner, never the shared subprocess.run.
            return ".github/workflows/ci.yml\n" if args[0] == "ls-tree" else wf

        with patch.object(au.GitSource, "_git", fake_git), \
             patch.object(SecurityScanner, "_run_discovery", return_value={"method": "none"}), \
             patch("gh_safe_repo.commands.create._local_branches", return_value=(["main"], "main")):
            code = self._run(["push", str(tmp_path), "alice/dest", "--dry-run", "--json"], client)
        assert code == 0
        doc = json.loads(capsys.readouterr().out)
        assert [w["kind"] for w in doc["warnings"]] == ["actions_blocked"]
        assert doc["warnings"][0]["branch"] == "main"
        assert "pin" not in doc["warnings"][0]["actions"][0]   # no API calls in dry run
        assert not client.get_json.called and not client.call_api.called
