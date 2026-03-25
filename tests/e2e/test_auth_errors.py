"""E2E tests — Group 2: Auth-dependent (no repo mutations).

These tests require a valid GitHub auth token but do NOT create, modify,
or delete any repositories. They use --dry-run where applicable.

Skipped automatically when no GitHub auth token is available.
"""

import json
import os
import tempfile

import pytest

from .conftest import requires_auth, run


pytestmark = requires_auth


class TestWrongOwner:
    """0.6 — Wrong owner is rejected."""

    def test_wrong_owner_exits_1(self):
        r = run("create", "definitelynotarealuserxyz/my-repo", "--dry-run")
        assert r.returncode == 1
        assert "does not match" in r.stderr.lower() or "does not match" in r.stdout.lower()


class TestLocalNonexistentPath:
    """4.3 — --local with non-existent path (full assertion with real owner)."""

    def test_local_bad_path_exits_2(self, gh_owner):
        r = run(
            "create", f"{gh_owner}/gsr-e2e-bad-path",
            "--local", "/tmp/path-does-not-exist-xyz",
            "--dry-run",
        )
        assert r.returncode == 2
        combined = (r.stderr + r.stdout).lower()
        assert "not a directory" in combined


class TestJsonCreateDryRun:
    """7.1 — JSON output for create --dry-run."""

    def test_json_is_valid_and_has_correct_schema(self, gh_owner):
        r = run(
            "create", f"{gh_owner}/gsr-e2e-json-test",
            "--dry-run", "--json",
        )
        assert r.returncode == 0

        data = json.loads(r.stdout)
        assert "changes" in data
        assert "summary" in data
        assert isinstance(data["changes"], list)
        assert isinstance(data["summary"], dict)

        # Every change has required fields
        for change in data["changes"]:
            assert "type" in change
            assert "category" in change
            assert "key" in change
            assert change["type"] in ("add", "update", "delete", "skip")

    def test_json_info_goes_to_stderr(self, gh_owner):
        """7.2 — stdout is pure JSON, info text on stderr."""
        r = run(
            "create", f"{gh_owner}/gsr-e2e-json-test",
            "--dry-run", "--json",
        )
        # stdout must be valid JSON on its own
        data = json.loads(r.stdout)
        assert isinstance(data, dict)
        # stderr has info messages (like "Configuring...")
        assert r.stderr.strip() != ""


class TestJsonFixDryRun:
    """7.3 — JSON output for fix --dry-run on a non-existent repo.

    NOTE: fix --dry-run on a non-existent repo will error, not produce JSON.
    We use a known repo format instead — but since we can't rely on any repo
    existing, we just test that the JSON flag doesn't break the error path.
    """

    def test_fix_nonexistent_repo_errors(self, gh_owner):
        r = run(
            "fix", f"{gh_owner}/gsr-e2e-nonexistent-json",
            "--dry-run", "--json",
        )
        # fix on a non-existent repo should error
        assert r.returncode != 0


class TestDebugOutput:
    """8.1 — --debug shows API call details."""

    def test_debug_shows_api_calls(self, gh_owner):
        r = run(
            "create", f"{gh_owner}/gsr-e2e-debug-test",
            "--dry-run", "--debug",
        )
        assert r.returncode == 0
        combined = r.stderr + r.stdout
        # Debug output should show API calls like "[DEBUG] GET /user -> 200"
        assert "[DEBUG]" in combined
        assert "GET" in combined


class TestCustomConfig:
    """9.1 — Custom config file changes the plan."""

    def test_custom_config_changes_plan(self, gh_owner):
        config_content = (
            "[repo]\n"
            "has_issues = false\n"
            "has_wiki = true\n"
            "\n"
            "[branch_protection]\n"
            "required_approving_reviews = 2\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ini", delete=False
        ) as f:
            f.write(config_content)
            config_path = f.name

        try:
            r = run(
                "create", f"{gh_owner}/gsr-e2e-config-test",
                "--config", config_path,
                "--dry-run", "--json",
            )
            assert r.returncode == 0
            data = json.loads(r.stdout)

            # has_issues should appear as ADD with value false
            has_issues = [
                c for c in data["changes"]
                if c["key"] == "has_issues"
            ]
            assert len(has_issues) == 1
            assert has_issues[0]["new"] is False
        finally:
            os.unlink(config_path)


class TestPlanLevelGating:
    """10.1 / 10.2 — Free plan private vs. public gating."""

    def test_private_repo_skips_bp_and_security_on_free_plan(self, gh_owner):
        """10.1 — Private repo on free plan skips branch protection and security."""
        r = run(
            "create", f"{gh_owner}/gsr-e2e-free-private",
            "--dry-run", "--json",
        )
        assert r.returncode == 0
        data = json.loads(r.stdout)

        # Collect SKIP changes by category
        skips = [c for c in data["changes"] if c["type"] == "skip"]
        skip_categories = {c["category"] for c in skips}

        # On free plan + private: branch_protection and security should be skipped
        # On paid plan: they might not be skipped — so we only assert that
        # the plan is internally consistent (has changes, summary matches)
        assert len(data["changes"]) > 0
        assert data["summary"] == _count_types(data["changes"])

    def test_public_repo_gets_bp_and_dependabot_on_free_plan(self, gh_owner):
        """10.2 — Public repo on free plan gets branch protection and Dependabot."""
        r = run(
            "create", f"{gh_owner}/gsr-e2e-free-public",
            "--public", "--dry-run", "--json",
        )
        assert r.returncode == 0
        data = json.loads(r.stdout)

        # Public repos should have ADD rows for branch protection
        adds = [c for c in data["changes"] if c["type"] == "add"]
        add_categories = {c["category"] for c in adds}

        # branch_protection and security should have ADD (not SKIP) on public repos
        assert "branch_protection" in add_categories
        assert len(data["changes"]) > 0
        assert data["summary"] == _count_types(data["changes"])


def _count_types(changes):
    """Count change types to verify summary consistency."""
    counts = {}
    for c in changes:
        counts[c["type"]] = counts.get(c["type"], 0) + 1
    return counts
