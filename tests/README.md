# tests

Unit and integration tests for `gh-safe-repo`.

## Setup

```bash
uv sync          # creates .venv and installs pytest
```

No other setup is required. There are no real API calls — all `subprocess` calls to
`gh api` and `git` are mocked except where noted below.

## Running tests

```bash
# Full suite
uv run pytest tests/ -v

# Single file
uv run pytest tests/test_plugins.py -v

# Single test
uv run pytest tests/test_plugins.py::TestRepositoryPlugin::test_plan_creates_repo -v

# Stop on first failure
uv run pytest tests/ -x
```

## Test files

| File | What it covers |
|---|---|
| `test_config_manager.py` | `ConfigManager` defaults, INI file parsing, `apply_overrides()`, validation errors |
| `test_diff.py` | `Change` and `Plan` dataclasses, `count_by_type()`, `format_plan_json()` |
| `test_github_client.py` | `GitHubClient` — `call_api()`, auth token resolution, `copy_repo()`, `push_local()`, status-code parsing |
| `test_plugins.py` | All four plugins (`RepositoryPlugin`, `ActionsPlugin`, `BranchProtectionPlugin`, `SecurityPlugin`) — plan generation, apply calls, no-op detection, plan-limit skips |
| `test_cli.py` | `main()` argument validation, `_resolve_branches()`, `format_plan_json()`, mutually-exclusive flag errors |
| `test_security_scanner.py` | `SecurityScanner` — real tempfiles on disk; truffleHog dispatch, regex fallback, `_unified_walk()`, AI context file detection, git history check |

## Mocking conventions

**All `subprocess` calls are mocked** in every test file except `test_security_scanner.py`.
Tests import `unittest.mock.patch` and mock at the call-site level, not at the OS level.
The common helper pattern:

```python
def make_completed_process(stdout="", stderr="", returncode=0):
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result
```

**`test_security_scanner.py` uses real temporary directories** (`tempfile.mkdtemp()`).
Files are written to disk and cleaned up in `teardown_method`. This is intentional —
mocking the filesystem for a scanner that reads file contents would test nothing useful.
Integration tests that exercise git (history checks, `clone_for_scan`) run actual `git`
commands in the temp directory via `make_git_repo()` / `git_add_commit()` helpers defined
at the top of that file.

**`GitHubClient` is mocked as a `MagicMock()`** in plugin tests. `make_mock_client()` in
`test_plugins.py` sets up the `repo_path()` side-effect so path construction works without
a real client.

**`FakeConfig`** in `test_security_scanner.py` is a lightweight dict-backed stand-in for
`ConfigManager`. The scanner only calls `config.getbool()` and `config.get()` so a full
`ConfigManager` instance is not needed.

## Adding tests

- Plugin tests go in `test_plugins.py`. Mock `GitHubClient` via `make_mock_client()` and
  assert on `client.call_api.call_args_list` to verify the right API calls were (or were
  not) made.
- Scanner tests go in `test_security_scanner.py`. Write real files to a `tempfile.mkdtemp()`
  directory; clean up in `teardown_method`.
- For truffleHog-specific paths, pre-seed `scanner._discovery` directly to bypass the
  subprocess version check:
  ```python
  scanner._discovery = {"method": "native", "version": "3.99.0"}
  ```
