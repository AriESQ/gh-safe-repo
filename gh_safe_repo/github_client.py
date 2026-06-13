"""
GitHub API client wrapping `gh api` via subprocess.
Auth priority: GITHUB_TOKEN env var > gh auth token > error.

Git transport (push/clone/preflight) lives in git_transport.GitTransport.
Callers must assign `self.transport` before invoking copy_repo / push_local /
clone_for_scan. See commands/create.py for the wiring.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

from .errors import APIError, AuthError
from .git_transport import GitTransport


class GitHubClient:
    def __init__(self, debug=False):
        self.debug = debug
        self._token = None
        self._use_gh = False
        self._user_data = None
        self._repo_cache = {}
        self.transport: Optional[GitTransport] = None
        self._authenticate()

    @property
    def token(self) -> str:
        """The API token (GITHUB_TOKEN env or gh auth). For transport wiring."""
        return self._token

    def _require_transport(self) -> GitTransport:
        if self.transport is None:
            raise RuntimeError(
                "GitHubClient.transport is not set. "
                "Assign a GitTransport before calling git operations."
            )
        return self.transport

    def _authenticate(self):
        # GITHUB_TOKEN env var takes priority — lets callers target a specific
        # account without switching the active gh session.
        token = os.environ.get("GITHUB_TOKEN", "")
        if token:
            self._token = token
            return

        # Fall back to the active gh CLI session.
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

        raise AuthError(
            "No GitHub credentials found. "
            "Run `gh auth login` or set the GITHUB_TOKEN environment variable."
        )

    def _get_user(self) -> dict:
        """Fetch /user once and cache; returns the raw response dict."""
        if self._user_data is None:
            self._user_data = self.get_json("/user")
        return self._user_data

    def get_repo_data(self, owner: str, repo: str) -> dict:
        """Fetch /repos/{owner}/{repo} once and cache; returns the raw response dict."""
        key = (owner, repo)
        if key not in self._repo_cache:
            self._repo_cache[key] = self.get_json(self.repo_path(owner, repo))
        return self._repo_cache[key]

    def get_owner(self):
        """Return the authenticated user's login."""
        return self._get_user()["login"]

    def get_plan_name(self) -> str:
        """Return the authenticated user's GitHub plan ('free', 'pro', etc.)."""
        try:
            data = self._get_user()
            return data.get("plan", {}).get("name", "free") or "free"
        except APIError:
            return "free"

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
        # gh api writes nothing to stderr on success; infer 200 from exit code 0.
        if status_code is None and result.returncode == 0:
            status_code = 200

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

    def get_default_branch(self, owner: str, repo: str):
        """Return the default branch name for an existing repo, or None on failure."""
        try:
            return self.get_repo_data(owner, repo).get("default_branch")
        except (APIError, ValueError):
            return None

    def copy_repo(self, owner, source_repo, dest_repo):
        """
        Mirror-clone source_repo and push all refs to dest_repo.
        Both repos must belong to owner.
        Uses the user's git credentials via self.transport.
        """
        transport = self._require_transport()
        source_url = transport.remote_url(owner, source_repo)
        dest_url = transport.remote_url(owner, dest_repo)

        with tempfile.TemporaryDirectory() as tmpdir:
            mirror_path = os.path.join(tmpdir, "mirror")

            try:
                transport.run(["git", "clone", "--mirror", source_url, mirror_path])
            except subprocess.CalledProcessError as e:
                raise APIError(transport.redact(
                    f"git clone failed for {source_url}: {(e.stderr or '').strip()}"
                ))

            transport.run(
                ["git", "-C", mirror_path, "remote", "set-url", "--push", "origin", dest_url],
            )

            try:
                transport.run(["git", "-C", mirror_path, "push", "--mirror", "origin"])
            except subprocess.CalledProcessError as e:
                raise APIError(transport.redact(
                    f"git push failed to {dest_url}: {(e.stderr or '').strip()}"
                ))

    def push_local(self, local_path: str, owner: str, dest_repo: str) -> None:
        """
        Push a local directory's code to a new empty GitHub repo.
        If local_path is a git repo, its full history is pushed.
        Otherwise files are staged in a fresh repo and pushed as an initial commit.
        """
        transport = self._require_transport()
        dest_url = transport.remote_url(owner, dest_repo)
        is_git_repo = os.path.isdir(os.path.join(local_path, ".git"))

        with tempfile.TemporaryDirectory() as tmpdir:
            work_path = os.path.join(tmpdir, "work")

            if is_git_repo:
                try:
                    transport.run(["git", "clone", local_path, work_path])
                except subprocess.CalledProcessError as e:
                    raise APIError(f"git clone (local) failed: {(e.stderr or '').strip()}")
            else:
                # Not a git repo — copy files and create an initial commit.
                # These local-only ops don't need transport env; using it for
                # consistency keeps a single subprocess path.
                shutil.copytree(local_path, work_path)
                try:
                    transport.run(["git", "init", work_path])
                    transport.run(["git", "-C", work_path, "add", "-A"])
                    staged = transport.run(
                        ["git", "-C", work_path, "diff", "--cached", "--quiet"],
                        check=False,
                    )
                    if staged.returncode != 0:  # has staged changes
                        transport.run(
                            ["git", "-C", work_path, "commit", "-m", "Initial commit"],
                        )
                    else:
                        # Empty directory — nothing to push
                        return
                except subprocess.CalledProcessError as e:
                    raise APIError(
                        f"Failed to create initial git commit: {(e.stderr or '').strip()}"
                    )

            try:
                # git clone sets up origin pointing to local_path; update it.
                # For fresh git init there is no origin yet; add it.
                if is_git_repo:
                    transport.run(
                        ["git", "-C", work_path, "remote", "set-url", "origin", dest_url],
                    )
                else:
                    transport.run(
                        ["git", "-C", work_path, "remote", "add", "origin", dest_url],
                    )
                transport.run(["git", "-C", work_path, "push", "origin", "--all"])
                transport.run(["git", "-C", work_path, "push", "origin", "--tags"])
            except subprocess.CalledProcessError as e:
                raise APIError(transport.redact(
                    f"git push failed to {dest_url}: {(e.stderr or '').strip()}"
                ))

        # Wire up the original local repo to the newly created remote so
        # future `git push` / `git pull` work without extra configuration.
        # Local-only ops; safe to use the transport for the env consistency.
        # persistent_url, not dest_url: a token-injected URL must never be
        # written into the user's long-lived .git/config.
        if is_git_repo:
            try:
                transport.run(
                    ["git", "-C", local_path, "remote", "add", "origin",
                     transport.persistent_url(owner, dest_repo)],
                )
                result = transport.run(
                    ["git", "-C", local_path, "symbolic-ref", "--short", "HEAD"],
                    check=False,
                )
                if result.returncode == 0:
                    branch = result.stdout.strip()
                    transport.run(
                        ["git", "-C", local_path, "branch", "--set-upstream-to",
                         f"origin/{branch}", branch],
                        check=False,
                    )
            except subprocess.CalledProcessError:
                pass  # non-fatal: remote wiring is a convenience

    def clone_for_scan(self, owner: str, repo: str, dest_path: str) -> None:
        """Full-clone repo into dest_path for pre-flight scanning.

        A full clone (no --depth) is required so truffleHog can scan the
        complete git history for secrets, not just the working-tree snapshot.
        """
        transport = self._require_transport()
        clone_url = transport.remote_url(owner, repo)
        try:
            transport.run(["git", "clone", clone_url, dest_path])
        except subprocess.CalledProcessError as e:
            raise APIError(transport.redact(
                f"git clone (scan) failed for {clone_url}: {(e.stderr or '').strip()}"
            ))

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
