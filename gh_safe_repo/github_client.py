"""
GitHub API client wrapping `gh api` via subprocess.
Auth priority: gh auth token > GITHUB_TOKEN env var > error.
Pattern adapted from gh-repo-settings/internal/infra/github/client.go.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from .errors import APIError, AuthError


class GitHubClient:
    def __init__(self, debug=False):
        self.debug = debug
        self._token = None
        self._use_gh = False
        self._user_data = None
        self._repo_cache = {}
        self._git_protocol = None
        self._authenticate()

    def _get_git_protocol(self) -> str:
        """Return 'ssh' or 'https' (default 'https'), cached."""
        if self._git_protocol is not None:
            return self._git_protocol
        try:
            result = subprocess.run(
                ["gh", "config", "get", "git_protocol"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                value = result.stdout.strip().lower()
                if value in ("ssh", "https"):
                    self._git_protocol = value
                    return value
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        self._git_protocol = "https"
        return "https"

    def git_remote_url(self, owner: str, repo: str) -> str:
        """Build a git remote URL using the user's configured protocol.

        Uses the user's own credentials (SSH key or HTTPS credential helper)
        rather than injecting the OAuth token, which avoids OAuth-App scope
        restrictions (e.g. workflow scope) on `git push`.
        """
        if self._get_git_protocol() == "ssh":
            return f"git@github.com:{owner}/{repo}.git"
        return f"https://github.com/{owner}/{repo}.git"

    def verify_git_credentials(self) -> None:
        """Pre-flight check that the user can authenticate to github.com via git.

        For SSH, runs `ssh -T git@github.com` (GitHub returns exit 1 with a
        success banner on success; 255 on auth failure). For HTTPS, returns
        without probing — credential-helper failures surface clearly on push.
        Raises AuthError on a known SSH failure.
        """
        if self._get_git_protocol() != "ssh":
            return
        try:
            result = subprocess.run(
                ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                 "git@github.com"],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            raise AuthError(
                f"Could not run `ssh` to verify GitHub credentials: {e}. "
                "Set `gh config set git_protocol https` or install OpenSSH."
            )
        # Exit 1 = authenticated (GitHub denies shell access). Anything else fails.
        if result.returncode != 1 or "successfully authenticated" not in (result.stderr or ""):
            stderr = (result.stderr or "").strip()
            raise AuthError(
                "SSH authentication to git@github.com failed. "
                "Add your SSH key to GitHub and load it into ssh-agent, "
                "or switch to HTTPS via `gh config set git_protocol https`.\n"
                f"  ssh said: {stderr}"
            )

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
        Uses the user's git credentials (SSH or credential helper).
        """
        source_url = self.git_remote_url(owner, source_repo)
        dest_url = self.git_remote_url(owner, dest_repo)
        source_display = source_url
        dest_display = dest_url

        with tempfile.TemporaryDirectory() as tmpdir:
            mirror_path = os.path.join(tmpdir, "mirror")

            if self.debug:
                print(f"[debug] git clone --mirror {source_display}", file=sys.stderr)

            try:
                subprocess.run(
                    ["git", "clone", "--mirror", source_url, mirror_path],
                    check=True,
                    capture_output=not self.debug,
                    text=True,
                )
            except subprocess.CalledProcessError as e:
                raise APIError(
                    f"git clone failed for {source_display}: {(e.stderr or '').strip()}"
                )

            if self.debug:
                print(f"[debug] git remote set-url --push origin {dest_display}", file=sys.stderr)

            subprocess.run(
                ["git", "-C", mirror_path, "remote", "set-url", "--push", "origin", dest_url],
                check=True,
                capture_output=True,
            )

            if self.debug:
                print(f"[debug] git push --mirror origin -> {dest_display}", file=sys.stderr)

            try:
                subprocess.run(
                    ["git", "-C", mirror_path, "push", "--mirror", "origin"],
                    check=True,
                    capture_output=not self.debug,
                    text=True,
                )
            except subprocess.CalledProcessError as e:
                raise APIError(
                    f"git push failed to {dest_display}: {(e.stderr or '').strip()}"
                )

    def push_local(self, local_path: str, owner: str, dest_repo: str) -> None:
        """
        Push a local directory's code to a new empty GitHub repo.
        If local_path is a git repo, its full history is pushed.
        Otherwise files are staged in a fresh repo and pushed as an initial commit.
        """
        dest_url = self.git_remote_url(owner, dest_repo)
        dest_display = dest_url
        is_git_repo = os.path.isdir(os.path.join(local_path, ".git"))

        with tempfile.TemporaryDirectory() as tmpdir:
            work_path = os.path.join(tmpdir, "work")

            if is_git_repo:
                if self.debug:
                    print(f"[debug] git clone {local_path} {work_path}", file=sys.stderr)
                try:
                    subprocess.run(
                        ["git", "clone", local_path, work_path],
                        check=True, capture_output=not self.debug, text=True,
                    )
                except subprocess.CalledProcessError as e:
                    raise APIError(f"git clone (local) failed: {(e.stderr or '').strip()}")
            else:
                # Not a git repo — copy files and create an initial commit
                shutil.copytree(local_path, work_path)
                try:
                    subprocess.run(
                        ["git", "init", work_path],
                        check=True, capture_output=True, text=True,
                    )
                    subprocess.run(
                        ["git", "-C", work_path, "add", "-A"],
                        check=True, capture_output=True, text=True,
                    )
                    # Check whether there is anything to commit
                    staged = subprocess.run(
                        ["git", "-C", work_path, "diff", "--cached", "--quiet"],
                        capture_output=True,
                    )
                    if staged.returncode != 0:  # has staged changes
                        subprocess.run(
                            ["git", "-C", work_path, "commit", "-m", "Initial commit"],
                            check=True, capture_output=True, text=True,
                        )
                    else:
                        # Empty directory — nothing to push
                        return
                except subprocess.CalledProcessError as e:
                    raise APIError(
                        f"Failed to create initial git commit: {(e.stderr or '').strip()}"
                    )

            if self.debug:
                print(f"[debug] git push --all --tags -> {dest_display}", file=sys.stderr)

            try:
                # git clone sets up origin pointing to local_path; update it.
                # For fresh git init there is no origin yet; add it.
                if is_git_repo:
                    subprocess.run(
                        ["git", "-C", work_path, "remote", "set-url", "origin", dest_url],
                        check=True, capture_output=True,
                    )
                else:
                    subprocess.run(
                        ["git", "-C", work_path, "remote", "add", "origin", dest_url],
                        check=True, capture_output=True,
                    )
                subprocess.run(
                    ["git", "-C", work_path, "push", "origin", "--all"],
                    check=True, capture_output=not self.debug, text=True,
                )
                subprocess.run(
                    ["git", "-C", work_path, "push", "origin", "--tags"],
                    check=True, capture_output=not self.debug, text=True,
                )
            except subprocess.CalledProcessError as e:
                raise APIError(
                    f"git push failed to {dest_display}: {(e.stderr or '').strip()}"
                )

        # Wire up the original local repo to the newly created remote so
        # future `git push` / `git pull` work without extra configuration.
        if is_git_repo:
            try:
                subprocess.run(
                    ["git", "-C", local_path, "remote", "add", "origin", dest_display],
                    check=True, capture_output=True,
                )
                result = subprocess.run(
                    ["git", "-C", local_path, "symbolic-ref", "--short", "HEAD"],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    branch = result.stdout.strip()
                    subprocess.run(
                        ["git", "-C", local_path, "branch", "--set-upstream-to",
                         f"origin/{branch}", branch],
                        capture_output=True,
                    )
            except subprocess.CalledProcessError:
                pass  # non-fatal: remote wiring is a convenience

    def clone_for_scan(self, owner: str, repo: str, dest_path: str) -> None:
        """Full-clone repo into dest_path for pre-flight scanning.

        A full clone (no --depth) is required so truffleHog can scan the
        complete git history for secrets, not just the working-tree snapshot.
        """
        clone_url = self.git_remote_url(owner, repo)
        display_url = clone_url
        if self.debug:
            print(f"[debug] git clone {display_url} {dest_path}", file=sys.stderr)
        try:
            subprocess.run(
                ["git", "clone", clone_url, dest_path],
                check=True,
                capture_output=not self.debug,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise APIError(f"git clone (scan) failed for {display_url}: {(e.stderr or '').strip()}")

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
