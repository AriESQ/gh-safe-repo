"""Suite-wide fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _no_background_git_maintenance(monkeypatch):
    """Stop git from forking auto-maintenance into test repos.

    `git commit` can start `gc --auto` / `maintenance run --auto` detached
    in the background. If it is still writing into `.git` when the test's
    TemporaryDirectory is removed, cleanup fails with "Directory not empty".
    Env-based config reaches every git process the tests and the code under
    test start, without touching the developer's own config.
    """
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "maintenance.auto")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "gc.auto")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "0")
