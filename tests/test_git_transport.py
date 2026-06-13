"""Tests for git_transport.py — all subprocess calls are mocked."""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from gh_safe_repo.errors import AuthError, ConfigError
from gh_safe_repo.git_transport import (
    GitTransport,
    discover_transport,
    PREFLIGHT_REPO,
)


def make_completed_process(stdout="", stderr="", returncode=0):
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


def make_transport(
    protocol="ssh",
    source_dir="/tmp/x",
    ssh_command=None,
    has_credential_helper=False,
    host="github.com",
    token=None,
):
    return GitTransport(
        protocol=protocol,
        source_dir=source_dir,
        ssh_command=ssh_command,
        has_credential_helper=has_credential_helper,
        host=host,
        token=token,
    )


class TestRemoteUrl:
    def test_ssh_url(self):
        t = make_transport(protocol="ssh")
        assert t.remote_url("alice", "myrepo") == "git@github.com:alice/myrepo.git"

    def test_https_url(self):
        t = make_transport(protocol="https")
        assert t.remote_url("alice", "myrepo") == "https://github.com/alice/myrepo.git"

    def test_no_token_in_url(self):
        for proto in ("ssh", "https"):
            url = make_transport(protocol=proto).remote_url("alice", "myrepo")
            assert "x-access-token" not in url

    def test_preflight_url_uses_known_nonexistent_repo(self):
        t = make_transport(protocol="ssh")
        assert PREFLIGHT_REPO in t.preflight_url()
        assert t.preflight_url() == f"git@github.com:{PREFLIGHT_REPO}.git"


class TestEnv:
    def test_ssh_propagates_ssh_command(self):
        t = make_transport(
            protocol="ssh",
            ssh_command="ssh -i /key -o IdentitiesOnly=yes",
        )
        env = t.env()
        assert env["GIT_SSH_COMMAND"] == "ssh -i /key -o IdentitiesOnly=yes"

    def test_ssh_omits_ssh_command_when_unset(self):
        t = make_transport(protocol="ssh", ssh_command=None)
        env = t.env()
        assert "GIT_SSH_COMMAND" not in env

    def test_https_disables_terminal_and_gcm_prompts(self):
        """HTTPS preflight must never hang on a username prompt or browser
        launch — fixes P4 (CI/headless) and P12 (GCM hang)."""
        t = make_transport(protocol="https")
        env = t.env()
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GCM_INTERACTIVE"] == "false"
        assert env["GCM_GUI_PROMPT"] == "false"

    def test_ssh_does_not_set_https_only_vars(self):
        t = make_transport(protocol="ssh")
        env = t.env()
        assert "GIT_TERMINAL_PROMPT" not in env
        assert "GCM_INTERACTIVE" not in env

    def test_inherits_parent_env(self):
        with patch.dict(os.environ, {"PATH": "/sentinel"}):
            env = make_transport().env()
            assert env["PATH"] == "/sentinel"


class TestRun:
    def test_passes_env_and_cmd(self):
        t = make_transport(protocol="ssh", ssh_command="custom-ssh")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process()
            t.run(["git", "clone", "x"])
        kwargs = mock_run.call_args.kwargs
        assert kwargs["env"]["GIT_SSH_COMMAND"] == "custom-ssh"
        assert mock_run.call_args.args[0] == ["git", "clone", "x"]

    def test_cwd_defaults_to_none_not_source_dir(self):
        """Critical: the push runs in a tempdir, not source_dir. The transport
        must NOT default cwd to source_dir or the push subprocess won't see
        its working tree."""
        t = make_transport(source_dir="/source")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process()
            t.run(["git", "status"])
        assert mock_run.call_args.kwargs["cwd"] is None

    def test_cwd_override_passes_through(self):
        t = make_transport()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process()
            t.run(["git", "status"], cwd="/elsewhere")
        assert mock_run.call_args.kwargs["cwd"] == "/elsewhere"


class TestPreflightSsh:
    def test_repository_not_found_is_success(self):
        t = make_transport(protocol="ssh")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                returncode=128,
                stderr="ERROR: Repository not found.\nfatal: Could not read from remote",
            )
            t.preflight()  # should not raise

    def test_permission_denied_raises_with_remediation(self):
        t = make_transport(protocol="ssh")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                returncode=128,
                stderr="git@github.com: Permission denied (publickey).",
            )
            with pytest.raises(AuthError) as exc_info:
                t.preflight()
        msg = str(exc_info.value)
        assert "SSH" in msg
        assert "git_protocol https" in msg

    def test_host_key_failure_specific_hint(self):
        t = make_transport(protocol="ssh")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                returncode=128,
                stderr="Host key verification failed.",
            )
            with pytest.raises(AuthError) as exc_info:
                t.preflight()
        assert "host key" in str(exc_info.value).lower()

    def test_propagates_ssh_command_into_preflight_subprocess(self):
        """Fixes P1: the preflight must use the same ssh_command that the
        real push will use, not the system default ssh."""
        t = make_transport(protocol="ssh", ssh_command="ssh -i /yubikey")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                returncode=128, stderr="ERROR: Repository not found.",
            )
            t.preflight()
        assert mock_run.call_args.kwargs["env"]["GIT_SSH_COMMAND"] == "ssh -i /yubikey"

    def test_timeout_raises_auth_error(self):
        t = make_transport(protocol="ssh")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 15)):
            with pytest.raises(AuthError) as exc_info:
                t.preflight()
        assert "timed out" in str(exc_info.value).lower()

    def test_git_missing_raises_auth_error(self):
        t = make_transport(protocol="ssh")
        with patch("subprocess.run", side_effect=FileNotFoundError("git")):
            with pytest.raises(AuthError) as exc_info:
                t.preflight()
        assert "git" in str(exc_info.value).lower()


class TestPreflightHttps:
    def test_repository_not_found_is_success(self):
        t = make_transport(protocol="https")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                returncode=128,
                stderr="remote: Repository not found.\nfatal: repository .. not found",
            )
            t.preflight()  # should not raise

    def test_terminal_prompts_disabled_means_no_helper(self):
        """P4: CI user with no credential helper. Preflight must catch this
        before the API call creates an empty repo on GitHub."""
        t = make_transport(protocol="https", has_credential_helper=False)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                returncode=128,
                stderr="fatal: could not read Username for 'https://github.com': "
                       "terminal prompts disabled",
            )
            with pytest.raises(AuthError) as exc_info:
                t.preflight()
        msg = str(exc_info.value)
        assert "HTTPS" in msg
        assert "no credential.helper" in msg.lower()
        assert "gh auth setup-git" in msg

    def test_terminal_prompts_disabled_with_helper_present(self):
        """If a helper IS configured but auth still failed, the hint is to
        re-authenticate, not to install a helper."""
        t = make_transport(protocol="https", has_credential_helper=True)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                returncode=128,
                stderr="fatal: Authentication failed for 'https://github.com'",
            )
            with pytest.raises(AuthError) as exc_info:
                t.preflight()
        msg = str(exc_info.value)
        assert "HTTPS" in msg
        # No-helper hint must NOT appear when a helper is configured
        assert "no credential.helper" not in msg.lower()

    def test_disables_browser_prompts_in_subprocess_env(self):
        """Fixes P12: GCM must not launch a browser during preflight."""
        t = make_transport(protocol="https")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                returncode=128, stderr="remote: Repository not found.",
            )
            t.preflight()
        env = mock_run.call_args.kwargs["env"]
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GCM_INTERACTIVE"] == "false"
        assert env["GCM_GUI_PROMPT"] == "false"


class TestTokenInjection:
    def test_token_injected_into_https_url(self):
        t = make_transport(protocol="https", token="ghp_secret123")
        assert (
            t.remote_url("alice", "myrepo")
            == "https://x-access-token:ghp_secret123@github.com/alice/myrepo.git"
        )

    def test_token_never_in_ssh_url(self):
        t = make_transport(protocol="ssh", token="ghp_secret123")
        assert "ghp_secret123" not in t.remote_url("alice", "myrepo")

    def test_persistent_url_never_embeds_token(self):
        """The remote written into the user's long-lived .git/config must be
        clean even when the push itself used token injection."""
        t = make_transport(protocol="https", token="ghp_secret123")
        url = t.persistent_url("alice", "myrepo")
        assert url == "https://github.com/alice/myrepo.git"
        assert "ghp_secret123" not in url

    def test_redact_strips_token(self):
        t = make_transport(protocol="https", token="ghp_secret123")
        assert t.redact("push to https://x-access-token:ghp_secret123@x failed") \
            == "push to https://x-access-token:***@x failed"

    def test_redact_noop_without_token(self):
        t = make_transport(protocol="https")
        assert t.redact("hello ghp_secret123") == "hello ghp_secret123"

    def test_debug_output_redacts_token(self, capsys):
        t = GitTransport(
            protocol="https", source_dir="/tmp/x",
            token="ghp_secret123", debug=True,
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process()
            t.run(["git", "push", t.remote_url("alice", "myrepo")])
        captured = capsys.readouterr()
        assert "ghp_secret123" not in captured.err
        assert "***" in captured.err

    def test_preflight_error_never_contains_token(self):
        t = make_transport(protocol="https", token="ghp_secret123")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                returncode=128,
                stderr="fatal: Authentication failed for "
                       "'https://x-access-token:ghp_secret123@github.com/x.git'",
            )
            with pytest.raises(AuthError) as exc_info:
                t.preflight()
        assert "ghp_secret123" not in str(exc_info.value)

    def test_preflight_token_failure_names_scopes(self):
        """P4/P6 friction is trial-and-error token scopes — the error must
        name them explicitly."""
        t = make_transport(protocol="https", token="ghp_secret123")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                returncode=128,
                stderr="fatal: Authentication failed for 'https://github.com/x.git'",
            )
            with pytest.raises(AuthError) as exc_info:
                t.preflight()
        msg = str(exc_info.value)
        assert "token" in msg.lower()
        assert "`repo`" in msg
        assert "`workflow`" in msg

    def test_preflight_token_not_found_is_still_success(self):
        t = make_transport(protocol="https", token="ghp_secret123")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                returncode=128, stderr="remote: Repository not found.",
            )
            t.preflight()  # should not raise


class TestDiscoverTransportMode:
    @staticmethod
    def _gh_https_no_helper(cmd, **kwargs):
        if cmd[:3] == ["gh", "config", "get"]:
            return make_completed_process(stdout="https\n")
        return make_completed_process(returncode=1)

    @staticmethod
    def _gh_ssh(cmd, **kwargs):
        if cmd[:3] == ["gh", "config", "get"]:
            return make_completed_process(stdout="ssh\n")
        return make_completed_process(returncode=1)

    def test_invalid_mode_raises_config_error(self):
        with pytest.raises(ConfigError) as exc_info:
            discover_transport("/tmp/x", mode="yolo", token="tok")
        assert "yolo" in str(exc_info.value)
        assert "[git_transport]" in str(exc_info.value)

    def test_token_mode_requires_token(self):
        with pytest.raises(AuthError) as exc_info:
            discover_transport("/tmp/x", mode="token", token=None)
        assert "GITHUB_TOKEN" in str(exc_info.value)

    def test_token_mode_forces_https_and_skips_discovery(self):
        """mode=token needs no gh/git config inspection at all."""
        with patch("subprocess.run") as mock_run:
            t = discover_transport("/tmp/x", mode="token", token="tok")
        assert t.protocol == "https"
        assert t.token == "tok"
        mock_run.assert_not_called()

    def test_user_creds_mode_never_attaches_token(self):
        """P2 guard: pinning user_creds guarantees the workflow-scope-free
        push even when HTTPS has no helper and a token is available."""
        with patch("subprocess.run", side_effect=self._gh_https_no_helper):
            t = discover_transport("/tmp/x", mode="user_creds", token="tok")
        assert t.token is None

    def test_auto_falls_back_to_token_when_https_no_helper(self):
        """P4/P6: CI with only GITHUB_TOKEN gets a push path."""
        with patch("subprocess.run", side_effect=self._gh_https_no_helper):
            t = discover_transport("/tmp/x", mode="auto", token="tok")
        assert t.protocol == "https"
        assert t.token == "tok"

    def test_auto_prefers_credential_helper_when_present(self):
        """P3/P12: a configured helper means the user has their own path."""
        def side_effect(cmd, **kwargs):
            if cmd[:3] == ["gh", "config", "get"]:
                return make_completed_process(stdout="https\n")
            if "credential.helper" in cmd:
                return make_completed_process(stdout="manager\n")
            return make_completed_process(returncode=1)

        with patch("subprocess.run", side_effect=side_effect):
            t = discover_transport("/tmp/x", mode="auto", token="tok")
        assert t.token is None
        assert t.has_credential_helper is True

    def test_auto_never_injects_token_for_ssh(self):
        """P1/P2/P10 guard: SSH users keep their own credentials."""
        with patch("subprocess.run", side_effect=self._gh_ssh):
            t = discover_transport("/tmp/x", mode="auto", token="tok")
        assert t.protocol == "ssh"
        assert t.token is None

    def test_auto_without_token_behaves_as_before(self):
        with patch("subprocess.run", side_effect=self._gh_https_no_helper):
            t = discover_transport("/tmp/x", mode="auto", token=None)
        assert t.token is None

    def test_mode_is_case_insensitive_and_stripped(self):
        with patch("subprocess.run", side_effect=self._gh_ssh):
            t = discover_transport("/tmp/x", mode=" AUTO ", token=None)
        assert t.protocol == "ssh"

    def test_empty_token_treated_as_absent(self):
        with patch("subprocess.run", side_effect=self._gh_https_no_helper):
            t = discover_transport("/tmp/x", mode="auto", token="")
        assert t.token is None


class TestDiscoverTransport:
    def test_reads_protocol_from_gh_config_host_specific_first(self):
        calls = []

        def side_effect(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ["gh", "config", "get"] and "-h" in cmd:
                return make_completed_process(stdout="ssh\n")
            if cmd[:3] == ["gh", "config", "get"]:
                return make_completed_process(stdout="https\n")
            # git config lookups (core.sshCommand, credential.helper) — empty
            return make_completed_process(returncode=1)

        with patch("subprocess.run", side_effect=side_effect):
            t = discover_transport("/tmp/some-source")

        assert t.protocol == "ssh"
        assert calls[0][:3] == ["gh", "config", "get"]
        assert "-h" in calls[0]

    def test_falls_back_to_global_when_host_unset(self):
        def side_effect(cmd, **kwargs):
            if "-h" in cmd:
                return make_completed_process(returncode=1)
            if cmd[:3] == ["gh", "config", "get"]:
                return make_completed_process(stdout="ssh\n")
            return make_completed_process(returncode=1)

        with patch("subprocess.run", side_effect=side_effect):
            t = discover_transport("/tmp/x")
        assert t.protocol == "ssh"

    def test_defaults_to_https_when_gh_unavailable(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("gh")):
            t = discover_transport("/tmp/x")
        assert t.protocol == "https"

    def test_captures_ssh_command_from_source_dir(self):
        """Fixes P1: per-directory core.sshCommand must be captured at
        discovery time so it follows the subprocess into the temp dir."""
        def side_effect(cmd, **kwargs):
            if cmd[:3] == ["gh", "config", "get"]:
                return make_completed_process(stdout="ssh\n")
            if "core.sshCommand" in cmd:
                # `git -C <source> config --get core.sshCommand`
                assert "/tmp/yubikey-dir" in cmd
                return make_completed_process(stdout="ssh -i /yk -o IdentitiesOnly=yes\n")
            return make_completed_process(returncode=1)

        with patch("subprocess.run", side_effect=side_effect):
            t = discover_transport("/tmp/yubikey-dir")
        assert t.ssh_command == "ssh -i /yk -o IdentitiesOnly=yes"

    def test_https_inspects_credential_helper_presence(self):
        def side_effect(cmd, **kwargs):
            if cmd[:3] == ["gh", "config", "get"]:
                return make_completed_process(stdout="https\n")
            if "credential.helper" in cmd:
                return make_completed_process(stdout="manager\n")
            return make_completed_process(returncode=1)

        with patch("subprocess.run", side_effect=side_effect):
            t = discover_transport("/tmp/x")
        assert t.has_credential_helper is True

    def test_https_no_helper_recorded_when_unset(self):
        def side_effect(cmd, **kwargs):
            if cmd[:3] == ["gh", "config", "get"]:
                return make_completed_process(stdout="https\n")
            return make_completed_process(returncode=1)

        with patch("subprocess.run", side_effect=side_effect):
            t = discover_transport("/tmp/x")
        assert t.has_credential_helper is False

    def test_ssh_does_not_query_credential_helper(self):
        """credential.helper is only relevant for HTTPS; don't waste a
        subprocess on it for SSH transports."""
        seen = []

        def side_effect(cmd, **kwargs):
            seen.append(cmd)
            if cmd[:3] == ["gh", "config", "get"]:
                return make_completed_process(stdout="ssh\n")
            return make_completed_process(returncode=1)

        with patch("subprocess.run", side_effect=side_effect):
            discover_transport("/tmp/x")
        assert not any("credential.helper" in c for c in seen)

    def test_source_dir_is_absolutized(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(returncode=1)
            t = discover_transport(".")
        assert os.path.isabs(t.source_dir)
