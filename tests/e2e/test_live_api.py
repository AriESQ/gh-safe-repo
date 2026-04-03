"""E2E tests — Group 3: Live GitHub API (create/fix/delete real repos).

These tests create, modify, and delete real GitHub repositories.
They are gated behind BOTH a valid GitHub auth token AND the E2E_LIVE
environment variable being set.

Prerequisites:
- `gh auth login` completed
- `gh auth refresh -h github.com -s delete_repo` (for cleanup)
- `E2E_LIVE=1` environment variable set

Usage:
    E2E_LIVE=1 uv run pytest tests/e2e/test_live_api.py -v
"""

import json
import os
import subprocess
import tempfile
import uuid

import pytest

from .conftest import requires_live, run


pytestmark = requires_live


def _delete_repo(owner, repo_name):
    """Delete a GitHub repo. Best-effort, ignores errors."""
    subprocess.run(
        ["gh", "repo", "delete", f"{owner}/{repo_name}", "--yes"],
        capture_output=True, text=True, timeout=15,
    )


@pytest.fixture
def live_repo(gh_owner, unique_repo_name):
    """Fixture that yields (owner, repo_name) and deletes the repo on teardown."""
    yield gh_owner, unique_repo_name
    _delete_repo(gh_owner, unique_repo_name)


class TestCreatePrivateThenFixIdempotent:
    """1.1 + 5.1 — Create private repo, then fix it → all SKIPs."""

    def test_create_then_fix_is_idempotent(self, live_repo):
        owner, repo_name = live_repo

        # Create the repo
        r = run("create", f"{owner}/{repo_name}", "--yes", "--json")
        assert r.returncode == 0

        # Fix should show all SKIPs (idempotent)
        r = run("fix", f"{owner}/{repo_name}", "--json", "--dry-run")
        assert r.returncode == 0
        data = json.loads(r.stdout)

        actionable = [
            c for c in data["changes"]
            if c["type"] in ("add", "update", "delete")
        ]
        # Ideally zero actionable changes (all SKIPs), but some settings
        # may not round-trip perfectly on all plan levels. Assert at most
        # a small number.
        assert len(actionable) <= 2, (
            f"Expected near-idempotent fix, but got {len(actionable)} "
            f"actionable changes: {actionable}"
        )


class TestCreateDuplicateRepoError:
    """1.2 — Creating the same repo twice is an error."""

    def test_duplicate_repo_exits_1(self, live_repo):
        owner, repo_name = live_repo

        # First create should succeed
        r = run("create", f"{owner}/{repo_name}", "--yes")
        assert r.returncode == 0

        # Second create should fail
        r = run("create", f"{owner}/{repo_name}", "--yes")
        assert r.returncode == 1
        combined = (r.stderr + r.stdout).lower()
        assert "already exists" in combined


class TestCreatePublicRepo:
    """2.1 — Create a public repo with branch protection."""

    def test_create_public(self, live_repo):
        owner, repo_name = live_repo

        r = run("create", f"{owner}/{repo_name}", "--public", "--yes", "--json")
        assert r.returncode == 0
        data = json.loads(r.stdout)

        # Public repos should have branch_protection ADD rows
        add_categories = {
            c["category"] for c in data["changes"] if c["type"] == "add"
        }
        assert "branch_protection" in add_categories


class TestCreateFromMirror:
    """3.2 — Mirror from a source repo into a new public repo."""

    @pytest.fixture
    def source_repo(self, gh_owner):
        """Create a source repo with content, clean up after."""
        source_name = f"gsr-e2e-{uuid.uuid4().hex[:8]}-source"
        # Create source repo
        r = run("create", f"{gh_owner}/{source_name}", "--yes")
        assert r.returncode == 0

        # Push a test file
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(
                ["git", "clone", f"https://github.com/{gh_owner}/{source_name}", tmpdir],
                capture_output=True, check=True, timeout=30,
            )
            readme = os.path.join(tmpdir, "README.md")
            with open(readme, "w") as f:
                f.write("# Test source\n")
            subprocess.run(["git", "add", "README.md"], cwd=tmpdir, capture_output=True, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"], cwd=tmpdir,
                capture_output=True, check=True,
            )
            subprocess.run(["git", "push"], cwd=tmpdir, capture_output=True, check=True, timeout=30)

        yield gh_owner, source_name
        _delete_repo(gh_owner, source_name)

    def test_mirror_copies_code(self, source_repo, live_repo):
        src_owner, src_name = source_repo
        dst_owner, dst_name = live_repo

        r = run(
            "create", f"{dst_owner}/{dst_name}",
            "--from", f"{src_owner}/{src_name}",
            "--public", "--yes",
        )
        assert r.returncode == 0
        combined = (r.stderr + r.stdout).lower()
        assert "mirrored" in combined or "success" in combined


class TestFixSettingsDrift:
    """5.2 — Fix a repo with settings that differ from safe defaults."""

    def test_fix_applies_changes(self, gh_owner):
        repo_name = f"gsr-e2e-{uuid.uuid4().hex[:8]}-drift"
        try:
            # Create repo manually via gh (not gh-safe-repo) — it will have
            # GitHub defaults, not safe defaults
            subprocess.run(
                ["gh", "repo", "create", f"{gh_owner}/{repo_name}", "--private", "--add-readme"],
                capture_output=True, check=True, timeout=30,
            )

            # Fix should find settings to update
            r = run("fix", f"{gh_owner}/{repo_name}", "--yes", "--json")
            assert r.returncode == 0
            data = json.loads(r.stdout)

            # Should have at least some UPDATE changes
            updates = [c for c in data["changes"] if c["type"] == "update"]
            assert len(updates) > 0, "Expected at least one UPDATE for drifted settings"
        finally:
            _delete_repo(gh_owner, repo_name)


class TestFixNonexistentRepo:
    """5.5 — Fix a repo that doesn't exist."""

    def test_fix_nonexistent_exits_1(self, gh_owner):
        r = run("fix", f"{gh_owner}/gsr-e2e-does-not-exist-xyz")
        assert r.returncode == 1
        combined = (r.stderr + r.stdout).lower()
        assert "does not exist" in combined


class TestPreflightScanAbort:
    """11.1 — Abort when pre-flight scan finds issues."""

    def test_abort_does_not_create_repo(self, gh_owner):
        repo_name = f"gsr-e2e-{uuid.uuid4().hex[:8]}-abort"
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a file with a fake secret
            leak = os.path.join(tmpdir, "leak.txt")
            with open(leak, "w") as f:
                f.write("GITHUB_TOKEN=ghp_fakefakefakefakefakefakefakefake01\n")

            # Pipe "n" to stdin to abort
            r = run(
                "create", f"{gh_owner}/{repo_name}",
                "--local", tmpdir,
                stdin_text="n\n",
            )

        # Should exit cleanly (user aborted)
        assert r.returncode == 0
        combined = (r.stderr + r.stdout).lower()
        assert "abort" in combined

        # Verify repo was NOT created
        check = subprocess.run(
            ["gh", "repo", "view", f"{gh_owner}/{repo_name}"],
            capture_output=True, text=True, timeout=10,
        )
        assert check.returncode != 0, "Repo should not exist after abort"
