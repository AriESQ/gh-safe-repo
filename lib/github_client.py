"""
GitHub API client wrapping `gh api` via subprocess.
Auth priority: gh auth token > GITHUB_TOKEN env var > error.
Pattern adapted from gh-repo-settings/internal/infra/github/client.go.
"""

import json
import os
import re
import subprocess
import sys

from .errors import APIError, AuthError


class GitHubClient:
    def __init__(self, debug=False):
        self.debug = debug
        self._token = None
        self._use_gh = False
        self._authenticate()

    def _authenticate(self):
        # Try gh CLI first
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            token = result.stdout.strip()
            if token:
                self._token = token
                self._use_gh = True
                return

        # Fall back to GITHUB_TOKEN env var
        token = os.environ.get("GITHUB_TOKEN", "")
        if token:
            self._token = token
            return

        raise AuthError(
            "No GitHub credentials found. "
            "Run `gh auth login` or set the GITHUB_TOKEN environment variable."
        )

    def get_owner(self):
        """Return the authenticated user's login."""
        data = self.get_json("/user")
        return data["login"]

    def call_api(self, method, endpoint, body=None):
        """
        Call the GitHub API via `gh api`.
        Returns (status_code, response_text).
        Raises APIError on non-2xx responses (except 404, which callers handle).
        """
        cmd = ["gh", "api", "--method", method, endpoint]

        if body:
            cmd += ["--input", "-"]

        if self.debug:
            print(f"[debug] {method} {endpoint}", file=sys.stderr)
            if body:
                print(f"[debug] body: {json.dumps(body, indent=2)}", file=sys.stderr)

        result = subprocess.run(
            cmd,
            input=json.dumps(body) if body else None,
            capture_output=True,
            text=True,
            env={**os.environ, "GH_TOKEN": self._token},
        )

        status_code = self._parse_status(result.stderr)

        if self.debug and result.stderr:
            print(f"[debug] stderr: {result.stderr.strip()}", file=sys.stderr)

        return status_code, result.stdout

    def get_json(self, endpoint):
        """GET an endpoint and return parsed JSON. Raises APIError on failure."""
        status, text = self.call_api("GET", endpoint)
        if status and status >= 400:
            raise APIError(f"GET {endpoint} returned {status}", status_code=status)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise APIError(f"GET {endpoint} returned non-JSON response")

    def call_json(self, method, endpoint, body=None):
        """Call API with JSON body and return parsed response. Raises APIError on failure."""
        status, text = self.call_api(method, endpoint, body)
        if status and status >= 400:
            raise APIError(
                f"{method} {endpoint} returned {status}: {text.strip()}",
                status_code=status,
            )
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise APIError(f"{method} {endpoint} returned non-JSON response")

    @staticmethod
    def repo_path(owner, repo, suffix=""):
        """Build a repo-scoped API path."""
        base = f"/repos/{owner}/{repo}"
        if suffix:
            return f"{base}/{suffix.lstrip('/')}"
        return base

    def _parse_status(self, stderr):
        """Extract HTTP status code from gh stderr output."""
        if not stderr:
            return None
        match = re.search(r"HTTP (\d{3})", stderr)
        if match:
            return int(match.group(1))
        # gh api exits non-zero and includes status in stderr differently
        match = re.search(r"(\d{3})", stderr)
        if match:
            return int(match.group(1))
        return None
