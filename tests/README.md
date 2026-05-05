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
| `test_config_consistency.py` | `SAFE_DEFAULTS` ↔ `gh-safe-repo.ini.example` parity (sections, keys, values), config file pickup for every key, CLI override precedence, static analysis that all consumed config keys have defaults |
| `test_diff.py` | `Change` and `Plan` dataclasses, `count_by_type()`, `format_plan_json()` |
| `test_github_client.py` | `GitHubClient` — `call_api()`, auth token resolution, `copy_repo()`, `push_local()`, status-code parsing, `git_remote_url()` protocol selection, `verify_git_credentials()` SSH probe |
| `test_plugins.py` | All four plugins (`RepositoryPlugin`, `ActionsPlugin`, `BranchProtectionPlugin`, `SecurityPlugin`) — plan generation, apply calls, no-op detection, plan-limit skips |
| `test_cli.py` | `main()` argument validation, `build_context()` owner check and `require_owner_match`, `fix` admin permissions check, `_resolve_branches()`, `format_plan_json()`, mutually-exclusive flag errors |
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

## E2E tests (`tests/e2e/`)

End-to-end tests that run the real `gh-safe-repo` binary via subprocess with no mocking.
Organized into three tiers by dependency level:

| File | Tier | Dependencies | Tests |
|---|---|---|---|
| `test_input_validation.py` | 1 | None | Help output, bad repo args, mutually exclusive flags, config errors |
| `test_scan.py` | 1 | Filesystem only | Scan for secrets, emails, large files, TODOs, AI context files |
| `test_auth_errors.py` | 2 | `gh auth token` | Wrong owner, --json/--debug output, custom config, plan-level gating |
| `test_live_api.py` | 3 | `gh auth token` + `E2E_LIVE=1` | Create/fix/delete real repos, idempotency, pre-flight scan abort |

### Running E2E tests

```bash
# Tier 1 — no auth or network needed
uv run pytest tests/e2e/test_input_validation.py tests/e2e/test_scan.py -v

# Tier 2 — requires gh auth login
uv run pytest tests/e2e/test_auth_errors.py -v

# Tier 3 — creates/deletes real repos (requires E2E_LIVE + delete_repo scope)
gh auth refresh -h github.com -s delete_repo
E2E_LIVE=1 uv run pytest tests/e2e/test_live_api.py -v

# All E2E tests
E2E_LIVE=1 uv run pytest tests/e2e/ -v

# Skip slow tests (e.g. 150MB large file scan)
uv run pytest tests/e2e/ -v -m "not slow"
```

### Prerequisites for Tier 3

- `gh auth login` with a token that has `delete_repo` scope
- `E2E_LIVE=1` environment variable set
- Tests use unique repo names (`gsr-e2e-{uuid}-{test}`) and clean up on teardown
- Recommended: use a dedicated bot account, not your main GitHub account

## Test conventions

**Default test email:** Use `example@example.com` (RFC 2606 reserved) as the standard
email in test fixtures. This avoids collisions with real addresses and keeps tests
consistent. Other emails (e.g. `action@github.com`, `alice@real-corp.com`) should only
appear where the test specifically requires a different address — for example, exclusion
logic tests that need distinct included/excluded values.

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
