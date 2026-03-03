"""Tests for security_scanner.py — uses real tempfiles, no filesystem mocking."""

import os
import shutil
import subprocess
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from gh_safe_repo.security_scanner import (
    Finding,
    FindingCategory,
    SecurityScanner,
    Severity,
    format_findings,
    _ai_context_hint,
)


# --- Test helpers ---

class FakeConfig:
    """Minimal config that returns values from a dict."""

    def __init__(self, overrides=None):
        self._data = {
            ("pre_flight_scan", "scan_for_secrets"): "true",
            ("pre_flight_scan", "scan_for_emails"): "true",
            ("pre_flight_scan", "scan_for_todos"): "true",
            ("pre_flight_scan", "trufflehog_mode"): "off",  # Always off for determinism
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
    """Always sets trufflehog_mode=off so tests are deterministic."""
    config = FakeConfig(overrides)
    return SecurityScanner(config)


def write_file(dir_path, filename, content):
    path = os.path.join(dir_path, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def make_git_repo(tmpdir: str) -> None:
    """Initialise a throwaway git repo with a known identity."""
    subprocess.run(["git", "init", tmpdir], check=True, capture_output=True)
    subprocess.run(["git", "-C", tmpdir, "config", "user.email", "test@example.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", tmpdir, "config", "user.name", "Test"],
                   check=True, capture_output=True)


def git_add_commit(tmpdir: str, message: str) -> None:
    subprocess.run(["git", "-C", tmpdir, "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", tmpdir, "commit", "-m", message],
                   check=True, capture_output=True)


# --- Test classes ---

class TestLargeFileScanning:
    def test_detects_oversized_file(self):
        # max = 0.001 MB = int(0.001 * 1024 * 1024) = 1048 bytes; 1500 > 1048
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "max_file_size_mb"): "0.001"})
            path = os.path.join(tmpdir, "big.bin")
            with open(path, "wb") as f:
                f.write(b"x" * 1500)
            findings = scanner.scan(tmpdir)
        large = [f for f in findings if f.category == FindingCategory.LARGE_FILE]
        assert len(large) == 1
        assert large[0].severity == Severity.WARNING
        assert large[0].file_path == "big.bin"
        assert large[0].line_number == 0
        assert "MB" in large[0].match

    def test_skips_file_under_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "small.txt", "tiny content")
            findings = scanner.scan(tmpdir)
        assert findings == []

    def test_skips_git_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "max_file_size_mb"): "0.001"})
            git_dir = os.path.join(tmpdir, ".git")
            os.makedirs(git_dir)
            path = os.path.join(git_dir, "pack.bin")
            with open(path, "wb") as f:
                f.write(b"x" * 1500)
            findings = scanner.scan(tmpdir)
        large = [f for f in findings if f.category == FindingCategory.LARGE_FILE]
        assert large == []


class TestSecretScanning:
    def test_detects_aws_access_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "creds.txt", "key = AKIAIOSFODNN7EXAMPLE\n")
            findings = scanner.scan(tmpdir)
        secrets = [f for f in findings if f.category == FindingCategory.SECRET]
        assert len(secrets) >= 1
        assert all(f.match == "[redacted]" for f in secrets)

    def test_detects_github_pat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "config.py", "TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz1234567890'\n")
            findings = scanner.scan(tmpdir)
        secrets = [f for f in findings if f.category == FindingCategory.SECRET]
        assert len(secrets) >= 1
        assert all(f.match == "[redacted]" for f in secrets)

    def test_detects_private_key_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "key.pem", "-----BEGIN RSA PRIVATE KEY-----\n")
            findings = scanner.scan(tmpdir)
        secrets = [f for f in findings if f.category == FindingCategory.SECRET]
        assert len(secrets) >= 1
        assert all(f.match == "[redacted]" for f in secrets)

    def test_detects_database_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "settings.py", "DB = 'postgres://user:password@localhost/db'\n")
            findings = scanner.scan(tmpdir)
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
            findings = scanner.scan(tmpdir)
        assert findings == []


class TestEmailScanning:
    def test_detects_email_address(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "readme.md", "Contact: alice@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert len(emails) >= 1
        assert emails[0].match == "alice@example.com"

    def test_email_shows_literal_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "file.txt", "Email: user@domain.org\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert any(f.match == "user@domain.org" for f in emails)

    def test_respects_scan_for_emails_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "scan_for_emails"): "false"})
            write_file(tmpdir, "file.txt", "Email: user@domain.org\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []


class TestTodoScanning:
    def test_detects_todo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "code.py", "# TODO: fix this\nx = 1\n")
            findings = scanner.scan(tmpdir)
        todos = [f for f in findings if f.category == FindingCategory.TODO]
        assert len(todos) >= 1

    def test_detects_fixme(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "code.py", "# FIXME: broken\n")
            findings = scanner.scan(tmpdir)
        todos = [f for f in findings if f.category == FindingCategory.TODO]
        assert len(todos) >= 1

    def test_detects_hack(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "code.py", "# HACK: workaround\n")
            findings = scanner.scan(tmpdir)
        todos = [f for f in findings if f.category == FindingCategory.TODO]
        assert len(todos) >= 1

    def test_detects_xxx(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "code.py", "# XXX: bad code\n")
            findings = scanner.scan(tmpdir)
        todos = [f for f in findings if f.category == FindingCategory.TODO]
        assert len(todos) >= 1

    def test_respects_scan_for_todos_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "scan_for_todos"): "false"})
            write_file(tmpdir, "code.py", "# TODO: ignored\n")
            findings = scanner.scan(tmpdir)
        todos = [f for f in findings if f.category == FindingCategory.TODO]
        assert todos == []


class TestSkipBehavior:
    def test_skips_binary_extensions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            # Write a .png file with email-like content — should be skipped
            write_file(tmpdir, "image.png", "alice@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_skips_git_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            git_dir = os.path.join(tmpdir, ".git")
            os.makedirs(git_dir)
            write_file(git_dir, "config", "alice@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_skips_node_modules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            nm_dir = os.path.join(tmpdir, "node_modules")
            os.makedirs(nm_dir)
            write_file(nm_dir, "index.js", "alice@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_skips_pycache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            pycache_dir = os.path.join(tmpdir, "__pycache__")
            os.makedirs(pycache_dir)
            write_file(pycache_dir, "module.pyc", "alice@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_skips_venv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            venv_dir = os.path.join(tmpdir, ".venv")
            os.makedirs(venv_dir)
            write_file(venv_dir, "site.py", "alice@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []


class TestSkippedCommittedDirs:
    def test_committed_skip_dir_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            os.makedirs(os.path.join(tmpdir, "node_modules"))
            scanner.scan(tmpdir)
        assert "node_modules" in scanner.skipped_committed_dirs

    def test_git_dir_not_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            os.makedirs(os.path.join(tmpdir, ".git"))
            scanner.scan(tmpdir)
        assert ".git" not in scanner.skipped_committed_dirs

    def test_no_skipped_dirs_when_clean(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            scanner.scan(tmpdir)
        assert scanner.skipped_committed_dirs == []

    def test_nested_skip_dir_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            os.makedirs(os.path.join(tmpdir, "packages", "node_modules"))
            scanner.scan(tmpdir)
        assert any("node_modules" in d for d in scanner.skipped_committed_dirs)

    def test_skipped_dirs_sorted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            os.makedirs(os.path.join(tmpdir, "node_modules"))
            os.makedirs(os.path.join(tmpdir, "dist"))
            scanner.scan(tmpdir)
        assert scanner.skipped_committed_dirs == sorted(scanner.skipped_committed_dirs)

    def test_multiple_skip_dirs_all_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            os.makedirs(os.path.join(tmpdir, "node_modules"))
            os.makedirs(os.path.join(tmpdir, "dist"))
            scanner.scan(tmpdir)
        names = {os.path.basename(d) for d in scanner.skipped_committed_dirs}
        assert "node_modules" in names
        assert "dist" in names


class TestTruffleHogIntegration:
    def test_no_trufflehog_no_container_falls_back_to_regex(self):
        # When neither native trufflehog nor a container runtime is available,
        # the scanner falls back to regex and still finds secrets.
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "trufflehog_mode"): "auto",
                ("pre_flight_scan", "scan_for_emails"): "false",
                ("pre_flight_scan", "scan_for_todos"): "false",
            })
            write_file(tmpdir, "creds.txt", "AKIAIOSFODNN7EXAMPLE\n")
            with patch.object(scanner, "_detect_native", return_value=None):
                with patch.object(scanner, "_detect_container_runtime", return_value=None):
                    findings = scanner.scan(tmpdir)
        secrets = [f for f in findings if f.category == FindingCategory.SECRET]
        assert len(secrets) >= 1

    def test_trufflehog_success_still_runs_regex_for_emails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "trufflehog_mode"): "auto",
                ("pre_flight_scan", "scan_for_todos"): "false",
            })
            write_file(tmpdir, "file.txt", "alice@example.com\n")
            # truffleHog returns success with no findings (non-None empty list)
            scanner._try_trufflehog = MagicMock(return_value=[])
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert len(emails) >= 1

    def test_uses_git_subcommand_for_git_repo(self):
        # When the scanned directory contains a .git folder, trufflehog must
        # use the `git` subcommand so it scans full commit history.
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, ".git"))
            scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "native"})
            # Force discovery to report native trufflehog available
            scanner._discovery = {"method": "native", "version": "3.99.0"}
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with patch("subprocess.run", return_value=completed) as mock_run:
                scanner._try_trufflehog(tmpdir)
            cmd = mock_run.call_args.args[0]
        assert cmd[1] == "git"

    def test_uses_filesystem_subcommand_for_non_git_dir(self):
        # Without a .git folder the directory is not a repo; use `filesystem`.
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "native"})
            scanner._discovery = {"method": "native", "version": "3.99.0"}
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with patch("subprocess.run", return_value=completed) as mock_run:
                scanner._try_trufflehog(tmpdir)
            cmd = mock_run.call_args.args[0]
        assert cmd[1] == "filesystem"

    def test_parses_git_source_metadata(self):
        # trufflehog git emits Git metadata; the parser must handle it.
        import json as _json
        git_line = _json.dumps({
            "SourceMetadata": {"Data": {"Git": {"file": "/abs/path/secrets.txt", "line": 7}}},
            "DetectorName": "AWSKeyID",
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "native"})
            scanner._discovery = {"method": "native", "version": "3.99.0"}
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=git_line + "\n", stderr=""
            )
            with patch("subprocess.run", return_value=completed):
                findings = scanner._try_trufflehog(tmpdir)
        assert findings is not None
        assert len(findings) == 1
        assert findings[0].category == FindingCategory.SECRET
        assert findings[0].line_number == 7

    def test_version_fallback_warning_emitted_for_v2(self, capsys):
        # If truffleHog v2 is found, a warning is printed and native returns None.
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "auto"})
        v2_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="trufflehog 2.0.0\n", stderr=""
        )
        with patch("subprocess.run", return_value=v2_result):
            version = scanner._detect_native()
        assert version is None
        captured = capsys.readouterr()
        assert "v2" in captured.err or "v3 required" in captured.err

    def test_unrecognised_version_output_warns(self, capsys):
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "auto"})
        bad_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="some unexpected string\n", stderr=""
        )
        with patch("subprocess.run", return_value=bad_result):
            version = scanner._detect_native()
        assert version is None
        captured = capsys.readouterr()
        assert "Warning" in captured.err

    def test_container_runtime_detected_podman(self):
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "auto"})
        with patch("shutil.which", side_effect=lambda name: "/usr/bin/podman" if name == "podman" else None):
            result = scanner._detect_container_runtime()
        assert result is not None
        assert result[0] == "podman"
        assert result[1] == "/usr/bin/podman"

    def test_container_runtime_detected_docker_fallback(self):
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "auto"})
        with patch("shutil.which", side_effect=lambda name: "/usr/bin/docker" if name == "docker" else None):
            result = scanner._detect_container_runtime()
        assert result is not None
        assert result[0] == "docker"

    def test_container_runtime_env_override(self):
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "auto"})
        with patch.dict(os.environ, {"CONTAINER_RUNTIME": "podman"}):
            with patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name == "podman" else None):
                result = scanner._detect_container_runtime()
        assert result is not None
        assert result[0] == "podman"

    def test_no_container_runtime_returns_none(self):
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "auto"})
        with patch("shutil.which", return_value=None):
            result = scanner._detect_container_runtime()
        assert result is None

    def test_scanner_description_native(self):
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "native"})
        scanner._discovery = {"method": "native", "version": "3.93.4"}
        assert scanner.scanner_description == "truffleHog v3.93.4"

    def test_scanner_description_container_podman(self):
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "auto"})
        scanner._discovery = {"method": "container", "runtime": "podman", "runtime_path": "/usr/bin/podman"}
        assert scanner.scanner_description == "truffleHog via podman"

    def test_scanner_description_container_docker(self):
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "auto"})
        scanner._discovery = {"method": "container", "runtime": "docker", "runtime_path": "/usr/bin/docker"}
        assert scanner.scanner_description == "truffleHog via docker"

    def test_scanner_description_regex_only_mode_off(self):
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "off"})
        assert scanner.scanner_description == "regex only"

    def test_scanner_description_regex_only_with_warning(self):
        # auto mode but nothing available → "regex only — see warning above"
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "auto"})
        scanner._discovery = {"method": "none"}
        assert scanner.scanner_description == "regex only — see warning above"

    def test_scanner_description_cached(self):
        # _run_discovery() should only be called once even if scanner_description is
        # accessed multiple times.
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "off"})
        _ = scanner.scanner_description
        _ = scanner.scanner_description
        assert scanner._discovery == {"method": "none"}

    def test_container_mode_uses_volume_mount(self):
        # In container mode, the run command must include a --volume mount for the scan path.
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "docker"})
            scanner._discovery = {
                "method": "container",
                "runtime": "podman",
                "runtime_path": "/usr/bin/podman",
            }
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with patch("subprocess.run", return_value=completed) as mock_run:
                scanner._try_trufflehog(tmpdir)
            cmd = mock_run.call_args.args[0]
        assert cmd[0] == "/usr/bin/podman"
        assert "run" in cmd
        assert "--volume" in cmd

    def test_backwards_compat_use_trufflehog_false(self):
        # Old configs with use_trufflehog = false should be treated as trufflehog_mode = off.
        config = FakeConfig({
            ("pre_flight_scan", "trufflehog_mode"): "auto",   # default
            ("pre_flight_scan", "use_trufflehog"): "false",   # old override
        })
        scanner = SecurityScanner(config)
        assert scanner._trufflehog_mode == "off"


class TestBannedStringScanning:
    def test_detects_exact_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "banned_strings"): "acme"})
            write_file(tmpdir, "readme.md", "This project is by acme.\n")
            findings = scanner.scan(tmpdir)
        banned = [f for f in findings if f.category == FindingCategory.BANNED_STRING]
        assert len(banned) == 1
        assert banned[0].match == "[redacted]"

    def test_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "banned_strings"): "projectx"})
            write_file(tmpdir, "notes.txt", "Project PROJECTX internal docs.\n")
            findings = scanner.scan(tmpdir)
        banned = [f for f in findings if f.category == FindingCategory.BANNED_STRING]
        assert len(banned) == 1

    def test_multiple_strings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "banned_strings"): "acme,octocat,projectx"})
            write_file(tmpdir, "config.py", "owner = 'octocat'\n# projectx project\n")
            findings = scanner.scan(tmpdir)
        banned = [f for f in findings if f.category == FindingCategory.BANNED_STRING]
        assert len(banned) == 2  # one per matching line

    def test_no_match_produces_no_findings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "banned_strings"): "supersecret"})
            write_file(tmpdir, "clean.py", "x = 1\nprint('hello')\n")
            findings = scanner.scan(tmpdir)
        banned = [f for f in findings if f.category == FindingCategory.BANNED_STRING]
        assert banned == []

    def test_empty_banned_strings_produces_no_findings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "file.txt", "acme octocat projectx\n")
            findings = scanner.scan(tmpdir)
        banned = [f for f in findings if f.category == FindingCategory.BANNED_STRING]
        assert banned == []

    def test_rule_includes_string_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "banned_strings"): "octocat"})
            write_file(tmpdir, "file.txt", "author: octocat\n")
            findings = scanner.scan(tmpdir)
        banned = [f for f in findings if f.category == FindingCategory.BANNED_STRING]
        assert any("octocat" in f.rule for f in banned)

    def test_newline_separated_strings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "banned_strings"): "acme\noctocat"})
            write_file(tmpdir, "file.txt", "org: acme\nuser: octocat\n")
            findings = scanner.scan(tmpdir)
        banned = [f for f in findings if f.category == FindingCategory.BANNED_STRING]
        assert len(banned) == 2

    def test_trufflehog_config_generated_for_banned_strings(self):
        scanner = make_scanner({("pre_flight_scan", "banned_strings"): "acme,projectx"})
        path = scanner._build_trufflehog_config(scanner._banned_strings)
        try:
            with open(path) as f:
                content = f.read()
            assert "banned-strings" in content
            assert "acme" in content
            assert "projectx" in content
            assert "(?i)" in content
        finally:
            os.unlink(path)


class TestAiContextFileScanning:
    def test_detects_claude_md(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "CLAUDE.md", "# Claude instructions\n")
            findings = scanner.scan(tmpdir)
        ai = [f for f in findings if f.category == FindingCategory.AI_CONTEXT_FILE]
        assert len(ai) == 1
        assert ai[0].file_path == "CLAUDE.md"

    def test_detects_agents_md_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "agents.md", "# Agents\n")  # lowercase
            findings = scanner.scan(tmpdir)
        ai = [f for f in findings if f.category == FindingCategory.AI_CONTEXT_FILE]
        assert len(ai) == 1
        assert ai[0].file_path == "agents.md"

    def test_detects_cursorrules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, ".cursorrules", "# cursor rules\n")
            findings = scanner.scan(tmpdir)
        ai = [f for f in findings if f.category == FindingCategory.AI_CONTEXT_FILE]
        assert len(ai) == 1
        assert ai[0].file_path == ".cursorrules"

    def test_detects_cursor_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            os.makedirs(os.path.join(tmpdir, ".cursor"))
            findings = scanner.scan(tmpdir)
        ai = [f for f in findings if f.category == FindingCategory.AI_CONTEXT_FILE]
        assert len(ai) == 1
        assert ".cursor" in ai[0].file_path

    def test_warn_ai_context_files_false_skips_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "warn_ai_context_files"): "false",
            })
            write_file(tmpdir, "CLAUDE.md", "# Claude instructions\n")
            findings = scanner.scan(tmpdir)
        ai = [f for f in findings if f.category == FindingCategory.AI_CONTEXT_FILE]
        assert ai == []

    def test_ai_context_file_finding_is_critical_severity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "CLAUDE.md", "# Claude instructions\n")
            findings = scanner.scan(tmpdir)
        ai = [f for f in findings if f.category == FindingCategory.AI_CONTEXT_FILE]
        assert len(ai) == 1
        assert ai[0].severity == Severity.CRITICAL

    def test_ai_context_finding_message_includes_scrub_script_hint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            write_file(tmpdir, "CLAUDE.md", "# Claude instructions\n")
            findings = scanner.scan(tmpdir)
        ai = [f for f in findings if f.category == FindingCategory.AI_CONTEXT_FILE]
        assert len(ai) == 1
        assert "scrub-ai-context.sh" in ai[0].match


class TestAiContextFileHistory:
    """_check_ai_context_history() detects AI context files deleted from working tree."""

    def test_deleted_claude_md_produces_finding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "CLAUDE.md", "# Internal notes\n")
            git_add_commit(tmpdir, "add CLAUDE.md")
            os.remove(os.path.join(tmpdir, "CLAUDE.md"))
            git_add_commit(tmpdir, "remove CLAUDE.md")
            findings = make_scanner().scan(tmpdir)
        hist = [f for f in findings if f.category == FindingCategory.AI_CONTEXT_FILE]
        assert len(hist) == 1
        assert hist[0].file_path == "CLAUDE.md"
        assert hist[0].severity == Severity.CRITICAL
        assert hist[0].rule == "AI context file in git history"
        assert "scrub-ai-context.sh" in hist[0].match

    def test_present_file_not_duplicated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "CLAUDE.md", "# Notes\n")
            git_add_commit(tmpdir, "add CLAUDE.md")
            findings = make_scanner().scan(tmpdir)
        ai = [f for f in findings if f.category == FindingCategory.AI_CONTEXT_FILE]
        assert len(ai) == 1
        assert ai[0].rule == "AI context file"  # working-tree rule, not history

    def test_non_git_directory_no_history_findings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            findings = make_scanner().scan(tmpdir)
        assert not any(f.category == FindingCategory.AI_CONTEXT_FILE for f in findings)

    def test_warn_false_skips_history_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "CLAUDE.md", "# Notes\n")
            git_add_commit(tmpdir, "add")
            os.remove(os.path.join(tmpdir, "CLAUDE.md"))
            git_add_commit(tmpdir, "remove")
            findings = make_scanner(
                {("pre_flight_scan", "warn_ai_context_files"): "false"}
            ).scan(tmpdir)
        assert not any(f.category == FindingCategory.AI_CONTEXT_FILE for f in findings)

    def test_deleted_cursor_dir_produces_finding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            cursor_dir = os.path.join(tmpdir, ".cursor")
            os.makedirs(cursor_dir)
            write_file(cursor_dir, "settings.json", '{"theme":"dark"}\n')
            git_add_commit(tmpdir, "add .cursor")
            shutil.rmtree(cursor_dir)
            git_add_commit(tmpdir, "remove .cursor")
            findings = make_scanner().scan(tmpdir)
        hist = [f for f in findings if f.category == FindingCategory.AI_CONTEXT_FILE]
        assert len(hist) == 1
        assert hist[0].file_path == ".cursor"

    def test_deleted_github_copilot_instructions_produces_finding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, ".github/copilot-instructions.md", "# Copilot\n")
            git_add_commit(tmpdir, "add copilot-instructions.md")
            os.remove(os.path.join(tmpdir, ".github", "copilot-instructions.md"))
            git_add_commit(tmpdir, "remove copilot-instructions.md")
            findings = make_scanner().scan(tmpdir)
        hist = [f for f in findings if f.category == FindingCategory.AI_CONTEXT_FILE]
        assert len(hist) == 1
        assert hist[0].file_path == ".github/copilot-instructions.md"

    def test_never_committed_file_no_history_finding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "README.md", "# Project\n")
            git_add_commit(tmpdir, "initial")
            findings = make_scanner().scan(tmpdir)
        assert not any(f.category == FindingCategory.AI_CONTEXT_FILE for f in findings)


class TestExcludePaths:
    def test_excluded_file_suppresses_email(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "scan_exclude_paths"): r"docs/",
            })
            write_file(tmpdir, "docs/api.json", "contact: alice@example.com\n")
            findings = scanner.scan(tmpdir)
        assert findings == []

    def test_excluded_file_suppresses_secrets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "scan_exclude_paths"): r"fixtures/",
            })
            write_file(tmpdir, "fixtures/creds.txt", "key = AKIAIOSFODNN7EXAMPLE\n")
            findings = scanner.scan(tmpdir)
        secrets = [f for f in findings if f.category == FindingCategory.SECRET]
        assert secrets == []

    def test_excluded_file_suppresses_ai_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "scan_exclude_paths"): r"CLAUDE\.md",
            })
            write_file(tmpdir, "CLAUDE.md", "# internal notes\n")
            findings = scanner.scan(tmpdir)
        ai = [f for f in findings if f.category == FindingCategory.AI_CONTEXT_FILE]
        assert ai == []

    def test_excluded_cursor_dir_suppressed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "scan_exclude_paths"): r"\.cursor",
            })
            os.makedirs(os.path.join(tmpdir, ".cursor"))
            findings = scanner.scan(tmpdir)
        ai = [f for f in findings if f.category == FindingCategory.AI_CONTEXT_FILE]
        assert ai == []

    def test_excluded_path_does_not_suppress_other_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "scan_exclude_paths"): r"docs/",
                ("pre_flight_scan", "scan_for_todos"): "false",
                ("pre_flight_scan", "scan_for_secrets"): "false",
            })
            write_file(tmpdir, "docs/api.json", "contact: alice@example.com\n")
            write_file(tmpdir, "src/main.py", "contact: bob@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert len(emails) == 1
        assert emails[0].file_path == "src/main.py"

    def test_multiple_exclude_patterns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "scan_exclude_paths"): r"docs/, tests/",
            })
            write_file(tmpdir, "docs/api.json", "alice@example.com\n")
            write_file(tmpdir, "tests/test_foo.py", "alice@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_excluded_path_suppresses_ai_context_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "CLAUDE.md", "# notes\n")
            git_add_commit(tmpdir, "add")
            os.remove(os.path.join(tmpdir, "CLAUDE.md"))
            git_add_commit(tmpdir, "remove")
            scanner = make_scanner({
                ("pre_flight_scan", "scan_exclude_paths"): r"CLAUDE\.md",
            })
            findings = scanner.scan(tmpdir)
        ai = [f for f in findings if f.category == FindingCategory.AI_CONTEXT_FILE]
        assert ai == []

    def test_exclude_paths_written_to_trufflehog_temp_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "trufflehog_mode"): "native",
                ("pre_flight_scan", "scan_exclude_paths"): r"docs/api\.json",
            })
            scanner._discovery = {"method": "native", "version": "3.99.0"}
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with patch("subprocess.run", return_value=completed) as mock_run:
                scanner._try_trufflehog(tmpdir)
            cmd = mock_run.call_args.args[0]
        assert "--exclude-paths" in cmd
        idx = cmd.index("--exclude-paths")
        exclude_file = cmd[idx + 1]
        # The temp file is deleted after the call; check the arg was present
        assert exclude_file.endswith(".txt")

    def test_container_mode_mounts_exclude_paths_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "trufflehog_mode"): "docker",
                ("pre_flight_scan", "scan_exclude_paths"): r"docs/",
            })
            scanner._discovery = {
                "method": "container",
                "runtime": "podman",
                "runtime_path": "/usr/bin/podman",
            }
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with patch("subprocess.run", return_value=completed) as mock_run:
                scanner._try_trufflehog(tmpdir)
            cmd = mock_run.call_args.args[0]
        assert "--exclude-paths" in cmd
        assert cmd.count("--volume") >= 2  # scan path + exclude file


class TestEmailIgnoreDomains:
    def test_ignored_domain_suppressed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "email_ignore_domains"): "example.com",
            })
            write_file(tmpdir, "readme.md", "Contact: alice@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_non_ignored_domain_still_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "email_ignore_domains"): "example.com",
            })
            write_file(tmpdir, "readme.md", "Contact: alice@real-corp.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert len(emails) == 1
        assert emails[0].match == "alice@real-corp.com"

    def test_multiple_ignored_domains(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "email_ignore_domains"): "example.com, domain.tld",
            })
            write_file(tmpdir, "readme.md",
                       "alice@example.com\nuser@domain.tld\nbob@real.io\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert len(emails) == 1
        assert emails[0].match == "bob@real.io"

    def test_domain_match_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "email_ignore_domains"): "Example.COM",
            })
            write_file(tmpdir, "readme.md", "Contact: alice@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_domain_ignore_does_not_suppress_other_categories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "email_ignore_domains"): "example.com",
            })
            write_file(tmpdir, "creds.txt", "key = AKIAIOSFODNN7EXAMPLE\n")
            findings = scanner.scan(tmpdir)
        secrets = [f for f in findings if f.category == FindingCategory.SECRET]
        assert len(secrets) >= 1


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
