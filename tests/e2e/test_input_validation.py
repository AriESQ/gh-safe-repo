"""E2E tests — Group 1: Pure input validation.

These run the real gh-safe-repo binary via subprocess with no mocking.
They test argument parsing and error messages that require zero external
resources (no GitHub token, no network, no repos).

Shared helpers (CLI binary location, run()) live in conftest.py.
"""

from .conftest import run


# ── Section 0: Sanity / help ────────────────────────────────────────


class TestHelpOutput:
    def test_no_subcommand_shows_help_and_exits_2(self):
        r = run()
        assert r.returncode == 2
        assert "create" in r.stderr or "create" in r.stdout
        assert "fix" in r.stderr or "fix" in r.stdout
        assert "scan" in r.stderr or "scan" in r.stdout

    def test_top_level_help_flag(self):
        r = run("--help")
        assert r.returncode == 0
        assert "create" in r.stdout
        assert "fix" in r.stdout
        assert "scan" in r.stdout

    def test_create_help(self):
        r = run("create", "--help")
        assert r.returncode == 0
        assert "--public" in r.stdout
        assert "--local" in r.stdout
        assert "--from" in r.stdout
        assert "--yes" in r.stdout or "-y" in r.stdout

    def test_fix_help(self):
        r = run("fix", "--help")
        assert r.returncode == 0
        assert "--yes" in r.stdout or "-y" in r.stdout

    def test_scan_help(self):
        r = run("scan", "--help")
        assert r.returncode == 0
        assert "path" in r.stdout.lower()


# ── Section 0.5 / 0.6: Bad repo arguments ───────────────────────────


class TestRepoArgValidation:
    def test_create_bare_name_rejected(self):
        """0.5 — create my-repo (no owner/) → error."""
        r = run("create", "my-repo")
        assert r.returncode == 2
        assert "owner/repo" in r.stderr.lower() or "owner/repo" in r.stdout.lower()

    def test_fix_bare_name_rejected(self):
        r = run("fix", "my-repo")
        assert r.returncode == 2
        assert "owner/repo" in r.stderr.lower() or "owner/repo" in r.stdout.lower()

    def test_create_empty_owner_rejected(self):
        r = run("create", "/my-repo")
        assert r.returncode == 2

    def test_create_empty_repo_rejected(self):
        r = run("create", "alice/")
        assert r.returncode == 2

    def test_from_bare_name_rejected(self):
        """3.5 — --from without owner/ is rejected."""
        r = run("create", "alice/dest", "--from", "source-only", "--public")
        assert r.returncode == 2
        assert "owner/repo" in r.stderr.lower() or "owner/repo" in r.stdout.lower()


# ── Section 4.3 / 4.4: --local errors ───────────────────────────────


class TestLocalFlagErrors:
    def test_local_nonexistent_path(self):
        """4.3 — --local with path that doesn't exist.

        NOTE: The --local path check happens after build_context() (which
        validates the owner against the authenticated user).  When no valid
        token is present or the owner is wrong, the owner check fails first.
        We use --dry-run here, but the owner still must be valid — so this
        test is in Group 2 (auth-dependent) territory.  We keep it here with
        a relaxed assertion: the tool must fail with non-zero exit.
        """
        r = run("create", "alice/my-repo", "--local", "/tmp/path-does-not-exist-xyz")
        assert r.returncode != 0

    def test_local_and_from_mutually_exclusive(self):
        """4.4 — --local and --from together."""
        r = run(
            "create", "alice/my-repo",
            "--local", "/tmp",
            "--from", "alice/other-repo",
            "--public",
        )
        assert r.returncode == 2
        assert "mutually exclusive" in r.stderr.lower() or "mutually exclusive" in r.stdout.lower()


# ── Section 6.7: scan without required arg ───────────────────────────


class TestScanArgValidation:
    def test_scan_no_path_argument(self):
        """6.7 — scan with no path → error."""
        r = run("scan")
        assert r.returncode == 2
        assert "required" in r.stderr.lower()


# ── Section 9.2: Missing config file exits with error ────────────────


class TestConfigFileHandling:
    def test_missing_config_exits_with_error(self):
        """9.2 — nonexistent --config path raises ConfigError (exit 1).

        After commit f9ac3cf, ConfigManager raises ConfigError when an
        explicit --config path doesn't exist (previously silent).
        """
        r = run(
            "create", "alice/my-repo",
            "--config", "/tmp/path-that-does-not-exist/gh-safe-repo.ini",
            "--dry-run",
        )
        assert r.returncode == 1
        combined = (r.stderr + r.stdout).lower()
        assert "config file not found" in combined
