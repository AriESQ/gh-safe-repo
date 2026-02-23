"""Tests for security_scanner.py — uses real tempfiles, no filesystem mocking."""

import os
import subprocess
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from lib.security_scanner import (
    Finding,
    FindingCategory,
    SecurityScanner,
    Severity,
    format_findings,
)


# --- Test helpers ---

class FakeConfig:
    """Minimal config that returns values from a dict."""

    def __init__(self, overrides=None):
        self._data = {
            ("pre_flight_scan", "scan_for_secrets"): "true",
            ("pre_flight_scan", "scan_for_emails"): "true",
            ("pre_flight_scan", "scan_for_todos"): "true",
            ("pre_flight_scan", "use_trufflehog"): "false",  # Always off for determinism
            ("pre_flight_scan", "max_file_size_mb"): "100",
        }
        if overrides:
            self._data.update(overrides)

    def get(self, section, key, fallback=None):
        return self._data.get((section, key), fallback)

    def getbool(self, section, key, fallback=False):
        val = self._data.get((section, key))
        if val is None:
            return fallback
        return val.lower() in ("true", "1", "yes")


def make_scanner(overrides=None):
    """Always sets use_trufflehog=false so tests are deterministic."""
    config = FakeConfig(overrides)
    return SecurityScanner(config)


def write_file(dir_path, filename, content):
    path = os.path.join(dir_path, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# --- Test classes ---

class TestLargeFileScanning:
    def test_detects_oversized_file(self):
        # max = 0.001 MB = int(0.001 * 1024 * 1024) = 1048 bytes; 1500 > 1048
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "max_file_size_mb"): "0.001"})
            path = os.path.join(tmpdir, "big.bin")
            with open(path, "wb") as f:
                f.write(b"x" * 1500)
            findings = scanner._scan_large_files(tmpdir)
        assert len(findings) == 1
        assert findings[0].category == FindingCategory.LARGE_FILE
        assert findings[0].severity == Severity.WARNING
        assert findings[0].file_path == "big.bin"
        assert findings[0].line_number == 0
        assert "MB" in findings[0].match

    def test_skips_file_under_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "small.txt", "tiny content")
            findings = scanner._scan_large_files(tmpdir)
        assert findings == []

    def test_skips_git_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "max_file_size_mb"): "0.001"})
            git_dir = os.path.join(tmpdir, ".git")
            os.makedirs(git_dir)
            path = os.path.join(git_dir, "pack.bin")
            with open(path, "wb") as f:
                f.write(b"x" * 1500)
            findings = scanner._scan_large_files(tmpdir)
        assert findings == []


class TestSecretScanning:
    def test_detects_aws_access_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "creds.txt", "key = AKIAIOSFODNN7EXAMPLE\n")
            findings = scanner._scan_regex(tmpdir, secrets=True)
        secrets = [f for f in findings if f.category == FindingCategory.SECRET]
        assert len(secrets) >= 1
        assert all(f.match == "[redacted]" for f in secrets)

    def test_detects_github_pat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "config.py", "TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz1234567890'\n")
            findings = scanner._scan_regex(tmpdir, secrets=True)
        secrets = [f for f in findings if f.category == FindingCategory.SECRET]
        assert len(secrets) >= 1
        assert all(f.match == "[redacted]" for f in secrets)

    def test_detects_private_key_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "key.pem", "-----BEGIN RSA PRIVATE KEY-----\n")
            findings = scanner._scan_regex(tmpdir, secrets=True)
        secrets = [f for f in findings if f.category == FindingCategory.SECRET]
        assert len(secrets) >= 1
        assert all(f.match == "[redacted]" for f in secrets)

    def test_detects_database_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "settings.py", "DB = 'postgres://user:password@localhost/db'\n")
            findings = scanner._scan_regex(tmpdir, secrets=True)
        secrets = [f for f in findings if f.category == FindingCategory.SECRET]
        assert len(secrets) >= 1
        assert all(f.match == "[redacted]" for f in secrets)

    def test_clean_file_produces_no_secret_findings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "scan_for_emails"): "false",
                ("pre_flight_scan", "scan_for_todos"): "false",
            })
            write_file(tmpdir, "clean.py", "x = 1\nprint('hello')\n")
            findings = scanner._scan_regex(tmpdir, secrets=True)
        assert findings == []


class TestEmailScanning:
    def test_detects_email_address(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "readme.md", "Contact: alice@example.com\n")
            findings = scanner._scan_regex(tmpdir, secrets=False)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert len(emails) >= 1
        assert emails[0].match == "alice@example.com"

    def test_email_shows_literal_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "file.txt", "Email: user@domain.org\n")
            findings = scanner._scan_regex(tmpdir, secrets=False)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert any(f.match == "user@domain.org" for f in emails)

    def test_respects_scan_for_emails_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "scan_for_emails"): "false"})
            write_file(tmpdir, "file.txt", "Email: user@domain.org\n")
            findings = scanner._scan_regex(tmpdir, secrets=False)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []


class TestTodoScanning:
    def test_detects_todo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "code.py", "# TODO: fix this\nx = 1\n")
            findings = scanner._scan_regex(tmpdir, secrets=False)
        todos = [f for f in findings if f.category == FindingCategory.TODO]
        assert len(todos) >= 1

    def test_detects_fixme(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "code.py", "# FIXME: broken\n")
            findings = scanner._scan_regex(tmpdir, secrets=False)
        todos = [f for f in findings if f.category == FindingCategory.TODO]
        assert len(todos) >= 1

    def test_detects_hack(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "code.py", "# HACK: workaround\n")
            findings = scanner._scan_regex(tmpdir, secrets=False)
        todos = [f for f in findings if f.category == FindingCategory.TODO]
        assert len(todos) >= 1

    def test_detects_xxx(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "code.py", "# XXX: bad code\n")
            findings = scanner._scan_regex(tmpdir, secrets=False)
        todos = [f for f in findings if f.category == FindingCategory.TODO]
        assert len(todos) >= 1

    def test_respects_scan_for_todos_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "scan_for_todos"): "false"})
            write_file(tmpdir, "code.py", "# TODO: ignored\n")
            findings = scanner._scan_regex(tmpdir, secrets=False)
        todos = [f for f in findings if f.category == FindingCategory.TODO]
        assert todos == []


class TestSkipBehavior:
    def test_skips_binary_extensions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            # Write a .png file with email-like content — should be skipped
            write_file(tmpdir, "image.png", "alice@example.com\n")
            findings = scanner._scan_regex(tmpdir, secrets=False)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_skips_git_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            git_dir = os.path.join(tmpdir, ".git")
            os.makedirs(git_dir)
            write_file(git_dir, "config", "alice@example.com\n")
            findings = scanner._scan_regex(tmpdir, secrets=False)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_skips_node_modules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            nm_dir = os.path.join(tmpdir, "node_modules")
            os.makedirs(nm_dir)
            write_file(nm_dir, "index.js", "alice@example.com\n")
            findings = scanner._scan_regex(tmpdir, secrets=False)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_skips_pycache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            pycache_dir = os.path.join(tmpdir, "__pycache__")
            os.makedirs(pycache_dir)
            write_file(pycache_dir, "module.pyc", "alice@example.com\n")
            findings = scanner._scan_regex(tmpdir, secrets=False)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_skips_venv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            venv_dir = os.path.join(tmpdir, ".venv")
            os.makedirs(venv_dir)
            write_file(venv_dir, "site.py", "alice@example.com\n")
            findings = scanner._scan_regex(tmpdir, secrets=False)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []


class TestTruffleHogIntegration:
    def test_file_not_found_falls_back_to_regex(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "use_trufflehog"): "true",
                ("pre_flight_scan", "scan_for_emails"): "false",
                ("pre_flight_scan", "scan_for_todos"): "false",
            })
            write_file(tmpdir, "creds.txt", "AKIAIOSFODNN7EXAMPLE\n")
            with patch("subprocess.run", side_effect=FileNotFoundError("trufflehog not found")):
                findings = scanner.scan(tmpdir)
        # Should fall back to regex and find the AWS key
        secrets = [f for f in findings if f.category == FindingCategory.SECRET]
        assert len(secrets) >= 1

    def test_trufflehog_success_still_runs_regex_for_emails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "use_trufflehog"): "true",
                ("pre_flight_scan", "scan_for_todos"): "false",
            })
            write_file(tmpdir, "file.txt", "alice@example.com\n")
            # truffleHog returns success with no findings (non-None empty list)
            scanner._try_trufflehog = MagicMock(return_value=[])
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert len(emails) >= 1


class TestFormatFindings:
    def test_empty_list_returns_empty_string(self):
        assert format_findings([]) == ""

    def test_critical_label_included(self):
        findings = [Finding(
            severity=Severity.CRITICAL,
            category=FindingCategory.SECRET,
            file_path="config.py",
            line_number=5,
            rule="AWS Access Key ID",
            match="[redacted]",
        )]
        output = format_findings(findings)
        assert "[CRITICAL]" in output

    def test_file_and_line_in_output(self):
        findings = [Finding(
            severity=Severity.WARNING,
            category=FindingCategory.EMAIL,
            file_path="readme.md",
            line_number=10,
            rule="Email address",
            match="user@example.com",
        )]
        output = format_findings(findings)
        assert "readme.md:10" in output

    def test_redacted_secret_not_shown_inline(self):
        findings = [Finding(
            severity=Severity.CRITICAL,
            category=FindingCategory.SECRET,
            file_path="creds.env",
            line_number=1,
            rule="GitHub token",
            match="[redacted]",
        )]
        output = format_findings(findings)
        # Only one line for a redacted finding — no extra match line
        lines = output.splitlines()
        assert len(lines) == 1

    def test_literal_email_shown_in_output(self):
        findings = [Finding(
            severity=Severity.WARNING,
            category=FindingCategory.EMAIL,
            file_path="docs.md",
            line_number=3,
            rule="Email address",
            match="alice@example.com",
        )]
        output = format_findings(findings)
        assert "alice@example.com" in output
