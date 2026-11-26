"""Tests for security_scanner.py — uses real tempfiles, no filesystem mocking."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from gh_safe_repo.security_scanner import (
    Finding,
    FindingCategory,
    SecurityScanner,
    Severity,
    format_findings,
    _ai_context_hint,
    _trufflehog_scan_error,
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
    # Isolate from the developer's global config: a global gitignore listing
    # node_modules/ would leave nothing to commit, and commit signing could
    # block on a hardware key.
    subprocess.run(["git", "-C", tmpdir, "config", "core.excludesFile", os.devnull],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", tmpdir, "config", "commit.gpgsign", "false"],
                   check=True, capture_output=True)


def git_add_commit(tmpdir: str, message: str) -> None:
    subprocess.run(["git", "-C", tmpdir, "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", tmpdir, "commit", "-m", message],
                   check=True, capture_output=True)


def mock_popen(stdout="", returncode=0):
    """Create a mock Popen context for _try_trufflehog tests.

    Returns (patcher, mock_cls) where mock_cls.call_args gives the
    command that was passed, matching the old subprocess.run test pattern.
    """
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (stdout, "")
    mock_proc.returncode = returncode
    mock_cls = MagicMock(return_value=mock_proc)
    return patch("subprocess.Popen", mock_cls)


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
            write_file(tmpdir, "readme.md", "Contact: example@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert len(emails) >= 1
        assert emails[0].match == "example@example.com"

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
            write_file(tmpdir, "image.png", "example@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_skips_git_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            git_dir = os.path.join(tmpdir, ".git")
            os.makedirs(git_dir)
            write_file(git_dir, "config", "example@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_skips_node_modules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            nm_dir = os.path.join(tmpdir, "node_modules")
            os.makedirs(nm_dir)
            write_file(nm_dir, "index.js", "example@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_skips_pycache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            pycache_dir = os.path.join(tmpdir, "__pycache__")
            os.makedirs(pycache_dir)
            write_file(pycache_dir, "module.pyc", "example@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_skips_venv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner()
            venv_dir = os.path.join(tmpdir, ".venv")
            os.makedirs(venv_dir)
            write_file(venv_dir, "site.py", "example@example.com\n")
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
            write_file(tmpdir, "file.txt", "example@example.com\n")
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
            with mock_popen() as mock_cls:
                scanner._try_trufflehog(tmpdir)
            cmd = mock_cls.call_args.args[0]
        assert cmd[1] == "git"

    def test_uses_filesystem_subcommand_for_non_git_dir(self):
        # Without a .git folder the directory is not a repo; use `filesystem`.
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "native"})
            scanner._discovery = {"method": "native", "version": "3.99.0"}
            with mock_popen() as mock_cls:
                scanner._try_trufflehog(tmpdir)
            cmd = mock_cls.call_args.args[0]
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
            with mock_popen(stdout=git_line + "\n"):
                findings = scanner._try_trufflehog(tmpdir)
        assert findings is not None
        assert len(findings) == 1
        assert findings[0].category == FindingCategory.SECRET
        assert findings[0].line_number == 7

    def test_relative_file_path_from_git_mode(self):
        # trufflehog git mode may emit relative paths (no leading /);
        # these must be used as-is, not mangled by os.path.relpath().
        import json as _json
        git_line = _json.dumps({
            "SourceMetadata": {"Data": {"Git": {"file": "secrets.txt", "line": 3}}},
            "DetectorName": "Generic",
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "native"})
            scanner._discovery = {"method": "native", "version": "3.99.0"}
            with mock_popen(stdout=git_line + "\n"):
                findings = scanner._try_trufflehog(tmpdir)
        assert findings is not None
        assert len(findings) == 1
        assert findings[0].file_path == "secrets.txt"

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
            with mock_popen() as mock_cls:
                scanner._try_trufflehog(tmpdir)
            cmd = mock_cls.call_args.args[0]
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


class TestEngineUsed:
    """scan() records the engine that actually ran, not the plan-time guess (#77)."""

    def _scan(self, mode, tmpdir, *, native=None, runtime=None, popen_rc=0,
              popen_missing=False, overrides=None):
        cfg = {("pre_flight_scan", "trufflehog_mode"): mode}
        cfg.update(overrides or {})
        scanner = make_scanner(cfg)
        popen_kwargs = (
            {"side_effect": FileNotFoundError()} if popen_missing
            else {"return_value": MagicMock(
                communicate=MagicMock(return_value=("", "")), returncode=popen_rc
            )}
        )
        with patch.object(scanner, "_detect_native", return_value=native), \
             patch.object(scanner, "_detect_container_runtime", return_value=runtime), \
             patch("subprocess.Popen", **popen_kwargs) as mock_popen:
            scanner.scan(tmpdir)
        return scanner, mock_popen

    def test_native_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner, _ = self._scan("auto", tmpdir, native="3.93.4")
        assert scanner.engine_used == "trufflehog-native"
        assert scanner.fell_back is False
        assert scanner.engine_error() is None
        assert scanner.engine_summary == "truffleHog v3.93.4"

    def test_container_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner, _ = self._scan("docker", tmpdir, runtime=("podman", "/usr/bin/podman"))
        assert scanner.engine_used == "trufflehog-container"
        assert scanner.fell_back is False
        assert scanner.engine_error() is None
        assert scanner.engine_report() == {
            "configured": "docker", "used": "trufflehog-container", "fell_back": False, "error": None,
        }

    def test_off_is_regex_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner, mock_popen = self._scan("off", tmpdir)
        mock_popen.assert_not_called()
        assert scanner.engine_used == "regex"
        assert scanner.fell_back is False
        assert scanner.engine_error() is None

    def test_legacy_use_trufflehog_false_is_off(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner, mock_popen = self._scan(
                "auto", tmpdir, overrides={("pre_flight_scan", "use_trufflehog"): "false"}
            )
        mock_popen.assert_not_called()
        assert scanner.engine_report() == {
            "configured": "off", "used": "regex", "fell_back": False, "error": None,
        }

    def test_secret_scanning_disabled_is_not_a_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner, mock_popen = self._scan(
                "native", tmpdir, overrides={("pre_flight_scan", "scan_for_secrets"): "false"}
            )
        mock_popen.assert_not_called()
        assert scanner.engine_used == "regex"
        assert scanner.fell_back is False
        assert scanner.engine_error() is None

    def test_auto_nothing_installed_falls_back_quietly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner, _ = self._scan("auto", tmpdir)
        assert scanner.engine_used == "regex"
        assert scanner.fell_back is True
        assert "not found" in scanner.fallback_reason
        # auto never fails on a fallback
        assert scanner.engine_error() is None

    def test_auto_runtime_failure_falls_back_quietly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner, _ = self._scan("auto", tmpdir, native="3.93.4", popen_rc=2)
        assert scanner.engine_used == "regex"
        assert scanner.fell_back is True
        assert "exited with status 2" in scanner.fallback_reason
        assert scanner.engine_error() is None
        assert "fell back" in scanner.engine_summary

    def test_native_missing_is_an_error(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner, _ = self._scan("native", tmpdir)
        assert scanner.engine_used == "regex"
        assert scanner.fell_back is True
        msg = scanner.engine_error()
        assert msg is not None
        assert "trufflehog_mode = native" in msg
        assert "not found on PATH" in msg
        # No contradictory "falling back" warning in an explicit mode.
        assert "falling back" not in capsys.readouterr().err

    def test_native_runtime_failure_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner, _ = self._scan("native", tmpdir, native="3.93.4", popen_rc=137)
        msg = scanner.engine_error()
        assert msg is not None
        assert "exited with status 137" in msg

    def test_native_binary_vanished_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner, _ = self._scan("native", tmpdir, native="3.93.4", popen_missing=True)
        assert scanner.fell_back is True
        assert "not found" in scanner.engine_error()

    def test_docker_without_runtime_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner, _ = self._scan("docker", tmpdir)
        msg = scanner.engine_error()
        assert "trufflehog_mode = docker" in msg
        assert "no container runtime" in msg
        assert scanner.engine_report() == {
            "configured": "docker", "used": "regex", "fell_back": True, "error": msg,
        }

    def test_docker_container_failure_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner, _ = self._scan(
                "docker", tmpdir, runtime=("podman", "/usr/bin/podman"), popen_rc=125
            )
        assert "truffleHog via podman exited with status 125" in scanner.engine_error()

    def test_native_wrong_version_reason_kept(self):
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "native"})
        v2 = subprocess.CompletedProcess(args=[], returncode=0, stdout="trufflehog 2.0.0\n", stderr="")
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("subprocess.run", return_value=v2):
                scanner.scan(tmpdir)
        assert "v3 required" in scanner.engine_error()

    def test_unrecognised_mode_is_strict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner, mock_popen = self._scan("dokcer", tmpdir)
        mock_popen.assert_not_called()
        assert "unrecognised trufflehog_mode 'dokcer'" in scanner.engine_error()

    def test_mode_is_case_insensitive(self):
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): " Native "})
        assert scanner.trufflehog_mode == "native"

    def test_strict_description_warns_scan_will_fail(self):
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "native"})
        with patch.object(scanner, "_detect_native", return_value=None):
            desc = scanner.scanner_description
        assert "scan will fail" in desc

    def test_rescan_resets_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner, _ = self._scan("auto", tmpdir)
            assert scanner.fell_back is True
            scanner._trufflehog_mode = "off"
            scanner.scan(tmpdir)
        assert scanner.fell_back is False
        assert scanner.fallback_reason == ""


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
            write_file(tmpdir, "docs/api.json", "contact: example@example.com\n")
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
            write_file(tmpdir, "docs/api.json", "contact: example@example.com\n")
            write_file(tmpdir, "src/main.py", "contact: example@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert len(emails) == 1
        assert emails[0].file_path == "src/main.py"

    def test_multiple_exclude_patterns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "scan_exclude_paths"): r"docs/, tests/",
            })
            write_file(tmpdir, "docs/api.json", "example@example.com\n")
            write_file(tmpdir, "tests/test_foo.py", "example@example.com\n")
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
            with mock_popen() as mock_cls:
                scanner._try_trufflehog(tmpdir)
            cmd = mock_cls.call_args.args[0]
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
            with mock_popen() as mock_cls:
                scanner._try_trufflehog(tmpdir)
            cmd = mock_cls.call_args.args[0]
        assert "--exclude-paths" in cmd
        assert cmd.count("--volume") >= 2  # scan path + exclude file


class TestExcludeEmails:
    def test_domain_exclusion_suppressed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "exclude_emails"): "@example.com",
            })
            write_file(tmpdir, "readme.md", "Contact: example@example.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_exact_address_exclusion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "exclude_emails"): "action@github.com",
            })
            write_file(tmpdir, "readme.md", "action@github.com\nother@github.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert len(emails) == 1
        assert emails[0].match == "other@github.com"

    def test_non_excluded_domain_still_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "exclude_emails"): "@example.com",
            })
            write_file(tmpdir, "readme.md", "Contact: alice@real-corp.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert len(emails) == 1
        assert emails[0].match == "alice@real-corp.com"

    def test_mixed_domain_and_address_exclusion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "exclude_emails"): "action@github.com, @example.com, @domain.tld",
            })
            write_file(tmpdir, "readme.md",
                       "action@github.com\nexample@example.com\nuser@domain.tld\nbob@real.io\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert len(emails) == 1
        assert emails[0].match == "bob@real.io"

    def test_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "exclude_emails"): "@Example.COM, Action@GitHub.com",
            })
            write_file(tmpdir, "readme.md", "example@example.com\naction@github.com\n")
            findings = scanner.scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert emails == []

    def test_exclusion_does_not_suppress_other_categories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({
                ("pre_flight_scan", "exclude_emails"): "@example.com",
            })
            write_file(tmpdir, "creds.txt", "key = AKIAIOSFODNN7EXAMPLE\n")
            findings = scanner.scan(tmpdir)
        secrets = [f for f in findings if f.category == FindingCategory.SECRET]
        assert len(secrets) >= 1


class TestEmailHistoryScanning:
    """Tests for _check_email_history() using real git repos."""

    def test_email_in_history_produces_finding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "readme.md", "Contact: example@example.com\n")
            git_add_commit(tmpdir, "add readme")
            scanner = make_scanner()
            findings = scanner.scan(tmpdir)
        history = [f for f in findings if f.rule == "Email address in git history"]
        assert len(history) >= 1
        assert history[0].match == "example@example.com"
        assert history[0].commit  # non-empty short hash
        assert history[0].timestamp  # non-empty ISO timestamp

    def test_deleted_email_still_found_in_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "secret.txt", "example@example.com\n")
            git_add_commit(tmpdir, "add secret")
            write_file(tmpdir, "secret.txt", "cleaned\n")
            git_add_commit(tmpdir, "clean up")
            scanner = make_scanner()
            findings = scanner.scan(tmpdir)
        history = [f for f in findings if f.rule == "Email address in git history"]
        assert any(f.match == "example@example.com" for f in history)

    def test_exclude_emails_domain_applies_to_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "readme.md", "example@example.com\n")
            git_add_commit(tmpdir, "add readme")
            scanner = make_scanner({
                ("pre_flight_scan", "exclude_emails"): "@example.com",
            })
            findings = scanner.scan(tmpdir)
        history = [f for f in findings if f.rule == "Email address in git history"]
        assert history == []

    def test_exclude_emails_exact_applies_to_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "readme.md", "action@github.com\nreal@github.com\n")
            git_add_commit(tmpdir, "add readme")
            scanner = make_scanner({
                ("pre_flight_scan", "exclude_emails"): "action@github.com",
            })
            findings = scanner.scan(tmpdir)
        history = [f for f in findings if f.rule == "Email address in git history"]
        emails = [f.match for f in history]
        assert "action@github.com" not in emails
        assert "real@github.com" in emails

    def test_scan_exclude_paths_applies_to_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "vendor/lib.txt", "example@example.com\n")
            git_add_commit(tmpdir, "add vendor")
            scanner = make_scanner({
                ("pre_flight_scan", "scan_exclude_paths"): "vendor/",
            })
            findings = scanner.scan(tmpdir)
        history = [f for f in findings if f.rule == "Email address in git history"]
        assert history == []

    def test_deduplication_same_email_same_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "readme.md", "example@example.com\n")
            git_add_commit(tmpdir, "commit 1")
            write_file(tmpdir, "readme.md", "updated example@example.com\n")
            git_add_commit(tmpdir, "commit 2")
            scanner = make_scanner()
            findings = scanner.scan(tmpdir)
        history = [f for f in findings if f.rule == "Email address in git history"]
        matches = [f for f in history if f.match == "example@example.com" and f.file_path == "readme.md"]
        assert len(matches) == 1

    def test_same_email_different_files_separate_findings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "a.txt", "example@example.com\n")
            write_file(tmpdir, "b.txt", "example@example.com\n")
            git_add_commit(tmpdir, "add both")
            scanner = make_scanner()
            findings = scanner.scan(tmpdir)
        history = [f for f in findings
                   if f.rule == "Email address in git history" and f.match == "example@example.com"]
        files = {f.file_path for f in history}
        assert "a.txt" in files
        assert "b.txt" in files

    def test_non_git_repo_no_history_findings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_file(tmpdir, "readme.md", "example@example.com\n")
            scanner = make_scanner()
            findings = scanner.scan(tmpdir)
        history = [f for f in findings if f.rule == "Email address in git history"]
        assert history == []

    def test_scan_for_emails_false_skips_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "readme.md", "example@example.com\n")
            git_add_commit(tmpdir, "add")
            scanner = make_scanner({
                ("pre_flight_scan", "scan_for_emails"): "false",
            })
            findings = scanner.scan(tmpdir)
        history = [f for f in findings if f.rule == "Email address in git history"]
        assert history == []

    def test_scan_email_history_false_skips_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "readme.md", "example@example.com\n")
            git_add_commit(tmpdir, "add")
            scanner = make_scanner({
                ("pre_flight_scan", "scan_email_history"): "false",
            })
            findings = scanner.scan(tmpdir)
        history = [f for f in findings if f.rule == "Email address in git history"]
        assert history == []

    def test_finding_has_warning_severity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "readme.md", "example@example.com\n")
            git_add_commit(tmpdir, "add")
            scanner = make_scanner()
            findings = scanner.scan(tmpdir)
        history = [f for f in findings if f.rule == "Email address in git history"]
        assert all(f.severity == Severity.WARNING for f in history)


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
            match="example@example.com",
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
        # Two lines: header + view hint, no match line for redacted
        lines = output.splitlines()
        assert len(lines) == 2
        assert "[redacted]" not in output

    def test_literal_email_shown_in_output(self):
        findings = [Finding(
            severity=Severity.WARNING,
            category=FindingCategory.EMAIL,
            file_path="docs.md",
            line_number=3,
            rule="Email address",
            match="example@example.com",
        )]
        output = format_findings(findings)
        assert "example@example.com" in output

    def test_view_hint_for_trufflehog_finding(self):
        findings = [Finding(
            severity=Severity.CRITICAL,
            category=FindingCategory.SECRET,
            file_path="github_stars.json",
            line_number=13819,
            rule="Secret detected by truffleHog (Box)",
            match="[redacted]",
            commit="7a4e0375",
            timestamp="2026-03-27 23:21:01 +0000",
        )]
        output = format_findings(findings)
        assert "View with: git show 7a4e0375:github_stars.json | sed -n '13815,13823p'" in output

    def test_view_hint_for_regex_finding(self):
        findings = [Finding(
            severity=Severity.CRITICAL,
            category=FindingCategory.SECRET,
            file_path="config.py",
            line_number=5,
            rule="AWS Access Key ID",
            match="[redacted]",
        )]
        output = format_findings(findings)
        assert "View with: sed -n '1,9p' config.py" in output

    def test_no_view_hint_when_no_line_number(self):
        findings = [Finding(
            severity=Severity.WARNING,
            category=FindingCategory.LARGE_FILE,
            file_path="big.bin",
            line_number=0,
            rule="Large file",
            match="15 MB",
        )]
        output = format_findings(findings)
        assert "View with:" not in output

    def test_view_hint_quotes_spaces_in_filename(self):
        findings = [Finding(
            severity=Severity.CRITICAL,
            category=FindingCategory.SECRET,
            file_path="my secrets/creds file.env",
            line_number=10,
            rule="AWS Access Key ID",
            match="[redacted]",
        )]
        output = format_findings(findings)
        assert "sed -n '6,14p' 'my secrets/creds file.env'" in output

    def test_view_hint_quotes_single_quotes_in_filename(self):
        findings = [Finding(
            severity=Severity.CRITICAL,
            category=FindingCategory.SECRET,
            file_path="it's a file.txt",
            line_number=1,
            rule="AWS Access Key ID",
            match="[redacted]",
        )]
        output = format_findings(findings)
        hint_line = [l for l in output.splitlines() if "View with:" in l][0]
        # The hint must not contain an unquoted single quote that would break shell parsing
        assert "it's a file.txt" not in hint_line
        # shlex.quote wraps the path so the quote is escaped
        assert "View with: sed" in hint_line

    def test_view_hint_quotes_shell_metacharacters(self):
        findings = [Finding(
            severity=Severity.CRITICAL,
            category=FindingCategory.SECRET,
            file_path="$(rm -rf /).txt",
            line_number=5,
            rule="AWS Access Key ID",
            match="[redacted]",
        )]
        output = format_findings(findings)
        hint_line = [l for l in output.splitlines() if "View with:" in l][0]
        # The $() must be quoted so it won't execute
        assert "$(rm -rf /)" not in hint_line or "'$(rm -rf /).txt'" in hint_line

    def test_view_hint_for_git_history_email_no_line_number(self):
        findings = [Finding(
            severity=Severity.WARNING,
            category=FindingCategory.EMAIL,
            file_path="public/github_stars.json",
            line_number=0,
            rule="Email address in git history",
            match="example@example.com",
            commit="c216887",
            timestamp="2026-03-27T19:21:01-04:00",
        )]
        output = format_findings(findings)
        assert "View with: git show c216887:public/github_stars.json" in output
        # No sed since there's no line number
        assert "sed" not in output

    def test_view_hint_quotes_trufflehog_path_with_spaces(self):
        findings = [Finding(
            severity=Severity.CRITICAL,
            category=FindingCategory.SECRET,
            file_path="src/my config.json",
            line_number=20,
            rule="Secret detected by truffleHog (AWS)",
            match="[redacted]",
            commit="abcd1234",
        )]
        output = format_findings(findings)
        assert "git show 'abcd1234:src/my config.json'" in output


class TestSkipDirsGitAware:
    """SKIP_DIRS that contain tracked git files should be scanned, not skipped."""

    def test_committed_skip_dir_is_scanned(self):
        """node_modules/ committed to git → scanner finds banned string inside it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            scanner = make_scanner({
                ("pre_flight_scan", "banned_strings"): "SUPERSECRET_IN_MODULES",
                ("pre_flight_scan", "scan_for_emails"): "false",
                ("pre_flight_scan", "scan_for_todos"): "false",
                ("pre_flight_scan", "scan_for_secrets"): "false",
            })
            write_file(tmpdir, "node_modules/lib.js", "var x = 'SUPERSECRET_IN_MODULES';\n")
            git_add_commit(tmpdir, "add node_modules")
            findings = scanner.scan(tmpdir)
        banned = [f for f in findings if f.category == FindingCategory.BANNED_STRING]
        assert len(banned) == 1
        assert "node_modules" in banned[0].file_path
        assert scanner.skipped_committed_dirs == []

    def test_uncommitted_skip_dir_in_git_repo_is_skipped(self):
        """node_modules/ present in working tree but not committed → still skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            # Initial commit so it's a valid repo
            write_file(tmpdir, "README.md", "hello\n")
            git_add_commit(tmpdir, "initial")
            scanner = make_scanner({
                ("pre_flight_scan", "banned_strings"): "SUPERSECRET_IN_MODULES",
                ("pre_flight_scan", "scan_for_emails"): "false",
                ("pre_flight_scan", "scan_for_todos"): "false",
                ("pre_flight_scan", "scan_for_secrets"): "false",
            })
            # Create node_modules in working tree WITHOUT committing
            write_file(tmpdir, "node_modules/lib.js", "var x = 'SUPERSECRET_IN_MODULES';\n")
            findings = scanner.scan(tmpdir)
        banned = [f for f in findings if f.category == FindingCategory.BANNED_STRING]
        assert banned == []
        assert any("node_modules" in d for d in scanner.skipped_committed_dirs)

    def test_committed_skip_dir_is_scanned_nested(self):
        """src/dist/ committed to git → scanner finds content inside it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            scanner = make_scanner({
                ("pre_flight_scan", "banned_strings"): "BUILT_ARTIFACT_SECRET",
                ("pre_flight_scan", "scan_for_emails"): "false",
                ("pre_flight_scan", "scan_for_todos"): "false",
                ("pre_flight_scan", "scan_for_secrets"): "false",
            })
            write_file(tmpdir, "src/dist/bundle.js", "// BUILT_ARTIFACT_SECRET\n")
            git_add_commit(tmpdir, "add dist")
            findings = scanner.scan(tmpdir)
        banned = [f for f in findings if f.category == FindingCategory.BANNED_STRING]
        assert len(banned) == 1
        assert "dist" in banned[0].file_path


class TestScanScope:
    """Issue #84: in a git repo, scan only what gets pushed."""

    def test_git_repo_skips_ignored_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, ".gitignore", "build/\nlocal.txt\n")
            write_file(tmpdir, "app.py", "x = 1\n")
            git_add_commit(tmpdir, "init")
            write_file(tmpdir, "build/out.txt", "leak@example.org\n")
            write_file(tmpdir, "local.txt", "leak@example.org\n")
            findings = make_scanner().scan(tmpdir)
        assert [f for f in findings if f.category == FindingCategory.EMAIL] == []

    def test_git_repo_scans_tracked_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "src/app.py", "leak@example.org\n")
            git_add_commit(tmpdir, "init")
            findings = make_scanner().scan(tmpdir)
        emails = [f for f in findings if f.category == FindingCategory.EMAIL]
        assert {f.file_path for f in emails if f.line_number} == {"src/app.py"}

    def test_non_git_dir_scans_everything(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_file(tmpdir, "notes/out.txt", "leak@example.org\n")
            findings = make_scanner().scan(tmpdir)
        assert any(f.category == FindingCategory.EMAIL for f in findings)

    def test_nul_byte_file_not_content_scanned(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "project.toe"), "wb") as f:
                f.write(b"\x00\x01leak@example.org\n")
            findings = make_scanner().scan(tmpdir)
        assert [f for f in findings if f.category == FindingCategory.EMAIL] == []


class TestProgress:
    def test_no_progress_when_stderr_not_tty(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_file(tmpdir, "a.py", "x = 1\n")
            make_scanner().scan(tmpdir)
        assert "Scanning files" not in capsys.readouterr().err

    def test_progress_disabled_by_flag(self):
        with patch("sys.stderr.isatty", return_value=True):
            assert SecurityScanner(FakeConfig(), progress=False)._progress is False

    def test_progress_shows_count_and_path(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "a.py", "x = 1\n")
            git_add_commit(tmpdir, "init")
            scanner = make_scanner()
            scanner._progress = True
            scanner.scan(tmpdir)
        err = capsys.readouterr().err
        assert "Scanning files: 1/1 (100%)  a.py" in err
        assert err.endswith("\r\033[K")


# The line truffleHog logged for a repository with no commits (#134).
INDEX_ERROR_LINE = json.dumps({
    "level": "error", "ts": "2026-11-01T00:00:00Z", "logger": "trufflehog",
    "msg": "error running scan",
    "error": ("failed to scan Git: error preparing repo: failed to read index "
              "file: open /src/.git/index: no such file or directory"),
})


def mock_popen_with_stderr(stderr_text, stdout="", returncode=0, only=None):
    """Like mock_popen(), but the fake child writes stderr_text to the
    stderr file descriptor it was handed, as a real child would.

    With `only`, just commands whose argv[0] is `only` are faked; every
    other command (the scanner's own git calls) runs for real."""
    real_popen = subprocess.Popen

    def fake_popen(cmd, *args, **kwargs):
        if only and cmd[0] != only:
            return real_popen(cmd, *args, **kwargs)
        os.write(kwargs["stderr"], stderr_text.encode())
        proc = MagicMock()
        proc.communicate.return_value = (stdout, None)
        proc.returncode = returncode
        return proc
    return patch("subprocess.Popen", side_effect=fake_popen)


class TestTrufflehogScanError:
    """A truffleHog run that logs a scan error did not scan (#134)."""

    def test_error_running_scan_with_exit_0(self):
        reason = _trufflehog_scan_error(INDEX_ERROR_LINE + "\n", 0, False)
        assert reason.startswith("failed to scan Git")
        assert "failed to read index file" in reason

    def test_error_running_scan_with_exit_1(self):
        assert _trufflehog_scan_error(INDEX_ERROR_LINE, 1, False)

    def test_fatal_level_counts(self):
        line = json.dumps({"level": "fatal", "msg": "error running scan", "error": "boom"})
        assert _trufflehog_scan_error(line, 0, False) == "boom"

    def test_other_error_with_exit_1_and_no_findings(self):
        line = json.dumps({"level": "error", "msg": "error creating engine", "error": "bad"})
        assert _trufflehog_scan_error(line, 1, False) == "bad"

    def test_other_error_does_not_fail_a_completed_scan(self):
        line = json.dumps({"level": "error", "msg": "error verifying", "error": "timeout"})
        # Exit 0: the scan finished. Exit 1 with findings: it found things.
        assert _trufflehog_scan_error(line, 0, False) is None
        assert _trufflehog_scan_error(line, 1, True) is None

    def test_info_lines_and_non_json_are_ignored(self):
        stderr = "\n".join([
            "Trying to pull ghcr.io/trufflesecurity/trufflehog:latest...",
            json.dumps({"level": "info-0", "msg": "running source"}),
            '{"level": "error", truncated',
            "[1, 2]",
            "",
        ])
        assert _trufflehog_scan_error(stderr, 0, False) is None
        assert _trufflehog_scan_error(stderr, 1, False) is None
        assert _trufflehog_scan_error("", 1, False) is None

    def test_reason_is_printable_and_capped(self):
        line = json.dumps({"level": "error", "msg": "error running scan",
                           "error": "bad\x1b[31m path " + "x" * 500})
        reason = _trufflehog_scan_error(line, 0, False)
        assert "\x1b" not in reason
        assert len(reason) <= 300

    @pytest.mark.parametrize("returncode", [0, 1])
    def test_try_trufflehog_returns_none_not_empty(self, returncode, capsys):
        """None = failed (fall back to regex); [] would mean "scanned, clean"."""
        scanner = make_scanner()
        scanner._discovery = {"method": "native", "version": "3.93.4"}
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock_popen_with_stderr(INDEX_ERROR_LINE + "\n", returncode=returncode):
            assert scanner._try_trufflehog(tmpdir) is None
        assert "failed to read index file" in scanner._trufflehog_failure
        # Still shown to the user as it arrives.
        assert "error running scan" in capsys.readouterr().err

    def test_clean_run_still_returns_empty_list(self):
        scanner = make_scanner()
        scanner._discovery = {"method": "native", "version": "3.93.4"}
        info_line = json.dumps({"level": "info-0", "msg": "finished scanning"})
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock_popen_with_stderr(info_line + "\n"):
            assert scanner._try_trufflehog(tmpdir) == []

    def test_scan_of_repo_without_commits_falls_back_to_regex(self):
        """End to end on a real `git init`-ed directory: the secret scan is
        reported as a regex fallback, never as a clean truffleHog run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            make_git_repo(tmpdir)
            write_file(tmpdir, "config.py", "key = 'AKIAIOSFODNN7EXAMPLE'\n")
            subprocess.run(["git", "-C", tmpdir, "add", "-A"], check=True,
                           capture_output=True)
            scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "auto"})
            scanner._discovery = {"method": "native", "version": "3.93.4"}
            with mock_popen_with_stderr(INDEX_ERROR_LINE + "\n", only="trufflehog"):
                findings = scanner.scan(tmpdir)
        assert scanner.fell_back is True
        assert scanner.engine_used == "regex"
        assert "failed to read index file" in scanner.fallback_reason
        assert "fell back from truffleHog" in scanner.engine_summary
        # The staged file was still scanned by the regex engine.
        assert any(f.category == FindingCategory.SECRET for f in findings)

    def test_explicit_mode_reports_engine_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "native"})
            with patch.object(scanner, "_detect_native", return_value="3.93.4"), \
                 mock_popen_with_stderr(INDEX_ERROR_LINE + "\n"):
                scanner.scan(tmpdir)
        assert scanner.fell_back is True
        assert "failed to read index file" in scanner.engine_error()


class TestRunTeeingStderr:
    """The real pipe plumbing, with a real child process."""

    def test_returns_both_streams_and_echoes_stderr(self, capsys):
        code = ("import sys; sys.stdout.write('out-line\\n'); "
                "sys.stderr.write('err-line\\n'); sys.exit(1)")
        stdout, stderr, returncode = SecurityScanner._run_teeing_stderr(
            [sys.executable, "-c", code]
        )
        assert (stdout, stderr, returncode) == ("out-line\n", "err-line\n", 1)
        assert "err-line" in capsys.readouterr().err

    def test_large_output_on_both_streams_does_not_deadlock(self, capsys):
        code = ("import sys; sys.stderr.write('e' * 300000); "
                "sys.stdout.write('o' * 300000)")
        stdout, stderr, returncode = SecurityScanner._run_teeing_stderr(
            [sys.executable, "-c", code]
        )
        assert (len(stdout), len(stderr), returncode) == (300000, 300000, 0)

    def test_echo_escapes_control_sequences_but_capture_stays_raw(self, capsys):
        # #125: the terminal copy is escaped; the copy used for error
        # detection is the child's exact output.
        code = ("import sys; sys.stderr.write("
                "'{\"msg\":\"a\\x1b]0;pwned\\x07\\u009b\\u202e\"}\\n')")
        _, stderr, _ = SecurityScanner._run_teeing_stderr(
            [sys.executable, "-c", code]
        )
        assert "\x1b" in stderr and "\u202e" in stderr
        echoed = capsys.readouterr().err
        for raw in ("\x1b", "\x07", "\u009b", "\u202e"):
            assert raw not in echoed
        assert "\\x1b]0;pwned" in echoed
        assert echoed.endswith("\n")

    def test_echo_keeps_multibyte_character_split_across_reads(self, capsys):
        # 4095 ASCII bytes put the two-byte 'é' across the 4096-byte read.
        code = ("import sys; sys.stderr.buffer.write("
                "b'x' * 4095 + 'é'.encode() + b'\\n')")
        SecurityScanner._run_teeing_stderr([sys.executable, "-c", code])
        echoed = capsys.readouterr().err
        assert echoed == "x" * 4095 + "é\n"

    def test_missing_binary_raises_and_leaks_no_descriptor(self):
        before = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else None
        with pytest.raises(FileNotFoundError):
            SecurityScanner._run_teeing_stderr(["gh-safe-repo-no-such-binary"])
        if before is not None:
            assert len(os.listdir("/dev/fd")) == before


# --- truffleHog status line (#10) ---

from gh_safe_repo import security_scanner as scanner_module  # noqa: E402
from gh_safe_repo.security_scanner import _TruffleHogStatus  # noqa: E402

CLEAR = "\r\033[K"


def _fd_count():
    return len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else None


class TestTruffleHogStatus:
    """The TTY-only status line drawn while truffleHog runs."""

    def _run(self, scanner, discovery, stderr_text=""):
        scanner._discovery = discovery
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(SecurityScanner, "_run_teeing_stderr",
                          wraps=SecurityScanner._run_teeing_stderr) as tee, \
             mock_popen_with_stderr(stderr_text):
            scanner._try_trufflehog(tmpdir)
        return tee.call_args.args[1]

    def test_no_status_when_stderr_not_tty(self, capsys):
        status = self._run(make_scanner(), {"method": "native", "version": "3.99.8"})
        assert status is None
        assert CLEAR not in capsys.readouterr().err

    def test_no_status_in_json_mode(self, capsys):
        with patch("sys.stderr.isatty", return_value=True):
            scanner = SecurityScanner(FakeConfig(), progress=False)
        status = self._run(scanner, {"method": "native", "version": "3.99.8"})
        assert status is None
        assert CLEAR not in capsys.readouterr().err

    def test_container_status_names_runtime_and_is_cleared(self, capsys):
        scanner = make_scanner()
        scanner._progress = True
        status = self._run(
            scanner,
            {"method": "container", "runtime": "podman", "runtime_path": "/x/podman"},
            "relayed-line\n",
        )
        assert isinstance(status, _TruffleHogStatus)
        err = capsys.readouterr().err
        assert "truffleHog via podman: starting container... 0:00" in err
        # The relayed line itself is intact and starts on a cleared line.
        assert CLEAR + "relayed-line\n" in err
        assert err.endswith(CLEAR)

    def test_echo_clears_line_before_and_redraws_after(self, capsys):
        status = _TruffleHogStatus("truffleHog", "starting", interval=3600)
        status.start()
        status.echo("hello\n", "hello\n")
        status.stop()
        err = capsys.readouterr().err
        assert err == (
            CLEAR + "truffleHog: starting... 0:00"
            + CLEAR + "hello\n"
            + CLEAR + "truffleHog: starting... 0:00"
            + CLEAR
        )

    def test_no_redraw_after_partial_line(self, capsys):
        status = _TruffleHogStatus("truffleHog", "starting", interval=3600)
        status.start()
        status.echo("partial", "partial")
        status.stop()
        err = capsys.readouterr().err
        # Nothing is drawn, or cleared, after the unterminated chunk.
        assert err.endswith(CLEAR + "partial")

    def test_phase_follows_stderr_lines(self, capsys):
        status = _TruffleHogStatus("truffleHog via podman", "starting container",
                                   interval=3600)
        status.start()
        line = "Trying to pull ghcr.io/trufflesecurity/trufflehog:latest...\n"
        status.echo(line, line)
        assert status._phase == _TruffleHogStatus.PULLING
        running = json.dumps({"level": "info-0", "msg": "running source"},
                             separators=(",", ":"))
        # Split across two chunks: the phase changes on the complete line.
        status.echo(running[:10], running[:10])
        assert status._phase == _TruffleHogStatus.PULLING
        status.echo(running[10:] + "\n", running[10:] + "\n")
        assert status._phase == _TruffleHogStatus.SCANNING
        finished = '{"level":"info-0","msg":"finished scanning","chunks":1}\n'
        status.echo(finished, finished)
        status.stop()
        assert status._phase == _TruffleHogStatus.FINISHING
        err = capsys.readouterr().err
        assert "truffleHog via podman: pulling image..." in err
        assert "truffleHog via podman: scanning..." in err

    def test_elapsed_time_is_minutes_and_seconds(self, capsys):
        status = _TruffleHogStatus("truffleHog", "scanning", interval=3600)
        with patch.object(scanner_module, "time") as fake_time:
            fake_time.monotonic.side_effect = [100.0, 100.4, 165.9]
            status.start()                 # start + first draw
            status.echo("x\n", "x\n")      # redraw at 65.9s
            status.stop()
        err = capsys.readouterr().err
        assert "truffleHog: scanning... 0:00" in err
        assert "truffleHog: scanning... 1:05" in err

    def test_line_is_truncated_to_terminal_width(self, capsys):
        status = _TruffleHogStatus("truffleHog", "x" * 200, interval=3600)
        with patch.object(scanner_module.shutil, "get_terminal_size",
                          return_value=os.terminal_size((40, 20))):
            status.start()
            status.stop()
        drawn = capsys.readouterr().err.split(CLEAR)[1]
        assert len(drawn) == 39

    def test_ticker_redraws_while_child_is_silent(self, capsys):
        # A real child that prints nothing for a while (an image pull or a
        # large repo): the ticker keeps the line alive (#10).
        status = _TruffleHogStatus("truffleHog", "starting", interval=0.05)
        code = "import time; time.sleep(0.4)"
        SecurityScanner._run_teeing_stderr([sys.executable, "-c", code], status)
        err = capsys.readouterr().err
        assert err.count("truffleHog: starting...") >= 3
        assert err.endswith(CLEAR)

    def test_real_child_relay_keeps_raw_copy_and_leaks_nothing(self, capsys):
        threads_before = threading.active_count()
        fds_before = _fd_count()
        status = _TruffleHogStatus("truffleHog", "starting", interval=0.01)
        code = ("import sys, time; sys.stderr.write('one\\x1b[31m\\n'); "
                "sys.stderr.flush(); time.sleep(0.1); "
                "sys.stderr.write('two\\n'); sys.stdout.write('out')")
        stdout, stderr, rc = SecurityScanner._run_teeing_stderr(
            [sys.executable, "-c", code], status
        )
        assert (stdout, stderr, rc) == ("out", "one\x1b[31m\ntwo\n", 0)
        err = capsys.readouterr().err
        assert "\x1b[31m" not in err          # relay still escaped (#125)
        assert CLEAR + "one\\x1b[31m\n" in err
        assert CLEAR + "two\n" in err
        assert err.endswith(CLEAR)
        assert threading.active_count() == threads_before
        if fds_before is not None:
            assert _fd_count() == fds_before

    def test_missing_binary_starts_no_status(self, capsys):
        threads_before = threading.active_count()
        status = _TruffleHogStatus("truffleHog", "starting")
        with pytest.raises(FileNotFoundError):
            SecurityScanner._run_teeing_stderr(["gh-safe-repo-no-such-binary"], status)
        assert status._thread is None
        assert threading.active_count() == threads_before
        assert capsys.readouterr().err == ""
# --- Hidden-character normalization and UTF-16 (#157) ---
#
# AWS's documented example key pair (not a real credential). The live
# evasion runs used truffleHog's public canary key, but GitHub push
# protection blocks that one, so it must never be committed (#157).
# Hidden characters are written as escapes only; never put a raw bidi or
# control character in this file.

EXAMPLE_KID = "AKIAIOSFODNN7EXAMPLE"
EXAMPLE_SEC = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
ZWSP = "\u200b"


def _pair(kid, sec=EXAMPLE_SEC):
    return f"aws_access_key_id = {kid}\naws_secret_access_key = {sec}\n"


def _mid(s, ins, at=8):
    return s[:at] + ins + s[at:]


def _evasion_variants():
    import base64
    import urllib.parse
    kid, sec = EXAMPLE_KID, EXAMPLE_SEC
    return {
        "00_baseline": _pair(kid),
        "01_zwsp": _pair(_mid(kid, "\u200b")),
        "02_zwj": _pair(_mid(kid, "\u200d")),
        "03_soft_hyphen": _pair(_mid(kid, "\u00ad")),
        "04_bom": _pair(_mid(kid, "\ufeff")),
        "05_word_joiner": _pair(_mid(kid, "\u2060")),
        "06_tag_chars": _pair(_mid(kid, "\U000e0041")),
        "07_esc_sgr_reset": _pair(_mid(kid, "\x1b[0m")),
        "08_backspace": _pair(_mid(kid, "X\b")),
        "09_cr_overwrite": (f"aws_access_key_id = AKIAXXXXXXXXXXXXXXXX\r"
                            f"aws_access_key_id = {kid}\naws_secret_access_key = {sec}\n"),
        "10_nul": _pair(_mid(kid, "\x00")),
        "11_rlo_reversed": _pair("\u202e" + kid[::-1] + "\u202c"),
        "12_secret_zwsp": _pair(kid, _mid(sec, "\u200b", 20)),
        "13_cyrillic_A": _pair("\u0410" + kid[1:]),
        "14_fullwidth_digit": _pair(kid.replace("7", "\uff17")),
        "15_concat": f'KID = "AKIAIOSF" + "ODNN7EXAMPLE"\nSEC = "{sec[:20]}" + "{sec[20:]}"\n',
        "16_line_continuation": f'KID = "AKIAIOSF\\\nODNN7EXAMPLE"\nSEC = "{sec}"\n',
        "17_reversed_literal": f'KID = "{kid[::-1]}"[::-1]\nSEC = "{sec}"\n',
        "18_py_hex_escapes": ('KID = "' + "".join(f"\\x{ord(c):02x}" for c in kid)
                              + f'"\nSEC = "{sec}"\n'),
        "19_base64_whole": base64.b64encode(_pair(kid).encode()).decode() + "\n",
        "20_base64_kid_only": (f"aws_access_key_id_b64 = {base64.b64encode(kid.encode()).decode()}\n"
                               f"aws_secret_access_key = {sec}\n"),
        "21_url_encoded": _pair(urllib.parse.quote(kid, safe="").replace("A", "%41")),
        "22_split_lines": (f"aws_access_key_id = AKIAIOSF\n  ODNN7EXAMPLE\n"
                           f"aws_secret_access_key = {sec}\n"),
        "23_kid_only": f"aws_access_key_id = {kid}\n",
        "24_utf16le": _pair(kid).encode("utf-16-le"),
    }


# variant -> (caught before #157, caught now, hidden characters named in the rule)
# "Before" is the regex engine alone (trufflehog_mode = off). The misses that
# remain are the issue's non-goals: homoglyphs and width variants, code-level
# reassembly, and encodings.
_EVASION_EXPECTED = {
    "00_baseline": (True, True, ""),
    "01_zwsp": (False, True, "U+200B ZERO WIDTH SPACE"),
    "02_zwj": (False, True, "U+200D ZERO WIDTH JOINER"),
    "03_soft_hyphen": (False, True, "U+00AD SOFT HYPHEN"),
    "04_bom": (False, True, "U+FEFF ZERO WIDTH NO-BREAK SPACE"),
    "05_word_joiner": (False, True, "U+2060 WORD JOINER"),
    "06_tag_chars": (False, True, "U+E0041 TAG LATIN CAPITAL LETTER A"),
    "07_esc_sgr_reset": (False, True, "ANSI escape sequence"),
    "08_backspace": (False, True, "U+0008 BACKSPACE"),
    "09_cr_overwrite": (True, True, ""),
    "10_nul": (False, True, "U+0000 NUL"),
    "11_rlo_reversed": (False, True,
                        "U+202E RIGHT-TO-LEFT OVERRIDE, U+202C POP DIRECTIONAL FORMATTING"),
    "12_secret_zwsp": (True, True, ""),
    "13_cyrillic_A": (False, False, None),
    "14_fullwidth_digit": (False, False, None),
    "15_concat": (False, False, None),
    "16_line_continuation": (False, False, None),
    "17_reversed_literal": (False, False, None),
    "18_py_hex_escapes": (False, False, None),
    "19_base64_whole": (False, False, None),
    "20_base64_kid_only": (False, False, None),
    "21_url_encoded": (False, False, None),
    "22_split_lines": (False, False, None),
    "23_kid_only": (True, True, ""),
    "24_utf16le": (False, True, ""),
}


def _write_variant(dir_path, name, body):
    path = os.path.join(dir_path, f"{name}.txt")
    if isinstance(body, bytes):
        with open(path, "wb") as f:
            f.write(body)
    else:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(body)
    return path


def _secrets(findings):
    return [f for f in findings if f.category == FindingCategory.SECRET]


def _scan_text(tmp_path, content, name="f.txt", overrides=None):
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    return make_scanner(overrides).scan(str(tmp_path))


class TestEvasionVariants:
    def test_table_covers_every_variant(self):
        assert set(_evasion_variants()) == set(_EVASION_EXPECTED)

    @pytest.mark.parametrize("name", sorted(_EVASION_EXPECTED))
    def test_variant(self, tmp_path, name):
        _write_variant(str(tmp_path), name, _evasion_variants()[name])
        _, caught, hidden = _EVASION_EXPECTED[name]
        secrets = _secrets(make_scanner().scan(str(tmp_path)))
        if not caught:
            assert secrets == []
            return
        assert len(secrets) == 1
        f = secrets[0]
        assert (f.severity, f.line_number, f.match) == (Severity.CRITICAL, 1, "[redacted]")
        assert f.hidden == hidden
        if hidden:
            assert f.rule == f"AWS Access Key ID (obfuscated with {hidden})"
        else:
            # Hits the raw regex already made keep their plain rule
            assert f.rule == "AWS Access Key ID"

    def test_whole_set_in_one_git_repo(self, tmp_path):
        make_git_repo(str(tmp_path))
        for name, body in _evasion_variants().items():
            _write_variant(str(tmp_path), name, body)
        git_add_commit(str(tmp_path), "variants")
        secrets = _secrets(make_scanner().scan(str(tmp_path)))
        caught = {f.file_path[:-len(".txt")] for f in secrets}
        assert caught == {n for n, (_, now, _) in _EVASION_EXPECTED.items() if now}


class TestNormalizeLine:
    @pytest.mark.parametrize("line", [
        "plain ascii\n",
        "windows line\r\n",
        "tab\tseparated\n",
        "no newline at end",
        "caf\u00e9 na\u00efve \u65e5\u672c\u8a9e\n",
        "non\u00a0breaking space\n",   # not printable, but nothing hidden
        "line\u2028separator\n",
    ])
    def test_nothing_hidden(self, line):
        from gh_safe_repo.security_scanner import _normalize_line
        assert _normalize_line(line) is None

    def test_backspace_is_applied_not_just_removed(self):
        from gh_safe_repo.security_scanner import _normalize_line
        (stripped, rendered), hidden = _normalize_line("abX\bcd\n")
        assert stripped == "abXcd"
        assert rendered == "abcd"
        assert hidden == "U+0008 BACKSPACE"

    def test_carriage_return_overwrites_from_column_zero(self):
        from gh_safe_repo.security_scanner import _normalize_line
        (stripped, rendered), _ = _normalize_line("xxxxxxyz\rabc\r\n")
        assert stripped == "xxxxxxyzabc"
        assert rendered == "abcxxxyz"

    def test_escape_sequences_removed(self):
        from gh_safe_repo.security_scanner import _normalize_line
        line = "a\x1b[31mb\x1b]0;title\x07c\x1b(Bd\x9b1me\n"
        (stripped, rendered), hidden = _normalize_line(line)
        assert stripped == rendered == "abcde"
        assert hidden == "ANSI escape sequence"

    def test_rlo_span_rendered_reversed(self):
        from gh_safe_repo.security_scanner import _normalize_line
        (stripped, rendered), _ = _normalize_line("x \u202ecba\u202c y\n")
        assert stripped == "x cba y"
        assert rendered == "x abc y"

    def test_hidden_list_is_capped(self):
        from gh_safe_repo.security_scanner import _normalize_line
        _, hidden = _normalize_line("a\u200bb\u200cc\u200dd\u2060e\ufefff\n")
        assert hidden.count("U+") == 3 and hidden.endswith(", ...")


class TestNormalizationFindings:
    def test_match_only_on_screen_is_caught(self, tmp_path):
        # Neither the bytes nor the stripped text hold the key; a terminal
        # shows it once the CR has moved the cursor back
        line = "key = xxxxxxxxODNN7EXAMPLE\rkey = AKIAIOSF\n"
        secrets = _secrets(_scan_text(tmp_path, line))
        assert [f.hidden for f in secrets] == ["U+000D CARRIAGE RETURN"]

    def test_overwrite_cannot_erase_a_key(self, tmp_path):
        # The rendered form is blanked by the CR; the stripped form keeps it
        line = _mid(EXAMPLE_KID, ZWSP) + "\r" + " " * 30 + "\n"
        secrets = _secrets(_scan_text(tmp_path, line))
        assert len(secrets) == 1 and secrets[0].severity == Severity.CRITICAL

    def test_warning_pattern_keeps_warning_severity(self, tmp_path):
        # A password value may hold anything, so this is a raw match ...
        line = f"password = 'abcdefgh{ZWSP}ijkl'\n"
        assert [f.hidden for f in _secrets(_scan_text(tmp_path, line))] == [""]
        # ... while a hidden character in the key name is not
        line = f"api{ZWSP}_key = 'abcdefghijklmnopqrst'\n"
        secrets = _secrets(_scan_text(tmp_path, line, name="g.txt"))
        assert [(f.severity, f.rule) for f in secrets if f.file_path == "g.txt"] == [
            (Severity.WARNING, "Generic API key (obfuscated with U+200B ZERO WIDTH SPACE)"),
        ]

    def test_banned_string_split_by_hidden_character(self, tmp_path):
        findings = _scan_text(tmp_path, "by Ac\u00adme Corp\n",
                              overrides={("pre_flight_scan", "banned_strings"): "acme"})
        banned = [f for f in findings if f.category == FindingCategory.BANNED_STRING]
        assert [(f.rule, f.match) for f in banned] == [
            ("Banned string: acme (obfuscated with U+00AD SOFT HYPHEN)", "[redacted]"),
        ]

    def test_crlf_file_line_numbers_and_plain_rule(self, tmp_path):
        content = "a\r\nb\r\nkey = " + EXAMPLE_KID + "\r\n"
        secrets = _secrets(_scan_text(tmp_path, content))
        assert [(f.line_number, f.rule, f.hidden) for f in secrets] == [
            (3, "AWS Access Key ID", ""),
        ]

    def test_lone_cr_does_not_start_a_line(self, tmp_path):
        # Line numbers follow `sed -n` and git, which count "\n" only
        content = "a\rb\nkey = " + EXAMPLE_KID + "\n"
        secrets = _secrets(_scan_text(tmp_path, content))
        assert [f.line_number for f in secrets] == [2]

    def test_report_names_hidden_characters_and_hint_reveals_them(self, tmp_path):
        secrets = _secrets(_scan_text(tmp_path, _pair(_mid(EXAMPLE_KID, ZWSP))))
        out = format_findings(secrets)
        assert "AWS Access Key ID (obfuscated with U+200B ZERO WIDTH SPACE)" in out
        assert "View with: sed -n '1,5p' f.txt | cat -v" in out
        assert EXAMPLE_KID not in out
        assert out.isascii()

    def test_plain_hit_hint_unchanged(self, tmp_path):
        out = format_findings(_secrets(_scan_text(tmp_path, _pair(EXAMPLE_KID))))
        assert out.endswith("View with: sed -n '1,5p' f.txt")

    def test_json_keeps_match_redacted(self, tmp_path):
        from gh_safe_repo.commands._common import format_findings_json
        secrets = _secrets(_scan_text(tmp_path, _pair(_mid(EXAMPLE_KID, "\x1b[0m"))))
        doc = json.loads(format_findings_json(secrets))
        (entry,) = doc["findings"]
        assert entry["match"] == "[redacted]"
        assert entry["rule"] == "AWS Access Key ID (obfuscated with ANSI escape sequence)"
        assert set(entry) == {
            "severity", "category", "file_path", "line_number", "rule", "match",
            "commit", "timestamp",
        }

    def test_emails_and_todos_use_the_raw_line(self, tmp_path):
        findings = _scan_text(tmp_path, f"TO{ZWSP}DO ex{ZWSP}ample@example.com\n")
        assert [f.match for f in findings if f.category == FindingCategory.TODO] == []
        assert [f.match for f in findings if f.category == FindingCategory.EMAIL] == [
            "ample@example.com",
        ]


class TestNormalizationFalsePositives:
    """Stripping Cf characters must not invent findings in ordinary text."""

    TEXTS = {
        # family emoji (ZWJ sequence), subdivision flag (tag sequence),
        # skin tone modifier, keycap with variation selector
        "emoji.md": ("Team: \U0001F468\u200d\U0001F469\u200d\U0001F467\u200d\U0001F466 "
                     "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F "
                     "\U0001F44D\U0001F3FD 1\ufe0f\u20e3\n"),
        # Persian with ZERO WIDTH NON-JOINER; Arabic with bidi marks and isolates
        "fa.md": "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645 "
                 "\u06a9\u062a\u0627\u0628\u200c\u0647\u0627\n",
        "ar.md": "\u200f\u0645\u0631\u062d\u0628\u0627\u200e ABC "
                 "\u2067\u0639\u0631\u0628\u064a\u2069\n",
        "crlf.txt": "first\r\nsecond line with token words\r\nthird\r\n",
        "bom.txt": "\ufeffBOM at the start\nsecond\n",
        "soft.txt": "hyphen\u00adation and AKIA\u00ad words\n",
        "log.txt": "\x1b[32mOK\x1b[0m build passed\r\n\x1b[31mFAIL\x1b[0m 3 tests\n",
        "progress.txt": "  10%\r  50%\r 100%\n",
    }

    def test_no_secret_or_banned_findings(self, tmp_path):
        for name, text in self.TEXTS.items():
            with open(tmp_path / name, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        findings = make_scanner({
            ("pre_flight_scan", "banned_strings"): "secretproject",
        }).scan(str(tmp_path))
        assert [f for f in findings if f.category in
                (FindingCategory.SECRET, FindingCategory.BANNED_STRING)] == []

    def test_crlf_and_i18n_lines_take_the_fast_path(self):
        from gh_safe_repo.security_scanner import _normalize_line
        assert _normalize_line(self.TEXTS["crlf.txt"].split("\n")[0] + "\n") is None
        assert _normalize_line("\u0633\u0644\u0627\u0645 \u062f\u0646\u06cc\u0627\n") is None


class TestTextDecoding:
    LINE = f"key = {EXAMPLE_KID}\n"

    @pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be", "utf-32"])
    def test_utf16_and_utf32_are_scanned(self, tmp_path, codec):
        # "utf-16"/"utf-32" write a BOM; the -le/-be forms do not
        content = ("first\r\n" + self.LINE).encode(codec)
        secrets = _secrets(_scan_text(tmp_path, content))
        assert [(f.line_number, f.rule) for f in secrets] == [(2, "AWS Access Key ID")]

    def test_utf16_emails_and_todos(self, tmp_path):
        findings = _scan_text(tmp_path, "# TODO mail example@example.com\n".encode("utf-16"))
        cats = {f.category for f in findings}
        assert {FindingCategory.EMAIL, FindingCategory.TODO} <= cats

    def test_utf8_bom_is_not_part_of_line_one(self, tmp_path):
        from gh_safe_repo.security_scanner import _read_text
        path = tmp_path / "b.txt"
        path.write_bytes(codecs_bom_utf8() + self.LINE.encode())
        assert _read_text(str(path)) == self.LINE

    @pytest.mark.parametrize("codec", ["utf-8-sig", "utf-16"])
    def test_escape_heavy_log_with_bom_is_text(self, tmp_path, codec):
        content = ("\x1b[1m\x1b[31mE\x1b[0m\x1b[2K\n" * 50 + self.LINE).encode(codec)
        assert len(_secrets(_scan_text(tmp_path, content))) == 1

    def test_stray_nul_text_is_scanned(self, tmp_path):
        content = ("a\x00b\n" + self.LINE).encode()
        assert len(_secrets(_scan_text(tmp_path, content))) == 1

    @pytest.mark.parametrize("content", [
        b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8 + b"key = " + EXAMPLE_KID.encode() + b"\n",
        b"\x00\x01" + ("key = " + EXAMPLE_KID + "\n").encode(),       # other C0 control
        b"key = " + EXAMPLE_KID.encode() + b"\n" + b"\x00" * 64,       # too many NULs
        b"\x00key = " + EXAMPLE_KID.encode() + b"\xc3\x28\n",          # invalid UTF-8
        b"\xff\xfe" + b"\x00\x00\x01\x00" * 40,                         # UTF-16 BOM, then controls
        bytes(b for c in range(1, 32) for b in (c, 0)) * 4,           # UTF-16 LE shape, controls
    ], ids=["elf", "c0", "many-nuls", "bad-utf8", "bom-controls", "utf16-controls"])
    def test_binaries_stay_binary(self, tmp_path, content):
        from gh_safe_repo.security_scanner import _read_text
        path = tmp_path / "blob"
        path.write_bytes(content)
        assert _read_text(str(path)) is None
        assert _secrets(make_scanner().scan(str(tmp_path))) == []

    def test_large_utf16_file_is_reported_not_scanned(self, tmp_path):
        findings = _scan_text(tmp_path, (self.LINE * 200).encode("utf-16"),
                              overrides={("pre_flight_scan", "max_file_size_mb"): "0.001"})
        assert [f.category for f in findings] == [FindingCategory.LARGE_FILE]

    def test_binary_extension_skips_utf16(self, tmp_path):
        findings = _scan_text(tmp_path, self.LINE.encode("utf-16"), name="data.bin")
        assert _secrets(findings) == []

    def test_excluded_utf16_file_skipped(self, tmp_path):
        findings = _scan_text(tmp_path, self.LINE.encode("utf-16"), name="w.txt",
                              overrides={("pre_flight_scan", "scan_exclude_paths"): r"^w\.txt$"})
        assert _secrets(findings) == []


def codecs_bom_utf8():
    import codecs
    return codecs.BOM_UTF8
