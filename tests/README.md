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

CI (`.github/workflows/ci.yml`) runs `uv run pytest tests/ -v` on Python 3.10 and 3.14
for every push to `master` and every pull request. CI has no GitHub token, so the Tier 2
and Tier 3 E2E tests below skip themselves. CI also sets `trufflehog_mode = off` through an
`XDG_CONFIG_HOME` config file, because the runner has docker and `auto` would pull the
truffleHog image. New tests must pass without network access or a token. CI sets
`GIT_AUTHOR_*`/`GIT_COMMITTER_*`, but tests that commit should still pass
`-c user.name=... -c commit.gpgsign=false` so they work on any machine.
`tests/conftest.py` turns off git's background auto-maintenance (`maintenance.auto`,
`gc.auto`) for every test through `GIT_CONFIG_*` env vars; a detached `gc` still writing
into `.git` made `TemporaryDirectory` cleanup fail intermittently in CI.

## Test files

| File | What it covers |
|---|---|
| `test_config_manager.py` | `ConfigManager` defaults, INI file parsing, `apply_overrides()`, validation errors |
| `test_config_consistency.py` | `SAFE_DEFAULTS` ↔ `gh-safe-repo.ini.example` parity (sections, keys, values), config file pickup for every key, CLI override precedence, static analysis that all consumed config keys have defaults |
| `test_actions_usage.py` | Actions in use (#111): `uses:` parsing and classification, whitelist pattern matching, policy evaluation (`all` / `selected` / `local_only`, GitHub-owned, `!` blocks, same-owner, verified → unknown, SHA pinning incl. the reusable-workflow exemption), before/after reporting (already-blocked refs not listed), pin lookups and their cap, `ApiSource` (contents API, local composite actions, 404 / read errors) and `GitSource`, `warnings` in the table and `--json`, and the command wiring: `audit`/`fix` with a real `ActionsPlugin`, `copy --dry-run` (zero API calls, `actions_scan_skipped`) and `push --dry-run` (local git, no pins). `GitSource._git` is patched in command tests, not `subprocess.run` |
| `test_diff.py` | `Change` and `Plan` dataclasses, `count_by_type()`, `format_plan_json()`, `PRESERVE` never actionable |
| `test_merge_policy.py` | The `fix`/`audit` three-way merge: every `SAFE_DEFAULTS` key in a merge section has a tier and every security key a direction, `is_stricter()` per shape, `ConfigManager.declared_sections`, `apply_merge_policy()` row by row (at-default → `UPDATE`, preference conflict → `PRESERVE`, stricter → `PRESERVE`, weaker → `UPDATE` + `weakened`, declared section wins, `--all` bypass, unannotated rows pass through), plugins annotate their rows, and ruleset / classic bodies carry a preserved value instead of the desired one |
| `test_github_client.py` | `GitHubClient` — `call_api()`, auth token resolution, `copy_repo()` (incl. setting the source default branch), `push_local()` (incl. the pushed branch/tag maps on `LocalWiring`), `matching_refs()` parsing (empty, 409 empty repo, paginated `[...][...]` output, annotated tags), status-code parsing, `git_remote_url()` protocol selection, `verify_git_credentials()` SSH probe |
| `test_plugins.py` | All four plugins (`RepositoryPlugin`, `ActionsPlugin`, `BranchProtectionPlugin`, `SecurityPlugin`) — plan generation, apply calls, no-op detection, plan-limit skips, strict reads, `protected_branches()` / `protected_tags()` presence reads |
| `test_cli.py` | `main()` argument validation, `build_context()` owner check and `require_owner_match`, `fix` admin permissions check, `_resolve_branches()`, `format_plan_json()` / `format_findings_json()`, `scan --json`, the `ScanDecision` matrix (`--yes` / no-TTY / criticals) and the exit codes it drives, ANSI suppression off a TTY, `--config`/`--no-config` resolution and the rejected combinations, `audit` exit codes (3 on drift, 0 compliant, never prompts or applies; `PRESERVE`-only → table + 0) against `fix --dry-run` (always 0), the `PRESERVE` / `⚠ weakened` rendering, `--all`, `fix` not applying or verifying `PRESERVE` rows, the merge fields in `--json`, `push`/`copy` delegation into `create.run`, `scan` defaulting to the cwd, `fix` and `create` verification exit matrix (0 / 3 / fatal → 1 / unknown → 0), `create` README-removal and `copy` description/topics checks, the `push`/`copy` code and wiring matrix (refs missing or at another SHA → 1, wrong default branch → 3, wiring mismatch or foreign origin → 0 with a warning, unreadable refs → 0), and the `--json` rule that stdout is one `json.loads`-able document |
| `test_verify.py` | `verify.py` — `Check`/`Verification` model and exit codes, audit-plan → check mapping, retry → success, retry → unknown, all-SKIP plugins never re-read, shared retry budget, `Probe` ok/mismatch/unknown (unknown never fatal), `create_probes()` row → probe mapping, `verify_create()` repo-exists short-circuit and `extra_probes` hook, the `push`/`copy` probes (pushed/mirrored refs fatal, default branch exit 3, local wiring advisory and never retried, against real temp git repos). Plugins are `MagicMock`s; `sleep` is injected |
| `test_local_repo.py` | Linked worktrees and submodules as sources (#103), real git with a local bare repo as the remote: `is_git_checkout()` / `external_git_dir()`, `push_local` from a worktree (dirty and clean) and a submodule leaves `rev-list --count --all` and `status --porcelain` unchanged and pushes the right heads, the non-git path never copies a `.git` entry, `_local_branches()`, and the scan (ignored files skipped, truffleHog `git` mode, container mounts the external gitdir) |
| `test_assistant_files.py` | AI coding assistant file catalog (#124): the shipped `data/ai_assistant_files.json` loads cleanly, covers every entry of the scanner's `_AI_CONTEXT_FILES` (root, nested and case variants), and flags generic names; every validation rule accepted and rejected (paths, globs, enums, unknown keys at each level, https URLs, ISO dates not in the future, ANSI/OSC/C1/bidi/zero-width injection in text, duplicate keys and paths, the error message never echoing raw escapes); exact / prefix / glob matching with `root` and `any` anchors. Pure, no filesystem except `tmp_path` loads |
| `test_security_scanner.py` | `SecurityScanner` — real tempfiles on disk; truffleHog dispatch, regex fallback, `_unified_walk()`, AI context file detection, git history check |

## Mocking conventions

**All `subprocess` calls are mocked** in every test file except `test_security_scanner.py`
and `test_local_repo.py` (real worktrees and submodules; see its docstring).
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

**Mock a module's `time`, not `time.sleep`.** `patch("pkg.mod.time.sleep")`
swaps the attribute on the shared `time` module, so it also counts stdlib
sleeps such as `subprocess`'s wait polling when a test runs real `git` (#109).
Retry-round counts in `test_cli.py` go through `patch_verify_sleep()`, which
patches `gh_safe_repo.verify.time`.

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
