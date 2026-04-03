"""E2E tests — Group 1b: Scan workflow.

These test the `gh-safe-repo scan` subcommand against real temporary
directories. No GitHub token or network required.
"""

import os
import subprocess

import pytest

from .conftest import run


class TestScanCleanDirectory:
    """6.1 — Scan a directory with no issues."""

    def test_clean_dir_exits_0(self, tmp_scan_dir):
        readme = os.path.join(tmp_scan_dir, "README.md")
        with open(readme, "w") as f:
            f.write("# Hello World\n")

        r = run("scan", tmp_scan_dir)
        assert r.returncode == 0
        assert "No issues found" in r.stdout


class TestScanFakeSecret:
    """6.2 — Scan a directory with a fake AWS key."""

    def test_fake_secret_exits_1(self, tmp_scan_dir):
        creds = os.path.join(tmp_scan_dir, "creds.env")
        with open(creds, "w") as f:
            f.write("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n")

        r = run("scan", tmp_scan_dir)
        assert r.returncode == 1
        assert "[CRITICAL]" in r.stdout


class TestScanEmailAddress:
    """6.3 — Scan a directory with an email address."""

    def test_email_shows_warning(self, tmp_scan_dir):
        readme = os.path.join(tmp_scan_dir, "README.md")
        with open(readme, "w") as f:
            f.write("Contact: example@example.com\n")

        r = run("scan", tmp_scan_dir)
        assert "[WARNING]" in r.stdout
        assert "email" in r.stdout.lower()


class TestScanLargeFile:
    """6.4 — Scan a directory with a file over the size limit."""

    @pytest.mark.slow
    def test_large_file_shows_warning(self, tmp_scan_dir):
        bigfile = os.path.join(tmp_scan_dir, "bigfile.bin")
        # Create a 150MB file (sparse write for speed)
        with open(bigfile, "wb") as f:
            f.seek(150 * 1024 * 1024 - 1)
            f.write(b"\0")

        r = run("scan", tmp_scan_dir, timeout=60)
        assert "[WARNING]" in r.stdout
        assert "Large file" in r.stdout


class TestScanTodoComment:
    """6.5 — Scan a directory with a TODO comment."""

    def test_todo_shows_info(self, tmp_scan_dir):
        app = os.path.join(tmp_scan_dir, "app.js")
        with open(app, "w") as f:
            f.write("// TODO: remove hardcoded password before shipping\n")

        r = run("scan", tmp_scan_dir)
        assert "[INFO]" in r.stdout
        assert "todo" in r.stdout.lower()


class TestScanAIContextFile:
    """6.6 — Scan a directory with a CLAUDE.md file."""

    def test_claude_md_exits_1(self, tmp_scan_dir):
        claude = os.path.join(tmp_scan_dir, "CLAUDE.md")
        with open(claude, "w") as f:
            f.write("# Instructions\n")

        r = run("scan", tmp_scan_dir)
        assert r.returncode == 1
        assert "[CRITICAL]" in r.stdout
        assert "AI context file" in r.stdout


class TestScanAIContextHistory:
    """6.8 — Scan a git repo where CLAUDE.md was added then deleted."""

    def test_deleted_ai_context_detected_in_history(self, tmp_scan_dir):
        # Initialize a git repo and add/remove CLAUDE.md
        subprocess.run(
            ["git", "init"], cwd=tmp_scan_dir,
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=tmp_scan_dir,
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=tmp_scan_dir,
            capture_output=True, check=True,
        )

        claude = os.path.join(tmp_scan_dir, "CLAUDE.md")
        with open(claude, "w") as f:
            f.write("# AI Instructions\n")
        subprocess.run(
            ["git", "add", "CLAUDE.md"], cwd=tmp_scan_dir,
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add claude"], cwd=tmp_scan_dir,
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "rm", "CLAUDE.md"], cwd=tmp_scan_dir,
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "remove claude"], cwd=tmp_scan_dir,
            capture_output=True, check=True,
        )

        r = run("scan", tmp_scan_dir)
        assert r.returncode == 1
        assert "[CRITICAL]" in r.stdout
        assert "AI context file" in r.stdout
