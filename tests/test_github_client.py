"""Tests for github_client.py — all subprocess calls are mocked."""

import json
from unittest.mock import MagicMock, patch

import pytest
from lib.errors import APIError, AuthError
from lib.github_client import GitHubClient


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
