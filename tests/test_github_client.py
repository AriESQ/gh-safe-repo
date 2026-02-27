"""Tests for github_client.py — all subprocess calls are mocked."""

import json
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest
from gh_safe_repo.errors import APIError, AuthError
from gh_safe_repo.github_client import GitHubClient


def make_completed_process(stdout="", stderr="", returncode=0):
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


class TestAuthentication:
    def test_uses_gh_cli_token(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(stdout="ghp_token123\n")
            client = GitHubClient()
            assert client._use_gh is True
            assert client._token == "ghp_token123"

    def test_falls_back_to_env_var(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "env_token_abc")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(returncode=1, stdout="")
            client = GitHubClient()
            assert client._use_gh is False
            assert client._token == "env_token_abc"

    def test_raises_auth_error_with_no_credentials(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(returncode=1, stdout="")
            with pytest.raises(AuthError):
                GitHubClient()


class TestCallApi:
    def _make_client(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(stdout="ghp_token\n")
            return GitHubClient()

    def test_get_json_returns_parsed_data(self):
        client = self._make_client()
        user_data = {"login": "testuser", "id": 12345}
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                stdout=json.dumps(user_data), stderr=""
            )
            result = client.get_json("/user")
        assert result["login"] == "testuser"

    def test_get_json_raises_on_4xx(self):
        client = self._make_client()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                stdout="", stderr="HTTP 404 Not Found"
            )
            with pytest.raises(APIError) as exc_info:
                client.get_json("/repos/owner/missing")
            assert exc_info.value.status_code == 404

    def test_call_json_with_body(self):
        client = self._make_client()
        response_data = {"id": 1, "name": "my-repo"}
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                stdout=json.dumps(response_data), stderr=""
            )
            result = client.call_json("POST", "/user/repos", {"name": "my-repo"})
        assert result["name"] == "my-repo"
        # Verify body was passed via stdin
        call_args = mock_run.call_args
        assert call_args.kwargs["input"] == json.dumps({"name": "my-repo"})

    def test_call_json_raises_on_error(self):
        client = self._make_client()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                stdout='{"message":"Unprocessable Entity"}',
                stderr="HTTP 422",
            )
            with pytest.raises(APIError) as exc_info:
                client.call_json("POST", "/user/repos", {"name": "bad"})
            assert exc_info.value.status_code == 422


class TestHelpers:
    def _make_client(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(stdout="ghp_token\n")
            return GitHubClient()

    def test_repo_path_no_suffix(self):
        client = self._make_client()
        assert client.repo_path("alice", "myrepo") == "/repos/alice/myrepo"

    def test_repo_path_with_suffix(self):
        client = self._make_client()
        assert (
            client.repo_path("alice", "myrepo", "actions/permissions")
            == "/repos/alice/myrepo/actions/permissions"
        )

    def test_repo_path_with_leading_slash_suffix(self):
        client = self._make_client()
        assert (
            client.repo_path("alice", "myrepo", "/contents/SECURITY.md")
            == "/repos/alice/myrepo/contents/SECURITY.md"
        )

    def test_parse_status_from_stderr(self):
        client = self._make_client()
        assert client._parse_status("HTTP 404 Not Found") == 404
        assert client._parse_status("HTTP 200 OK") == 200
        assert client._parse_status("") is None


class TestCopyRepo:
    def _make_client(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(stdout="ghp_token\n")
            client = GitHubClient()
            client._token = "ghp_testtoken"
            return client

    def test_copy_repo_calls_git_clone_mirror(self):
        client = self._make_client()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process()
            client.copy_repo("alice", "private-repo", "public-repo")

        # First subprocess.run call should be git clone --mirror
        clone_call = mock_run.call_args_list[0]
        cmd = clone_call.args[0]
        assert cmd[0] == "git"
        assert "--mirror" in cmd
        assert any("private-repo.git" in arg for arg in cmd)
        assert any("x-access-token:" in arg for arg in cmd)

    def test_copy_repo_calls_git_push_mirror(self):
        client = self._make_client()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process()
            client.copy_repo("alice", "private-repo", "public-repo")

        # Last subprocess.run call should be git push --mirror
        push_call = mock_run.call_args_list[-1]
        cmd = push_call.args[0]
        assert cmd[0] == "git"
        assert "--mirror" in cmd
        assert "push" in cmd

    def test_copy_repo_sets_push_url_to_dest(self):
        client = self._make_client()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process()
            client.copy_repo("alice", "private-repo", "public-repo")

        # Middle call should be git remote set-url --push with dest URL
        set_url_call = mock_run.call_args_list[1]
        cmd = set_url_call.args[0]
        assert "set-url" in cmd
        assert any("public-repo.git" in arg for arg in cmd)

    def test_copy_repo_raises_api_error_on_clone_failure(self):
        client = self._make_client()
        clone_error = subprocess.CalledProcessError(128, "git", stderr="fatal: repo not found")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = clone_error
            with pytest.raises(APIError) as exc_info:
                client.copy_repo("alice", "private-repo", "public-repo")
        assert "clone" in str(exc_info.value).lower()

    def test_copy_repo_raises_api_error_on_push_failure(self):
        client = self._make_client()
        success = make_completed_process()
        push_error = subprocess.CalledProcessError(1, "git", stderr="error: push rejected")

        with patch("subprocess.run") as mock_run:
            # clone and set-url succeed, push fails
            mock_run.side_effect = [success, success, push_error]
            with pytest.raises(APIError) as exc_info:
                client.copy_repo("alice", "private-repo", "public-repo")
        assert "push" in str(exc_info.value).lower()


class TestCloneForScan:
    def _make_client(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(stdout="ghp_token\n")
            client = GitHubClient()
            client._token = "ghp_testtoken"
            return client

    def test_clone_for_scan_is_full_clone(self):
        # A full clone (no --depth) is required so truffleHog can walk the
        # entire git history, not just the HEAD working-tree snapshot.
        client = self._make_client()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process()
            client.clone_for_scan("alice", "private-repo", "/tmp/scan_dir")
        cmd = mock_run.call_args.args[0]
        assert "--depth=1" not in cmd

    def test_clone_for_scan_includes_dest_path(self):
        client = self._make_client()
        dest = "/tmp/my_scan_dir"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process()
            client.clone_for_scan("alice", "private-repo", dest)
        cmd = mock_run.call_args.args[0]
        assert dest in cmd

    def test_clone_for_scan_raises_api_error_on_failure(self):
        client = self._make_client()
        clone_error = subprocess.CalledProcessError(128, "git", stderr="fatal: repo not found")
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = clone_error
            with pytest.raises(APIError) as exc_info:
                client.clone_for_scan("alice", "private-repo", "/tmp/scan")
        assert "clone" in str(exc_info.value).lower()


class TestGetPlanName:
    def _make_client(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(stdout="ghp_token\n")
            return GitHubClient()

    def test_returns_plan_name_from_user_endpoint(self):
        client = self._make_client()
        user_data = {"login": "alice", "plan": {"name": "pro"}}
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                stdout=json.dumps(user_data), stderr=""
            )
            result = client.get_plan_name()
        assert result == "pro"

    def test_returns_free_when_plan_key_missing(self):
        client = self._make_client()
        user_data = {"login": "alice"}
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                stdout=json.dumps(user_data), stderr=""
            )
            result = client.get_plan_name()
        assert result == "free"

    def test_returns_free_when_plan_name_missing(self):
        client = self._make_client()
        user_data = {"login": "alice", "plan": {}}
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_completed_process(
                stdout=json.dumps(user_data), stderr=""
            )
            result = client.get_plan_name()
        assert result == "free"
