"""Shared fixtures and helpers for E2E tests."""

import json
import os
import shutil
import subprocess
import tempfile
import uuid

import pytest


# ── CLI binary location ──────────────────────────────────────────────


def _find_cli():
    """Locate the gh-safe-repo binary inside the active venv."""
    path = shutil.which("gh-safe-repo")
    if path:
        return path
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    candidate = os.path.join(repo_root, ".venv", "bin", "gh-safe-repo")
    if os.path.isfile(candidate):
        return candidate
    raise FileNotFoundError(
        "gh-safe-repo not found on PATH or in .venv/bin/. Run `uv sync` first."
    )


CLI = _find_cli()


def run(*args, stdin_text=None, timeout=30):
    """Run gh-safe-repo with the given arguments and return CompletedProcess."""
    return subprocess.run(
        [CLI, *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ── Markers ──────────────────────────────────────────────────────────


def _has_gh_auth():
    """Return True if `gh auth token` succeeds."""
    try:
        r = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and r.stdout.strip() != ""
    except Exception:
        return False


_AUTH_AVAILABLE = _has_gh_auth()


requires_auth = pytest.mark.skipif(
    not _AUTH_AVAILABLE,
    reason="No GitHub auth token available (run `gh auth login`)",
)

requires_live = pytest.mark.skipif(
    not (os.environ.get("E2E_LIVE") and _AUTH_AVAILABLE),
    reason="E2E_LIVE env var not set or no auth token",
)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def tmp_scan_dir():
    """Create a temporary directory for scan tests, cleaned up after."""
    d = tempfile.mkdtemp(prefix="gsr-e2e-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def gh_owner():
    """Return the authenticated GitHub username. Skip if no auth."""
    if not _AUTH_AVAILABLE:
        pytest.skip("No GitHub auth token available")
    r = subprocess.run(
        ["gh", "api", "/user", "--jq", ".login"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        pytest.skip(f"Failed to get GitHub username: {r.stderr}")
    return r.stdout.strip()


@pytest.fixture
def unique_repo_name(request):
    """Generate a unique repo name for live tests: gsr-e2e-{uuid8}-{test}."""
    short_id = uuid.uuid4().hex[:8]
    test_name = request.node.name.replace("test_", "")[:30]
    return f"gsr-e2e-{short_id}-{test_name}"
