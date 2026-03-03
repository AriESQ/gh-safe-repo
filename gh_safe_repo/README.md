# gh_safe_repo — package internals

This directory is the Python package imported by the `gh-safe-repo` launcher.

## Relationship to the launcher

The repository root contains a thin launcher script named `gh-safe-repo` (no `.py`
extension). Its only job is to ensure the package is importable — by inserting the
repo root into `sys.path` when run directly — and then call `gh_safe_repo.cli.main()`.
When the tool is installed via `uv tool install .` or `pip install`, the same
`main()` is wired up as the console-script entry point declared in `pyproject.toml`,
and the launcher is not used.

```
gh-safe-repo          ← thin launcher (direct-run entry point)
gh_safe_repo/         ← this package (all real logic lives here)
```

## Module map

| Module | Purpose |
|---|---|
| `cli.py` | `main()` + plan/apply orchestration, all I/O |
| `github_client.py` | Wrapper around `gh api` (subprocess); `copy_repo()`, `push_local()` |
| `config_manager.py` | INI config parsing via `configparser`; holds `SAFE_DEFAULTS` |
| `diff.py` | `Change` and `Plan` dataclasses; `count_by_type()`, `format_plan_json()` |
| `errors.py` | Custom exception hierarchy (`GhSafeRepoError`, etc.) |
| `security_scanner.py` | Pre-flight scanner: truffleHog dispatch, regex fallback, `_unified_walk()` |
| `plugins/base.py` | Abstract `BasePlugin` — defines the `plan()` / `apply()` interface |
| `plugins/repository.py` | Repo creation (`POST /user/repos`) and basic repo settings (`PATCH`) |
| `plugins/actions.py` | GitHub Actions permissions (allowed actions, workflow perms, SHA pinning) |
| `plugins/branch_protection.py` | Classic branch protection + Rulesets API |
| `plugins/security.py` | Dependabot alerts + secret scanning |
| `templates/` | File templates (currently empty) |

## Plugin architecture

Each plugin follows a fetch → diff → apply cycle:

1. **`plan()`** — fetches current repo state from the GitHub API and compares it against
   the desired state from config. Returns a `Plan` (list of `Change` objects tagged as
   `ADD` / `UPDATE` / `DELETE` / `SKIP`).
2. **`apply()`** — iterates the plan and makes only the API calls that correspond to
   real changes. No-ops (settings already at the desired value) produce no API calls.

This means audit mode and create mode share the same code path. The only difference
is whether current state is fetched from an existing repo or assumed to be GitHub
defaults.

```
cli.main()
  │
  ├─ each plugin.plan()  →  Plan (list of Change)
  ├─ print plan table
  └─ each plugin.apply() →  API calls for non-SKIP changes
```

## Adding a new setting

1. Identify the GitHub API endpoint.
2. Add the key and safe default to `config_manager.py:ConfigManager.SAFE_DEFAULTS`.
3. Add the corresponding entry (with a comment) to `config.ini.example` in the repo root.
4. Update the appropriate plugin's `plan()` and `apply()` methods.
5. Add tests in `tests/test_plugins.py` — all `subprocess` calls must be mocked.

## Key design rules

- **No runtime dependencies.** Everything uses the Python standard library. Do not
  add third-party packages without prior discussion.
- **All GitHub API calls go through `GitHubClient`.** Never call `subprocess` or
  `gh api` directly from a plugin or from `cli.py`.
- **Tokens are never logged.** Debug output uses sanitised URLs; `GH_TOKEN` is
  injected into the child-process environment, not into logged command strings.
- **`GET /user` and `GET /repos/{owner}/{repo}` are cached.** `GitHubClient` caches
  both; every plugin hits the cache instead of making a fresh HTTP call.
