"""
GitHub API client wrapping `gh api` via subprocess.
Auth priority: GITHUB_TOKEN env var > gh auth token > error.

Git transport (push/clone/preflight) lives in git_transport.GitTransport.
Callers must assign `self.transport` before invoking copy_repo / push_local /
clone_for_scan. See commands/create.py for the wiring.
"""

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .errors import APIError, AuthError
from .git_transport import GitTransport
from .local_repo import is_git_checkout


@dataclass(frozen=True)
class LocalWiring:
    """Outcome of wiring the user's repo to the new remote after push_local."""
    branch: Optional[str] = None
    origin: bool = False
    tracking: bool = False
    error: Optional[str] = None
    # Branch made the new repo's default; None when unknown or not set.
    default_branch: Optional[str] = None
    # What the push sent (#77): branch name -> commit SHA, and tag name ->
    # the SHA the tag ref points at (the tag object for an annotated tag, the
    # commit for a lightweight one; the same value `git/matching-refs`
    # reports as object.sha). Empty when nothing was pushed.
    pushed: Dict[str, str] = field(default_factory=dict)
    tags: Dict[str, str] = field(default_factory=dict)
    # An 'origin' that already pointed elsewhere and was left untouched.
    foreign_origin: Optional[str] = None


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

    def get_repo_data(self, owner: str, repo: str, refresh: bool = False) -> dict:
        """Fetch /repos/{owner}/{repo} once and cache; returns the raw response dict.

        The cache lives for the whole process, so anything read after an apply
        (post-condition verification, #77) must pass ``refresh=True`` or call
        ``forget_repo()`` first, otherwise it sees the pre-apply state.
        """
        key = (owner, repo)
        if refresh or key not in self._repo_cache:
            self._repo_cache.pop(key, None)
            self._repo_cache[key] = self.get_json(self.repo_path(owner, repo))
        return self._repo_cache[key]

    def forget_repo(self, owner: str, repo: str) -> None:
        """Drop the cached GET /repos/{owner}/{repo}; the next read refetches."""
        self._repo_cache.pop((owner, repo), None)

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

    def call_api(self, method, endpoint, body=None, paginate=False):
        """
        Call the GitHub API via `gh api`.
        Returns (status_code, response_text).
        Raises APIError on non-2xx responses (except 404, which callers handle).

        paginate=True adds `--paginate`: gh follows the Link headers and
        writes every page's JSON back to back (see get_json_pages).
        """
        cmd = ["gh", "api", "--method", method, endpoint]
        if paginate:
            cmd.append("--paginate")

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

    def get_json_pages(self, endpoint) -> list:
        """GET every page of an endpoint; returns one decoded value per page.

        `gh api --paginate` without `--slurp` (which older gh lacks) prints
        each page as its own JSON document, so a two-page array endpoint
        yields `[...][...]`, not one valid JSON value. Decode it as a stream.
        Raises APIError on failure or a non-JSON response.
        """
        status, text = self.call_api("GET", endpoint, paginate=True)
        if status and status >= 400:
            raise APIError(f"GET {endpoint} returned {status}", status_code=status)
        try:
            return _decode_json_stream(text or "")
        except json.JSONDecodeError:
            raise APIError(f"GET {endpoint} returned non-JSON response")

    def matching_refs(self, owner: str, repo: str, namespace: str) -> Dict[str, str]:
        """Return {name: sha} for every ref under refs/<namespace>/ on GitHub.

        Uses the REST API (`git/matching-refs`), never `git ls-remote`: over
        SSH every git connection can cost a hardware-key touch (#77). The sha
        is the ref's own target, so an annotated tag maps to its tag object,
        not the peeled commit. An empty repository (409 "Git Repository is
        empty") is {}; any other failure raises APIError.
        """
        endpoint = self.repo_path(
            owner, repo, f"git/matching-refs/{namespace}/"
        ) + "?per_page=100"
        try:
            pages = self.get_json_pages(endpoint)
        except APIError as e:
            if e.status_code == 409:
                return {}
            raise
        prefix = f"refs/{namespace}/"
        refs: Dict[str, str] = {}
        for page in pages:
            if not isinstance(page, list):
                raise APIError(f"GET {endpoint} returned an unexpected response")
            for item in page:
                ref = (item or {}).get("ref") or ""
                if ref.startswith(prefix):
                    refs[ref[len(prefix):]] = (item.get("object") or {}).get("sha")
        return refs

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

    def copy_repo(self, owner, source_repo, dest_repo, default_branch=None):
        """
        Mirror-clone source_repo and push all refs to dest_repo.
        Both repos must belong to owner.
        Uses the user's git credentials via self.transport.

        default_branch (the source's) is then set on dest_repo: a mirror
        push does not carry HEAD, so GitHub would otherwise pick its own
        default (#77 verifies it).
        """
        transport = self._require_transport()
        source_url = transport.remote_url(owner, source_repo)
        dest_url = transport.remote_url(owner, dest_repo)

        with tempfile.TemporaryDirectory() as tmpdir:
            mirror_path = os.path.join(tmpdir, "mirror")

            try:
                transport.run_remote(["git", "clone", "--mirror", source_url, mirror_path])
            except subprocess.CalledProcessError as e:
                raise APIError(transport.redact(
                    f"git clone failed for {source_url}: {(e.stderr or '').strip()}"
                ))

            transport.run(
                ["git", "-C", mirror_path, "remote", "set-url", "--push", "origin", dest_url],
            )

            try:
                transport.run_remote(["git", "-C", mirror_path, "push", "--mirror", "origin"])
            except subprocess.CalledProcessError as e:
                raise APIError(transport.redact(
                    f"git push failed to {dest_url}: {(e.stderr or '').strip()}"
                ))

        if default_branch:
            self.call_api("PATCH", self.repo_path(owner, dest_repo),
                          {"default_branch": default_branch})

    def push_local(self, local_path: str, owner: str, dest_repo: str,
                   default_branch: Optional[str] = None) -> "LocalWiring":
        """
        Push a local directory's code to a new empty GitHub repo.
        If local_path is a git repo, every local branch and tag is pushed
        and the original repo gets `origin` + upstream tracking; the
        returned LocalWiring says what was done. Otherwise files are staged
        in a fresh repo and pushed as an initial commit.

        default_branch is pushed first and then set as the repo default
        (#86); without it GitHub picks whichever branch arrives first.

        A linked worktree or submodule (`.git` file) takes the git path:
        `clone --bare` follows the gitdir. A worktree shares its main repo's
        refs, so "every local branch" is the main repo's branches (#103).
        """
        transport = self._require_transport()
        dest_url = transport.remote_url(owner, dest_repo)
        is_git_repo = is_git_checkout(local_path)

        with tempfile.TemporaryDirectory() as tmpdir:
            work_path = os.path.join(tmpdir, "work")

            if is_git_repo:
                try:
                    # --bare: every source branch becomes a local head, so
                    # `push --all` sends them all. A normal clone has only
                    # the checked-out branch as a head (#86).
                    transport.run(["git", "clone", "--bare", local_path, work_path])
                except subprocess.CalledProcessError as e:
                    raise APIError(f"git clone (local) failed: {(e.stderr or '').strip()}")
            else:
                # Not a git repo — copy files and create an initial commit.
                # These local-only ops don't need transport env; using it for
                # consistency keeps a single subprocess path.
                # Never copy a .git entry: a .git file would point the git
                # commands below at the real repository and commit into it
                # (#103). is_git_checkout() routes worktrees and submodules to
                # the clone path above; this guards anything it misses.
                shutil.copytree(local_path, work_path,
                                ignore=shutil.ignore_patterns(".git"))
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
                        return LocalWiring()
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
                # Record what will be pushed so the user's repo can be given
                # matching remote-tracking refs without a network fetch, and
                # so verification can compare GitHub against it (#77).
                pushed_refs = self._local_refs(transport, work_path, "refs/heads")
                pushed_tags = self._local_refs(transport, work_path, "refs/tags")
                if default_branch not in pushed_refs:
                    default_branch = None
                # One push for every branch and tag: each connection costs a
                # security-key touch, and a miss between separate pushes left
                # a half-pushed repo (#79). Default branch first, since the
                # first branch to land becomes GitHub's default.
                order = sorted(pushed_refs, key=lambda b: b != default_branch)
                refspecs = [f"refs/heads/{b}:refs/heads/{b}" for b in order]
                refspecs.append("refs/tags/*:refs/tags/*")
                transport.run_remote(
                    ["git", "-C", work_path, "push", "origin", *refspecs]
                )
            except subprocess.CalledProcessError as e:
                raise APIError(transport.redact(
                    f"git push failed to {dest_url}: {(e.stderr or '').strip()}"
                ))

        if not is_git_repo:
            return LocalWiring(pushed=pushed_refs, tags=pushed_tags)
        if default_branch:
            # Belt and braces: don't rely on first-push ordering alone.
            self.call_api("PATCH", self.repo_path(owner, dest_repo),
                          {"default_branch": default_branch})
        wiring = self._wire_local_repo(local_path, owner, dest_repo, pushed_refs)
        return dataclasses.replace(
            wiring, default_branch=default_branch,
            pushed=pushed_refs, tags=pushed_tags,
        )

    @staticmethod
    def _local_refs(transport, work_path, prefix) -> Dict[str, str]:
        """{name: sha} for the refs under prefix in work_path.

        lstrip=2, not refname:short: :short renders a branch that shares a
        tag's name as "heads/<name>". objectname is the ref's own target (the
        tag object for an annotated tag), matching the API's object.sha.
        """
        out = transport.run(
            ["git", "-C", work_path, "for-each-ref",
             "--format=%(refname:lstrip=2) %(objectname)", prefix],
            check=False,
        ).stdout or ""
        return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)

    def _wire_local_repo(self, local_path, owner, dest_repo, pushed_refs):
        """Point the user's original repo at the new remote and set upstream.

        The push ran from a temp clone, so the user's repo has no
        refs/remotes/origin/* and `git branch --set-upstream-to` would fail
        (#70). The pushed SHAs are known, so the remote-tracking refs are
        written directly; no fetch (and no credentials) needed.

        persistent_url, not remote_url: a token-injected URL must never be
        written into the user's long-lived .git/config.
        """
        transport = self._require_transport()
        url = transport.persistent_url(owner, dest_repo)
        git = ["git", "-C", local_path]

        head = transport.run(git + ["symbolic-ref", "--short", "HEAD"], check=False)
        branch = head.stdout.strip() if head.returncode == 0 else None

        existing = transport.run(git + ["remote", "get-url", "origin"], check=False)
        if existing.returncode == 0:
            if existing.stdout.strip() != url:
                return LocalWiring(
                    branch=branch,
                    error=(f"'origin' already points at {existing.stdout.strip()}; "
                           f"left unchanged"),
                    foreign_origin=existing.stdout.strip(),
                )
        else:
            try:
                transport.run(git + ["remote", "add", "origin", url])
            except subprocess.CalledProcessError as e:
                return LocalWiring(
                    branch=branch,
                    error=f"could not add 'origin': {(e.stderr or '').strip()}",
                )

        if branch is None:
            return LocalWiring(origin=True, error="detached HEAD; no branch to track")

        try:
            for name, sha in pushed_refs.items():
                transport.run(git + ["update-ref", f"refs/remotes/origin/{name}", sha])
            transport.run(
                git + ["branch", f"--set-upstream-to=origin/{branch}", branch],
            )
        except subprocess.CalledProcessError as e:
            return LocalWiring(
                branch=branch, origin=True,
                error=f"could not set upstream: {(e.stderr or '').strip()}",
            )
        return LocalWiring(branch=branch, origin=True, tracking=True)

    def clone_for_scan(self, owner: str, repo: str, dest_path: str) -> None:
        """Full-clone repo into dest_path for pre-flight scanning.

        A full clone (no --depth) is required so truffleHog can scan the
        complete git history for secrets, not just the working-tree snapshot.
        """
        transport = self._require_transport()
        clone_url = transport.remote_url(owner, repo)
        try:
            transport.run_remote(["git", "clone", clone_url, dest_path])
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


def _decode_json_stream(text: str) -> List:
    """Decode zero or more concatenated JSON values (`gh api --paginate`)."""
    decoder = json.JSONDecoder()
    values = []
    i, n = 0, len(text)
    while True:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            return values
        value, i = decoder.raw_decode(text, i)
        values.append(value)
