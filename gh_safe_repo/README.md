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
| `cli.py` | `build_parser()` / `main()` — subparser dispatch to `create`, `fix`, `scan` commands |
| `commands/_common.py` | Shared helpers: `CLIContext`, `parse_repo_arg()`, `build_context()`, `add_common_args()`, plan and findings formatting (`format_plan_json(plan, verification=None)`, `format_findings_json()`), the pre-flight `ScanDecision`, `print_verification()` and the banners, `--json` stdout discipline (`prose_stream()`, `prose_to_stderr()`, `ask()`), ANSI output (auto-disabled off a TTY / under `NO_COLOR`) |
| `commands/create.py` | `create` subcommand — new repo with safe defaults |
| `commands/fix.py` | `fix` subcommand — audit existing repo, show diff, apply corrections |
| `commands/scan.py` | `scan` subcommand — local-only secret scanning |
| `github_client.py` | Wrapper around `gh api` (subprocess); `copy_repo()`, `push_local()` (bare-clones so all branches push; pushes and sets `default_branch`; returns a `LocalWiring` describing the default branch and the `origin`/upstream setup on the user's repo), `clone_for_scan()`; `git_remote_url()` builds URLs per `gh config get git_protocol`; `verify_git_credentials()` pre-flight probes SSH |
| `git_transport.py` | Frozen `GitTransport` (protocol, env, URLs, redaction, `preflight()`, `run_remote()` which retries a missed `*-SK` touch once) built by `discover_transport()`; `security_key_hint()` names a missed `*-SK` hardware-key touch in git errors |
| `config_manager.py` | INI config parsing via `configparser`; holds `SAFE_DEFAULTS` |
| `diff.py` | `Change` and `Plan` dataclasses; `count_by_type()` |
| `verify.py` | Post-apply verification (#77): `Check` / `Verification` result model (`ok`, `has_fatal`, `exit_code()`, `to_dict()` = the `--json` `verification` block) and `verify_plugins()`, which re-audits plugins after apply with 1s/2s/4s retry rounds and maps the audit plan to `ok` / `mismatch` / `unknown` checks |
| `errors.py` | Custom exception hierarchy (`GhSafeRepoError`, etc.) |
| `security_scanner.py` | Pre-flight scanner: truffleHog dispatch, regex fallback, `_unified_walk()` (tracked files only in git repos, NUL-byte binary sniff, TTY progress line). `scan()` records the engine that ran (`engine_used`, `fell_back`, `fallback_reason`); `engine_error()` turns a fallback under explicit `native`/`docker` into an error message |
| `plugins/base.py` | Abstract `BasePlugin` — defines the `fetch_current_state(strict=False)` / `plan()` / `apply()` interface |
| `plugins/repository.py` | Repo creation (`POST /user/repos`) and basic repo settings (`PATCH`) |
| `plugins/actions.py` | GitHub Actions permissions (allowed actions, workflow perms, SHA pinning) |
| `plugins/branch_protection.py` | Branch protection via Rulesets API (default; classic per-branch path behind `use_rulesets = false`) |
| `plugins/security.py` | Dependabot alerts/security updates, secret scanning/push protection, private vuln reporting |
| `plugins/tag_protection.py` | Immutable tags via Rulesets API (prevent deletion/rewriting of release tags) |
| `templates/` | File templates (currently empty) |

## Global vs. command options

`--config` and `--debug` are parsed on either side of the command name. They are
registered twice: once on the top-level parser (`build_parser()`, with the real
defaults) and once on each subparser (`add_common_args()`, with
`default=argparse.SUPPRESS`). The SUPPRESS is what makes the pre-command form
usable — without it, the subparser's own default overwrites the value the
top-level parser already stored in the namespace. Any new global option must
follow the same two-sided pattern; command-specific options (`--json`,
`--dry-run`, …) stay on their subparser with a normal default.

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
  ├─ each plugin.apply() →  API calls for non-SKIP changes
  └─ verify_plugins()    →  fetch_current_state(strict=True) + plan() again (fix)
```

### Plugin contract for verification

`fix` verifies an apply by running the same plugin's audit again
(`verify.verify_plugins()`), so every plugin must satisfy two rules:

- **The post-apply audit plan must be empty.** After a successful `apply()`,
  `plan(current_state=fetch_current_state())` returns only `SKIP` rows. Any
  other row is reported as a `mismatch` (expected = `new`, actual = `old`) and
  makes `fix` exit `3`. A new setting whose audit cannot express "done" (it
  always emits an `UPDATE`) breaks this; a create-only row the audit never
  emits must be listed in `verify.CREATE_ONLY_KEYS`.
- **`fetch_current_state(strict=True)` must not guess.** When a read cannot
  tell "off" from "could not read" (403, 5xx, missing status, unparseable
  body), raise `APIError` instead of returning GitHub's default. Only a
  definite answer may be returned: a 2xx, or the endpoint's documented 404 for
  "off". The verifier retries and then reports `unknown`, which warns but does
  not fail the run. `strict=False` (planning) keeps the lenient behaviour.
  Reads must not be served from a stale cache: the verifier calls
  `client.forget_repo()` first, so read `GET /repos` via `get_repo_data()`.

## Adding a new setting

1. Identify the GitHub API endpoint.
2. Add the key and safe default to `config_manager.py:SAFE_DEFAULTS`.
3. Add the corresponding entry (with a comment) to `gh-safe-repo.ini.example` in the repo root.
   `test_config_consistency.py` will fail if the example file and `SAFE_DEFAULTS` diverge.
4. Update the appropriate plugin's `plan()` and `apply()` methods.
5. Add tests in `tests/test_plugins.py` — all `subprocess` calls must be mocked.

## Key design rules

- **No runtime dependencies.** Everything uses the Python standard library. Do not
  add third-party packages without prior discussion.
- **All GitHub API calls go through `GitHubClient`.** Never call `subprocess` or
  `gh api` directly from a plugin or from `cli.py`.
- **Tokens are never logged.** Debug output uses sanitised URLs; `GH_TOKEN` is
  injected into the child-process environment, not into logged command strings.
- **`fix` emits repo identity in debug mode.** After `get_repo_data()`, `fix`
  prints the repo's `id`, `full_name`, and `owner.type` to stderr when `--debug`
  is set, so users can confirm they are targeting the correct repo.
- **`GET /user` and `GET /repos/{owner}/{repo}` are cached.** `GitHubClient` caches
  both; every plugin hits the cache instead of making a fresh HTTP call. Anything
  read after an apply must use `get_repo_data(refresh=True)` or call
  `forget_repo()` first, or it sees the pre-apply state.
- **Under `--json`, stdout carries exactly one JSON document.** Route prose
  through `info(..., json_mode=)`, `prose_stream(json_mode)` or
  `prose_to_stderr(json_mode)`, and prompts through `ask()`.
