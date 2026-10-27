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
| `cli.py` | `build_parser()` / `main()` — subparser dispatch to `new`, `push`, `copy`, `audit`, `fix`, `scan`; the grouped verb list in the top-level `--help` replaces argparse's flat `commands` section |
| `commands/_common.py` | Shared helpers: `CLIContext`, `parse_repo_arg()`, `build_context()`, `add_common_args()` / `add_mutation_args()` / `add_create_args()` (the uniform option sets), `resolve_config_arg()` (`--no-config` → the `args.config == ""` sentinel), plan and findings formatting (`format_plan_json(plan, verification=None)`, `format_findings_json()`), the pre-flight `ScanDecision`, the `create`/`fix` confirmation prompt (`confirm()`: EOF → exit 1 naming `--yes`), `print_verification()` and the banners, `--json` stdout discipline (`prose_stream()`, `prose_to_stderr()`, `ask()`), ANSI output (auto-disabled off a TTY / under `NO_COLOR`) |
| `commands/create.py` | the `new` subcommand (file keeps its old name) — empty repo with safe defaults. `run()` is also the engine for `push` (`args.local_path`) and `copy` (`args.from_repo`) |
| `commands/push.py` | `push <path> <owner/repo>` — sets `args.local_path` and delegates to `create.run` |
| `commands/copy.py` | `copy <src-owner/repo> <owner/repo>` — sets `args.from_repo` and delegates to `create.run` |
| `commands/audit.py` | `audit <owner/repo>` — calls `fix.run(args, audit=True)`: dry run, no prompt, exit 3 on drift (`ADD`/`UPDATE`/`DELETE` rows only; `PRESERVE` is not drift) |
| `commands/fix.py` | `fix` subcommand — audit existing repo, show diff, apply corrections; `run(args, audit=False)`. `apply_merge_policy(plan, config, all_=False)` is the three-way merge (the only place that knows the policy): per UPDATE row, `old` vs `github_default` decides whether a person changed it, then `tier` + `is_stricter()` + `config.is_declared(section)` turn it into `PRESERVE` or flag it `weakened`; `--all` returns the plan unchanged. A second pass turns an `UPDATE` listed in `DEPENDENT_SETTINGS` into `SKIP` when its parent row was preserved at a value where the dependent has no effect. Applied to every plugin plan before display/apply and passed to `verify_plugins(policy=)` so the re-read is judged the same way. `add_all_arg()` is shared with `audit` |
| `commands/scan.py` | `scan` subcommand — local-only secret scanning |
| `github_client.py` | Wrapper around `gh api` (subprocess); `copy_repo()`, `push_local()` (bare-clones so all branches push; pushes and sets `default_branch`; returns a `LocalWiring` describing the default branch, the `origin`/upstream setup on the user's repo, a foreign `origin` left untouched, and the pushed `{branch: sha}` / `{tag: sha}` maps), `copy_repo(default_branch=)` sets the source's default after the mirror, `matching_refs(owner, repo, "heads"|"tags")` (`{name: sha}` via `git/matching-refs`, paginated with `get_json_pages()`; 409 empty repo → `{}`), `clone_for_scan()`; `git_remote_url()` builds URLs per `gh config get git_protocol`; `verify_git_credentials()` pre-flight probes SSH |
| `git_transport.py` | Frozen `GitTransport` (protocol, env, URLs, redaction, `preflight()`, `run_remote()` which retries a missed `*-SK` touch once) built by `discover_transport()`; `security_key_hint()` names a missed `*-SK` hardware-key touch in git errors, and `GitTransport.auth_hint()` adds the directory the SSH settings were read from for a plain publickey failure |
| `local_repo.py` | `is_git_checkout()`: the one test for whether a source path is a git checkout (a `.git` directory, or the `.git` file of a linked worktree / submodule; #103), used by `push` validation, `push_local`, the scanner and branch detection. `external_git_dir()`: the repository a `.git` file points at, mounted into container truffleHog |
| `config_manager.py` | INI config parsing via `configparser`; holds `SAFE_DEFAULTS`, plus the merge metadata: `MERGE_SECTIONS` (the five repo-state sections; `[pre_flight_scan]`/`[git_transport]` are tool-run behaviour and excluded), `SETTING_TIERS` (`(section, key)` → `security`/`preference`, keyed by plan row), `SAFE_DIRECTIONS` (which value is stricter: a strict boolean, `"higher"`, `"subset"`, or an enum tuple weakest → strictest), `setting_tier()`, `is_stricter()`, `has_direction()`, and `DEPENDENT_SETTINGS` (`(section, key)` → `(parent key, parent value it needs)`; today the three selected-actions sub-settings on `allowed_actions == "selected"`). `ConfigManager.declared_sections` / `is_declared()` are the sections named in the user's own INI file (explicit intent), as opposed to values seeded from `SAFE_DEFAULTS` |
| `actions_usage.py` | Actions in use (#111): `plan_warnings(plan, baseline, repo_owner, source=, client=, skip_reason=)` returns the `PlanWarning`s for one Actions plan: `whitelist_warnings()` (patterns an `UPDATE` drops) and, when an actionable row touches a `POLICY_KEYS` setting, `collect_usages(source)` (line-based `parse_uses()` + `classify()` over `.github/workflows/*.yml|yaml`, following local composite actions) evaluated with `evaluate(ref, policy, repo_owner)` before vs after (`policy_after()`); only newly blocked / newly unknown refs are reported, with `resolve_pin()` remediation SHAs (≤ `MAX_PIN_LOOKUPS`). Sources: `ApiSource` (contents API, read-only; `fix`/`audit`/`copy`) and `GitSource` (`git ls-tree`/`git show` on the local default branch; `push`). Never changes the plan's rows |
| `diff.py` | `Change` and `Plan` dataclasses; `count_by_type()`. `PlanWarning` (`kind`, `message`, `details`; `to_dict()`) and `Plan.warnings` (carried by `merge()`), the advisory list `print_plan()` and `format_plan_json()` render. `ChangeType.PRESERVE` plus the merge fields on `Change` (`tier`, `github_default`, `weakened`); `actionable_changes` excludes both `SKIP` and `PRESERVE`, `preserved_changes` lists the latter |
| `verify.py` | Post-apply verification (#77): `Check` / `Verification` result model (`ok`, `has_fatal`, `exit_code()`, `to_dict()` = the `--json` `verification` block), `verify_plugins()` (re-audits plugins after apply, `fix`), `Probe` + `verify()` (plugin audits and dedicated reads in one set of 1s/2s/4s retry rounds), and `create_probes()` / `verify_create()` (`create`: repo exists, visibility, README removed, `copy` description/topics, protection presence; repo-exists runs first and short-circuits), plus the `push`/`copy` probes passed as `extra_probes`: `pushed_ref_probes()` / `mirrored_ref_probes()` (fatal, refs via the API), `default_branch_probe()` and `local_wiring_probes()` (advisory, local git config, `retry=False`) |
| `errors.py` | Custom exception hierarchy (`GhSafeRepoError`, etc.) |
| `security_scanner.py` | Pre-flight scanner: truffleHog dispatch, regex fallback, `_unified_walk()` (tracked files only in git repos, NUL-byte binary sniff, TTY progress line). `scan()` records the engine that ran (`engine_used`, `fell_back`, `fallback_reason`); `engine_error()` turns a fallback under explicit `native`/`docker` into an error message |
| `plugins/base.py` | Abstract `BasePlugin` — defines the `fetch_current_state(strict=False)` / `plan()` / `apply()` interface; class attrs `section` / `github_defaults`, `annotate(change)` (tags a per-key row with `tier` + `github_default`), `preserved_values(plan)` (the `{key: current}` a composite write must carry); module-level `ruleset_enforced(ruleset)` (only `enforcement: "active"` counts as protection, shared by the branch and tag plugins) |
| `plugins/repository.py` | Repo creation (`POST /user/repos`) and basic repo settings (`PATCH`) |
| `plugins/actions.py` | GitHub Actions permissions (allowed actions, workflow perms, SHA pinning) |
| `plugins/branch_protection.py` | Branch protection via Rulesets API (default; classic per-branch path behind `use_rulesets = false`) |
| `plugins/security.py` | Dependabot alerts/security updates, secret scanning/push protection, private vuln reporting |
| `plugins/tag_protection.py` | Immutable tags via Rulesets API (prevent deletion/rewriting of release tags) |
| `templates/` | File templates (currently empty) |

## Global vs. command options

`--config`, `--no-config` and `--debug` are parsed on either side of the command name. They are
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

Plugins are two-way diffs and stay that way. The three-way merge that `fix`/`audit`
run on an existing repo (`commands/fix.py:apply_merge_policy`) sits between
`plan()` and `apply()`:

- `plan()` calls `self.annotate(change)` on every per-key row, which sets
  `change.tier` (from `config_manager.setting_tier(self.section, key)`) and
  `change.github_default` (from `self.github_defaults`, the module's
  `GITHUB_DEFAULTS`). Rows without a GitHub default (create-only rows,
  plan-limit `SKIP`s) stay unannotated and pass through the policy.
- The policy turns an `UPDATE` into `PRESERVE` or sets `weakened`. `PRESERVE`
  is not in `plan.actionable_changes`, so `apply()` never writes it and
  `verify_plugins(policy=...)` never expects it to have changed.
- **Composite writes must honour `PRESERVE`.** Branch and tag protection are
  written as one ruleset body built from `_desired()`, not from the rows. Their
  `apply()` overlays `self.preserved_values(plan)` on the desired dict first, so
  fixing `allow_force_pushes` does not reset a preserved
  `required_approving_reviews = 3` to `1`. Any new plugin that writes a body
  wider than its actionable rows must do the same.
- `create` never calls the policy.

```
cli.main()
  │
  ├─ each plugin.plan()  →  Plan (list of Change)
  ├─ print plan table
  ├─ each plugin.apply() →  API calls for non-SKIP changes
  └─ verify_plugins()    →  fetch_current_state(strict=True) + plan() again (fix)
     verify_create()     →  the same, plus create_probes() for CREATE_ONLY_KEYS (create)
```

### Plugin contract for verification

`fix` and the creating commands verify an apply by running the same plugin's audit again
(`verify.verify_plugins()` / `verify.verify_create()`), so every plugin must
satisfy two rules:

- **The post-apply audit plan must be empty.** After a successful `apply()`,
  `plan(current_state=fetch_current_state())` returns only `SKIP` rows. Any
  other row is reported as a `mismatch` (expected = `new`, actual = `old`) and
  makes `fix` exit `3`. A new setting whose audit cannot express "done" (it
  always emits an `UPDATE`) breaks this; a create-only row the audit never
  emits must be listed in `verify.CREATE_ONLY_KEYS` **and** get a probe in
  `verify.create_probes()`, or `create` silently never checks it. `create`
  passes its create-mode plan (no `current_state`) per plugin; the audit is
  still `fetch_current_state(strict=True)` + `plan(current_state=...)`, and the
  create-mode `ADD` rows map to the audit's rows by `(category, key)`.
- **`fetch_current_state(strict=True)` must not guess.** When a read cannot
  tell "off" from "could not read" (403, 5xx, missing status, unparseable
  body), raise `APIError` instead of returning GitHub's default. Only a
  definite answer may be returned: a 2xx, or the endpoint's documented 404 for
  "off". The verifier retries and then reports `unknown`, which warns but does
  not fail the run. `strict=False` (planning) keeps the lenient behaviour.
  Reads must not be served from a stale cache: the verifier calls
  `client.forget_repo()` first, so read `GET /repos` via `get_repo_data()`.

Presence reads used by the creating commands' probes follow the same strictness:
`BranchProtectionPlugin.protected_branches()` (the ruleset's include list, or
the branches whose classic protection answers 200) and
`TagProtectionPlugin.protected_tags()` return `[]` only on a definite "none"
and raise `APIError` otherwise. A `verify.Probe` is `(category, key,
expected, read, matches=None, fatal=False, remediation)`; `read()` raising
means unknown. `verify_create(..., extra_probes=...)` is the hook for further
create checks; `commands/create.py:_code_probes()` uses it for the code push
(`code/branches`, `code/tags`: fatal), `repo/default_branch` (exit 3) and the
local wiring (`local/origin`, `local/tracking`). A probe (and its `Check`) can
be `advisory=True`: a mismatch is reported but never changes the exit code
(`Verification.has_mismatch` ignores it; `advisories` lists it).
`retry=False` settles after one read, for local state that cannot be
eventually consistent. A probe's `read()` may fill in `expected` on its first
attempt (the `copy` source refs).

## Adding a new setting

1. Identify the GitHub API endpoint.
2. Add the key and safe default to `config_manager.py:SAFE_DEFAULTS`.
3. Add the corresponding entry (with a comment) to `gh-safe-repo.ini.example` in the repo root.
   `test_config_consistency.py` will fail if the example file and `SAFE_DEFAULTS` diverge.
4. Add its tier to `config_manager.py:SETTING_TIERS` and, for a security-tier
   setting, its direction to `SAFE_DIRECTIONS` (`tests/test_merge_policy.py`
   fails otherwise). Add the GitHub default to the plugin's `GITHUB_DEFAULTS`.
5. Update the appropriate plugin's `plan()` (wrap per-key rows in `self.annotate(...)`)
   and `apply()` methods.
6. Add tests in `tests/test_plugins.py` — all `subprocess` calls must be mocked.

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
  `prose_to_stderr(json_mode)`, and prompts through `ask()` (or `confirm()`
  for a `[y/N]` that must fail, not decline, on EOF).
