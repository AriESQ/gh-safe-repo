"""Auth persona matrix (#122, Recommendation D of the 2026-05-06 auth review).

Each test builds one persona's environment with `tests/personas.py` and runs
`push` or `copy` through `cli.main()`. Only the subprocess boundary is faked
for auth: `GitHubClient`, `discover_transport`, `GitTransport.preflight`,
`push_local`, `copy_repo` and `clone_for_scan` all run for real, against real
temp git repos. What each persona should do, and what is not covered, is in
docs/2026-11-27_auth-persona-matrix-scope.md.

P9 (two gh accounts, #121) is deliberately absent until #121 lands.
"""

import dataclasses
import os

import pytest

from tests.personas import Persona, drive, make_local_repo, origin_url

SK_COMMAND = "ssh -i ~/.ssh/id_ed25519_sk -o IdentitiesOnly=yes -o IdentityAgent=none"
CI_TOKEN = "ghp_ci_fine_grained_0123456789"
APP_TOKEN = "ghs_installation_0123456789"


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch):
    """The developer's own git config must not shape the temp repos."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    def _run(persona, *argv, cwd=None):
        return drive(persona, list(argv), tmp_path=tmp_path,
                     monkeypatch=monkeypatch, capsys=capsys, cwd=cwd)
    return _run


@pytest.fixture
def src(tmp_path):
    """A local repo to `push`, outside any includeIf scope."""
    return make_local_repo(tmp_path / "work" / "proj")


# ---------------------------------------------------------------- personas

P2 = Persona("P2 solo ssh", repos={"alice/src": {}})

P3 = Persona("P3 https + gh helper", gh_protocol="https",
             credential_helpers=("!/usr/bin/gh auth git-credential",),
             repos={"alice/src": {}})

P4 = Persona("P4 CI GITHUB_TOKEN", gh_token=None, gh_protocol=None,
             env={"GITHUB_TOKEN": CI_TOKEN}, repos={"alice/src": {}})

P5 = Persona("P5 SAML SSO org member", sso_orgs=("acme",),
             repos={"alice/src": {}, "acme/src": {}})

P6 = Persona("P6 GitHub App token", gh_token=None, gh_protocol=None,
             env={"GITHUB_TOKEN": APP_TOKEN}, installation_token=True)

P12 = Persona("P12 GCM", gh_protocol="https", credential_helpers=("manager",),
              repos={"alice/src": {}})


def sk_persona(name, scope):
    """P1 / P10: an SK key that only signs through core.sshCommand.

    scope=None is a global core.sshCommand; a path is an includeIf scope.
    """
    return Persona(name, ssh_command=SK_COMMAND, ssh_command_scope=scope,
                   ssh_key_needs_command=True, repos={"alice/src": {}})


@pytest.fixture
def scoped(tmp_path):
    """includeIf "gitdir:<home>/alt-account/" with a repo inside it."""
    scope = tmp_path / "home" / "alt-account"
    repo = make_local_repo(scope / "proj")
    return scope, repo


def _tokens_absent(outcome, *tokens):
    for token in tokens:
        assert token not in outcome.text, "token leaked into output"


# ---------------------------------------------------------------- P2

class TestP2SoloSsh:
    """The happy path and the regression canary."""

    def test_push_succeeds_over_ssh(self, run, src):
        o = run(P2, "push", str(src), "alice/dst")
        assert o.code == 0, o.err
        assert [op.verb for op in o.world.net] == ["ls-remote", "push"]
        assert all(op.url.startswith("git@github.com:") for op in o.world.net)
        assert o.world.ops("push")[0].url == "git@github.com:alice/dst.git"
        assert "GIT_SSH_COMMAND" not in o.world.ops("push")[0].env
        assert origin_url(src) == "git@github.com:alice/dst.git"
        assert o.created

    def test_copy_succeeds_over_ssh(self, run):
        o = run(P2, "copy", "alice/src", "alice/dst")
        assert o.code == 0, o.err
        assert [(op.verb, op.url) for op in o.world.net] == [
            ("clone", "git@github.com:alice/src.git"),        # pre-flight scan clone
            ("ls-remote", "git@github.com:gh-safe-repo-preflight/nonexistent.git"),
            ("clone", "git@github.com:alice/src.git"),        # --mirror
            ("push", "git@github.com:alice/dst.git"),
        ]

    def test_ssh_user_never_gets_token_injection(self, run, src):
        """Even with GITHUB_TOKEN exported, an SSH user keeps their own key
        (the workflow-scope reason token injection was removed, b822e9c)."""
        p = dataclasses.replace(P2, env={"GITHUB_TOKEN": CI_TOKEN})
        o = run(p, "push", str(src), "alice/dst")
        assert o.code == 0, o.err
        for call in o.world.calls:
            assert not any("x-access-token" in a or CI_TOKEN in a for a in call.argv
                           if call.argv[0] == "git")


# ---------------------------------------------------------------- P1 / P10

SCOPED_SK = [
    pytest.param("P1 macOS YubiKey via includeIf", id="P1"),
    pytest.param("P10 Linux SK key via includeIf", id="P10-includeIf"),
]


class TestSkKeyIncludeIf:
    """P1 and P10 share the structural failure (review §P10): a per-directory
    core.sshCommand that must follow the push into a temp dir."""

    @pytest.mark.parametrize("name", SCOPED_SK)
    def test_push_from_inside_scope_carries_ssh_command_into_temp_dir(self, run, scoped, name):
        scope, repo = scoped
        o = run(sk_persona(name, str(scope)), "push", str(repo), "alice/dst")
        assert o.code == 0, o.err
        preflight, push = o.world.ops("ls-remote")[0], o.world.ops("push")[0]
        assert preflight.env["GIT_SSH_COMMAND"] == SK_COMMAND
        assert push.env["GIT_SSH_COMMAND"] == SK_COMMAND
        # The push ran from a temp clone where includeIf no longer matches.
        assert not os.path.realpath(push.cwd).startswith(os.path.realpath(str(scope)))

    @pytest.mark.parametrize("name", SCOPED_SK)
    def test_copy_from_inside_scope_succeeds(self, run, scoped, name):
        scope, _ = scoped
        o = run(sk_persona(name, str(scope)), "copy", "alice/src", "alice/dst", cwd=scope)
        assert o.code == 0, o.err
        assert all(op.env.get("GIT_SSH_COMMAND") == SK_COMMAND for op in o.world.net)

    @pytest.mark.parametrize("name", SCOPED_SK)
    def test_copy_from_outside_scope_fails_before_creating(self, run, scoped, tmp_path, name):
        """#100: the scan clone fails first; the hint names the directory
        and says core.sshCommand was not found there."""
        scope, _ = scoped
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        o = run(sk_persona(name, str(scope)), "copy", "alice/src", "alice/dst", cwd=elsewhere)
        assert o.code == 1
        assert not o.created
        assert "Pre-flight scan failed" in o.err
        assert "Likely cause:" in o.err
        assert str(elsewhere.resolve()) in o.err or str(elsewhere) in o.err
        assert "core.sshCommand: not set" in o.err
        assert o.world.ops("push") == []

    def test_p10_global_ssh_command_works_from_any_directory(self, run, tmp_path):
        p = sk_persona("P10 Linux SK key, global core.sshCommand", None)
        anywhere = tmp_path / "anywhere"
        anywhere.mkdir()
        o = run(p, "copy", "alice/src", "alice/dst", cwd=anywhere)
        assert o.code == 0, o.err
        assert all(op.env.get("GIT_SSH_COMMAND") == SK_COMMAND for op in o.world.net)

    def test_p10_headless_no_agent_fails_preflight_with_remediation(self, run, src):
        """cron / tmux-before-login: no agent, no sshCommand."""
        p = Persona("P10 headless", ssh_key_works=False)
        o = run(p, "push", str(src), "alice/dst")
        assert o.code == 1
        assert not o.created
        assert "Permission denied / publickey" in o.err
        assert "git_protocol https" in o.err
        assert "core.sshCommand: not set" in o.err
        assert o.world.ops("push") == []


class TestSkKeyTouch:
    """#79: a touch proves only that the key answered then."""

    def test_missed_touch_on_push_is_retried_once(self, run, scoped):
        scope, repo = scoped
        p = dataclasses.replace(sk_persona("P1", str(scope)), script=("auto", "sk_missed"))
        o = run(p, "push", str(repo), "alice/dst")
        assert o.code == 0, o.err
        assert "Touch it when it blinks" in o.err
        assert [op.outcome for op in o.world.ops("push")] == ["sk_missed", "ok"]

    def test_two_missed_touches_on_push_exit_1_with_recovery(self, run, scoped):
        scope, repo = scoped
        p = dataclasses.replace(sk_persona("P1", str(scope)),
                                script=("auto", "sk_missed", "sk_missed"))
        o = run(p, "push", str(repo), "alice/dst")
        assert o.code == 1
        assert o.created            # the repo exists, empty: the documented trade-off
        assert "security key did not confirm" in o.err
        assert "git remote add origin git@github.com:alice/dst.git" in o.err
        assert "gh-safe-repo fix alice/dst --yes" in o.err

    def test_missed_touch_at_preflight_creates_nothing(self, run, scoped):
        scope, repo = scoped
        p = dataclasses.replace(sk_persona("P1", str(scope)), script=("sk_missed",))
        o = run(p, "push", str(repo), "alice/dst")
        assert o.code == 1
        assert not o.created
        assert "security key did not confirm" in o.err
        assert len(o.world.ops("ls-remote")) == 1   # preflight is not retried
        assert o.world.ops("push") == []

    def test_preflight_timeout_waiting_for_touch(self, run, scoped):
        scope, repo = scoped
        p = dataclasses.replace(sk_persona("P1", str(scope)), script=("timeout",))
        o = run(p, "push", str(repo), "alice/dst")
        assert o.code == 1
        assert not o.created
        assert "timed out after 45s" in o.err
        assert "security key" in o.err


# ---------------------------------------------------------------- P3

class TestP3HttpsHelper:
    def test_push_uses_helper_and_clean_url(self, run, src):
        o = run(P3, "push", str(src), "alice/dst")
        assert o.code == 0, o.err
        assert [op.url for op in o.world.net][1:] == ["https://github.com/alice/dst.git"]
        for op in o.world.net:
            assert op.env["GIT_TERMINAL_PROMPT"] == "0"
        assert origin_url(src) == "https://github.com/alice/dst.git"

    def test_stale_helper_fails_preflight_before_creating(self, run, src):
        p = dataclasses.replace(P3, helper_state="expired")
        o = run(p, "push", str(src), "alice/dst")
        assert o.code == 1
        assert not o.created
        assert "HTTPS authentication to github.com failed" in o.err
        assert "No credential.helper" not in o.err

    def test_no_helper_auto_falls_back_to_token(self, run, src):
        """`gh auth login` without `setup-git`: auto injects the gh token."""
        p = dataclasses.replace(P3, credential_helpers=())
        o = run(p, "push", str(src), "alice/dst")
        assert o.code == 0, o.err
        assert o.world.ops("push")[0].url.startswith("https://x-access-token:")
        assert origin_url(src) == "https://github.com/alice/dst.git"
        _tokens_absent(o, P3.gh_token)

    def test_no_helper_token_fallback_rejected_for_workflows(self, run, tmp_path):
        """The fallback's known cost: the OAuth token lacks `workflow`, so a
        push carrying .github/workflows is refused after the repo exists.
        There is no remediation hint for this today (scope doc, finding F4)."""
        repo = make_local_repo(tmp_path / "wf", workflows=True)
        p = dataclasses.replace(P3, credential_helpers=(), pushes_workflows=True)
        o = run(p, "push", str(repo), "alice/dst")
        assert o.code == 1
        assert o.created
        assert "without `workflow` scope" in o.err
        _tokens_absent(o, P3.gh_token)

    @pytest.mark.xfail(strict=True, reason=(
        "finding F6: preflight treats any stderr containing 'not found' as "
        "success, so '/bin/sh: gh: command not found' from a dead helper "
        "passes and the push fails after the repo is created"))
    def test_missing_helper_binary_fails_preflight(self, run, src):
        p = dataclasses.replace(P3, script=("helper_missing", "helper_missing"))
        o = run(p, "push", str(src), "alice/dst")
        assert o.code == 1
        assert not o.created

    def test_no_helper_user_creds_fails_preflight(self, run, src):
        p = dataclasses.replace(P3, credential_helpers=(), transport_mode="user_creds")
        o = run(p, "push", str(src), "alice/dst")
        assert o.code == 1
        assert not o.created
        assert "No credential.helper is configured" in o.err
        assert not any("x-access-token" in op.url for op in o.world.net)


# ---------------------------------------------------------------- P4 / P6

class TestP4Ci:
    def test_push_auto_uses_token_in_url(self, run, src):
        o = run(P4, "push", str(src), "alice/dst")
        assert o.code == 0, o.err
        push = o.world.ops("push")[0]
        assert push.url == f"https://x-access-token:{CI_TOKEN}@github.com/alice/dst.git"
        assert push.env["GIT_TERMINAL_PROMPT"] == "0"
        assert origin_url(src) == "https://github.com/alice/dst.git"
        _tokens_absent(o, CI_TOKEN)

    def test_copy_auto_uses_token_for_clone_and_push(self, run):
        o = run(P4, "copy", "alice/src", "alice/dst")
        assert o.code == 0, o.err
        assert all(f"x-access-token:{CI_TOKEN}@" in op.url for op in o.world.net)
        _tokens_absent(o, CI_TOKEN)

    def test_failed_copy_push_never_prints_token(self, run):
        """git echoes the token-bearing URL in its stderr; it must be redacted."""
        p = dataclasses.replace(P4, script=("auto", "auto", "auto", "auth_failed"))
        o = run(p, "copy", "alice/src", "alice/dst")
        assert o.code == 1
        assert "Code push failed" in o.err
        _tokens_absent(o, CI_TOKEN)

    def test_mode_token_skips_discovery(self, run, src):
        p = dataclasses.replace(P4, transport_mode="token")
        o = run(p, "push", str(src), "alice/dst")
        assert o.code == 0, o.err
        assert o.world.ran("gh", "config") == []
        assert o.world.ran("git", "config", "--get", "core.sshCommand") == []
        assert o.world.ran("git", "config", "--get-all", "credential.helper") == []
        _tokens_absent(o, CI_TOKEN)

    def test_mode_user_creds_fails_preflight(self, run, src):
        p = dataclasses.replace(P4, transport_mode="user_creds")
        o = run(p, "push", str(src), "alice/dst")
        assert o.code == 1
        assert not o.created
        assert "No credential.helper is configured" in o.err
        _tokens_absent(o, CI_TOKEN)

    @pytest.mark.xfail(strict=True, raises=FileNotFoundError, reason=(
        "finding F2: with no `gh` on PATH every API call raises an uncaught "
        "FileNotFoundError; the review's P4 setup says gh may be absent"))
    def test_no_gh_installed_fails_cleanly(self, run, src):
        p = dataclasses.replace(P4, gh_installed=False)
        o = run(p, "push", str(src), "alice/dst")
        assert o.code == 1
        assert "gh" in o.err


class TestP6AppToken:
    def test_installation_token_stops_at_owner_check(self, run, src):
        """GET /user is 403 for installation tokens, so push/copy stop
        before any git op (finding F3: the review expects P4 behaviour)."""
        o = run(P6, "push", str(src), "alice/dst")
        assert o.code == 1
        assert not o.created
        assert "Could not determine GitHub user" in o.err
        assert o.world.net == []
        _tokens_absent(o, APP_TOKEN)


# ---------------------------------------------------------------- P5

class TestP5Sso:
    def test_copy_from_sso_org_fails_at_scan_clone(self, run):
        """The scan clone of the org source hits SAML first, before the
        preflight and before creation. git's message is passed through;
        there is no SSO-specific remediation (finding F5)."""
        o = run(P5, "copy", "acme/src", "alice/dst")
        assert o.code == 1
        assert not o.created
        assert "Pre-flight scan failed" in o.err
        assert "SAML SSO" in o.err
        assert o.world.ops("ls-remote") == []

    def test_push_to_org_is_refused_by_owner_check(self, run, src):
        o = run(P5, "push", str(src), "acme/dst")
        assert o.code == 1
        assert "does not match authenticated user 'alice'" in o.err
        assert o.world.net == []

    def test_personal_push_unaffected(self, run, src):
        o = run(P5, "push", str(src), "alice/dst")
        assert o.code == 0, o.err


# ---------------------------------------------------------------- P12

class TestP12Gcm:
    def test_push_succeeds_without_prompts(self, run, src):
        o = run(P12, "push", str(src), "alice/dst")
        assert o.code == 0, o.err
        for op in o.world.net:
            assert op.env["GCM_INTERACTIVE"] == "false"
            assert op.env["GCM_GUI_PROMPT"] == "false"
            assert "x-access-token" not in op.url

    def test_expired_gcm_token_fails_preflight_without_browser(self, run, src):
        p = dataclasses.replace(P12, helper_state="expired")
        o = run(p, "push", str(src), "alice/dst")
        assert o.code == 1
        assert not o.created
        assert "HTTPS authentication to github.com failed" in o.err
        assert o.world.ops("push") == []


# ---------------------------------------------------------------- matrix-wide

def _success_cases(tmp_path):
    scope = tmp_path / "home" / "alt-account"
    return {
        "P1": (sk_persona("P1", str(scope)), scope),
        "P2": (P2, None),
        "P3": (P3, None),
        "P4": (P4, None),
        "P4-token": (dataclasses.replace(P4, transport_mode="token"), None),
        "P10-global": (sk_persona("P10", None), None),
        "P12": (P12, None),
    }


@pytest.mark.parametrize("key", ["P1", "P2", "P3", "P4", "P4-token", "P10-global", "P12"])
class TestInvariants:
    """Properties every working persona must keep. A fix for one persona that
    breaks one of these for another fails here."""

    def _push(self, run, tmp_path, key):
        persona, scope = _success_cases(tmp_path)[key]
        repo = make_local_repo((scope or tmp_path / "work") / "proj")
        return run(persona, "push", str(repo), "alice/dst"), persona, repo

    def test_preflight_matches_push_transport(self, run, tmp_path, key):
        """Gap 2: the preflight must use the push's scheme, credential path
        and transport env, and run exactly once, before the push."""
        o, _, _ = self._push(run, tmp_path, key)
        assert o.code == 0, o.err
        assert [op.verb for op in o.world.net] == ["ls-remote", "push"]
        pre, push = o.world.net
        assert pre.url.split("github.com")[0] == push.url.split("github.com")[0]
        for var in ("GIT_SSH_COMMAND", "GIT_TERMINAL_PROMPT", "GCM_INTERACTIVE",
                    "GCM_GUI_PROMPT"):
            assert pre.env.get(var) == push.env.get(var), var

    def test_no_token_in_output_or_git_config(self, run, tmp_path, key):
        o, persona, repo = self._push(run, tmp_path, key)
        assert o.code == 0, o.err
        _tokens_absent(o, *persona.tokens)
        assert "x-access-token" not in (origin_url(repo) or "")


# ---------------------------------------------------------------- findings

@pytest.mark.parametrize("login,source", [
    pytest.param("alice", "bob/src", id="other-owner"),
    pytest.param("AliceDev", "alicedev/src", id="owner-casing"),
])
def test_copy_pushes_to_destination_owner(run, login, source):
    p = dataclasses.replace(P2, login=login, gh_token="gho_x",
                            repos={source: {}})
    o = run(p, "copy", source, f"{login}/dst")
    assert o.code == 0, o.err
    assert o.world.ops("push")[0].url == f"git@github.com:{login}/dst.git"
