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
        for verb in ("create", "publish", "mirror", "audit", "fix", "scan"):
            assert verb in r.stdout
        assert "--no-config" in r.stdout

    def test_create_help(self):
        r = run("create", "--help")
        assert r.returncode == 0
        assert "--public" in r.stdout
        assert "--local" not in r.stdout
        assert "--from" not in r.stdout
        assert "--yes" in r.stdout or "-y" in r.stdout

    def test_publish_help_positionals_are_source_then_destination(self):
        r = run("publish", "--help")
        assert r.returncode == 0
        assert r.stdout.index("PATH") < r.stdout.index("OWNER/REPO")
        assert "--public" in r.stdout

    def test_mirror_help_says_not_a_fork(self):
        r = run("mirror", "--help")
        assert r.returncode == 0
        assert "not a GitHub fork" in r.stdout
        assert r.stdout.index("SRC-OWNER/REPO") < r.stdout.index(" OWNER/REPO")

    def test_audit_help_has_no_mutation_flags(self):
        r = run("audit", "--help")
        assert r.returncode == 0
        assert "--dry-run" not in r.stdout
        assert "--yes" not in r.stdout
        assert "--json" in r.stdout

    def test_fix_help(self):
        r = run("fix", "--help")
        assert r.returncode == 0
        assert "--yes" in r.stdout or "-y" in r.stdout

    def test_scan_help(self):
        r = run("scan", "--help")
        assert r.returncode == 0
        assert "path" in r.stdout.lower()

    def test_top_level_help_lists_global_options(self):
        r = run("--help")
        assert r.returncode == 0
        assert "--config" in r.stdout
        assert "--debug" in r.stdout

    def test_global_debug_before_command(self, tmp_path):
        """--debug must work before the command, not just after it."""
        r = run("--debug", "scan", str(tmp_path))
        assert r.returncode == 0
        assert "[debug]" in r.stderr


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

    def test_mirror_source_bare_name_rejected(self):
        """3.5 — mirror source without owner/ is rejected."""
        r = run("mirror", "source-only", "alice/dest", "--public")
        assert r.returncode == 2
        assert "owner/repo" in r.stderr.lower() or "owner/repo" in r.stdout.lower()

    def test_mirror_swapped_positionals_rejected(self):
        """A path where the source owner/repo belongs is a usage error."""
        r = run("mirror", "alice/dest", "/tmp", "--public")
        assert r.returncode == 2
        assert "owner/repo" in r.stderr.lower() or "owner/repo" in r.stdout.lower()


# ── Section 4.3 / 4.4: publish source errors ────────────────────────


class TestPublishSourceErrors:
    def test_publish_nonexistent_path(self):
        """4.3 — publish with a path that doesn't exist.

        NOTE: The path check happens after build_context() (which
        validates the owner against the authenticated user).  When no
        valid token is present or the owner is wrong, the owner check
        fails first, so this only asserts a non-zero exit; the full
        assertion lives in Group 2 (auth-dependent).
        """
        r = run("publish", "/tmp/path-does-not-exist-xyz", "alice/my-repo")
        assert r.returncode != 0

    def test_create_rejects_old_code_source_flags(self):
        """4.4 — --local / --from no longer exist on create."""
        r = run("create", "alice/my-repo", "--local", "/tmp")
        assert r.returncode == 2
        assert "unrecognized arguments" in r.stderr
        r = run("create", "alice/my-repo", "--from", "alice/other-repo")
        assert r.returncode == 2
        assert "unrecognized arguments" in r.stderr


# ── Section 6.7: scan with no path argument ────────────────────────────


class TestScanArgValidation:
    def test_scan_no_path_scans_cwd(self, tmp_path):
        """6.7 — scan with no path scans the current directory."""
        import subprocess
        from .conftest import CLI
        (tmp_path / "README.md").write_text("# clean\n")
        r = subprocess.run([CLI, "scan"], cwd=tmp_path, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0
        assert str(tmp_path.resolve()) in r.stdout or str(tmp_path) in r.stdout


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

    def test_config_without_value_is_usage_error(self):
        r = run("scan", "/tmp", "--config")
        assert r.returncode == 2
        assert "expected one argument" in r.stderr

    def test_config_and_no_config_together_is_usage_error(self, tmp_path):
        r = run("scan", str(tmp_path), "--config", "x.ini", "--no-config")
        assert r.returncode == 2
        assert "mutually exclusive" in r.stderr
