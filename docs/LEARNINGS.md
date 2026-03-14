# Implementation Learnings

Technical notes accumulated during development. Moved from CLAUDE.md to keep the LLM context lean. These are preserved for reference — consult them when working on related areas.

## Phase 1 (MVP)

**`allow_merge_commit` is an additional safe default.** GitHub defaults it to `true`; we set it to `false`. This wasn't in the original planning docs but is now applied and tested.

**`gh api` body passing pattern.** JSON request bodies are passed via `--input -` with the JSON written to stdin — not via `--field` flags. `--field` only handles simple key=value pairs and doesn't support nested objects. See `gh_safe_repo/github_client.py:call_api()`.

**Status code parsing is regex on stderr.** `gh api` exits non-zero on 4xx/5xx but doesn't expose the HTTP status code cleanly — it's embedded in stderr text. We regex-match `HTTP (\d{3})` with a fallback to any 3-digit number. This is fragile; if `gh` changes its error format it will break. See `gh_safe_repo/github_client.py:_parse_status()`.

**`GH_TOKEN` env var in subprocess calls.** When calling `gh api` via subprocess, the token is injected as `GH_TOKEN` in the child process environment. This works whether the token came from `gh auth token` or `GITHUB_TOKEN`. The child `gh` process picks it up without needing interactive auth.

**`--dry-run` skips the repo-exists check.** No API calls are made at all during a dry-run — including the GET that checks whether the repo already exists. This is intentional: dry-run is a pure planning operation. The trade-off is that a dry-run won't warn you if the repo already exists.

## Phase 2

**`enforce_admins = false` is the correct default for owner workflows.** `enforce_admins = false` means the repo owner's token bypasses branch protection rules, which is intentional — protect against external contributors, not the owner's own tooling. This matters for `--from` mirror pushes and `--local` pushes where the owner pushes directly to the default branch.

**Secret scanning requires no API call for public repos.** GitHub enables it automatically on all public repositories. We model this as a SKIP with reason "Automatically enabled for public repositories by GitHub" rather than an ADD — so it appears in the plan output for visibility but makes no API call. See `gh_safe_repo/plugins/security.py`.

**Git copy auth uses `x-access-token` in the HTTPS URL.** The pattern `https://x-access-token:{token}@github.com/{owner}/{repo}.git` works for `git clone` and `git push` without any SSH setup. The token is never logged — debug output uses a sanitised URL without the credential. See `gh_safe_repo/github_client.py:copy_repo()`.

**`is_public` is derived from config after overrides, not from `args.public` directly.** The main executable computes `is_public = not config.getbool('repo', 'private', fallback=True)` after `apply_overrides()` has run. This means both `--public` on the CLI and `private = false` in the config file produce the correct behaviour.

**`--from` must be enforced to require `--public` early.** If `--from` is passed without `--public`, argparse raises an error immediately before any API calls are made. Allowing `--from` to a private repo would silently push code to a destination that has no branch protection — not what we want.

## Phase 3

**Scan runs before `print_plan()`, not after.** The pre-flight scan is called in `main()` after source-repo validation but before the plugins build the plan. This means the user sees scan findings and either aborts or continues — then sees the full plan table. If the user aborts, we never do the work of running all the plugins. The SCAN entry still appears in the plan table (it is added unconditionally during the plan-build phase) so the dry-run plan correctly shows the scan as a pending step.

**truffleHog exit code 1 means "findings found", not "error".** `returncode == 0` means no findings; `returncode == 1` means findings were found (both are valid). Any other return code (e.g., wrong version, crash) is treated as failure and falls back to regex. This matches truffleHog v3 behaviour; v2 has a different exit-code convention, which is why the fallback triggers for v2 installs.

**`_try_trufflehog` returns `None` vs `[]` to distinguish failure from "no findings".** Returning `None` tells `scan()` to fall back to regex for secrets. Returning `[]` (empty list) tells `scan()` that truffleHog ran successfully and found nothing — regex still runs for emails/TODOs but not for secrets. This avoids double-reporting secrets when both tools are available.

**`_try_trufflehog` routes to `trufflehog git` vs `trufflehog filesystem` based on `.git` presence.** When the scanned path contains a `.git` directory it is a real repo; we use `trufflehog git file://<path>` so truffleHog walks the full commit history. For a plain directory (e.g. `--scan` on an arbitrary folder) we fall back to `trufflehog filesystem`. The JSON output differs: git mode uses `SourceMetadata.Data.Git`; filesystem mode uses `SourceMetadata.Data.Filesystem`. The parser tries both keys.

**`FakeConfig` in scanner tests avoids coupling to `ConfigManager`.** The scanner only calls `config.getbool()` and `config.get()` — so a lightweight dict-backed `FakeConfig` in the test file is cleaner than instantiating a real `ConfigManager` with a temp INI file. The pattern also makes it easy to override individual keys per test via the `overrides` dict.

**Decimal `max_file_size_mb` values need `float()` before `int()`.** `configparser` returns all values as strings. `int("0.001")` raises `ValueError`; `int(float("0.001") * 1024 * 1024)` works correctly. This matters for tests that use small thresholds (like `0.001` MB) to avoid writing actual large files to disk.

**`ChangeCategory.SCAN` is needed for plan output, not just for completeness.** The `print_plan()` table uses `change.category.value` as a display column. Without the SCAN category, the plan entry for the pre-flight scan would need to be forced into an existing category (e.g., FILE), which would be misleading. The new category makes the plan output self-describing.

**`clone_for_scan` uses a full clone (no `--depth`), unlike `copy_repo` which uses `--mirror`.** truffleHog's `git` subcommand must walk the full commit graph to find secrets that were introduced and later deleted — a shallow clone would miss them entirely. `copy_repo` uses `--mirror` to faithfully replicate all refs and history. The two methods are intentionally separate with different clone strategies.

## Phase 4

**`lib/` → `gh_safe_repo/` is a mechanical rename — internal relative imports are unchanged.** Relative imports inside the package (`from ..diff import ...`, `from .base import ...`) didn't need touching. Only the test files and the thin `gh-safe-repo` launcher needed updating from `lib.xxx` to `gh_safe_repo.xxx`.

**`GET /user` is now cached on `GitHubClient`.** `get_owner()` and `get_plan_name()` both previously called `GET /user` independently. A private `_get_user()` method now fetches once and stores the result in `self._user_data`; both methods delegate to it.

**`_build_ruleset_body()` is separate from `apply()` to keep tests clean.** Extracting the ruleset body construction into its own method allows `test_ruleset_body_includes_pr_rule` and `test_ruleset_body_admin_bypass_when_enforce_admins_false` to call `_build_ruleset_body(desired)` directly without going through `apply()`. This avoids needing a mock client just to inspect the body shape.

**Secret scanning on private paid repos uses a different API path than Dependabot.** Dependabot is enabled via `PUT /repos/{owner}/{repo}/vulnerability-alerts` (no body). Secret scanning is enabled via `PATCH /repos/{owner}/{repo}` with `{"security_and_analysis": {"secret_scanning": {"status": "enabled"}}}`. These are completely different endpoints; the `apply()` dispatch in `SecurityPlugin` keys on `change.key` to route correctly.

**pyproject.toml `[dependency-groups]` is a UV-native feature (PEP 735).** It's not the same as `[project.optional-dependencies]`. UV resolves dev deps from `[dependency-groups]` natively; `pip install -e .[dev]` would not see them. This is intentional — the tool has no runtime dependencies at all, only a dev dependency on pytest.

**`sys.path.insert(0, ...)` in the thin launcher is needed for uninstalled use.** When running `./gh-safe-repo` directly from the repo root (without `uv tool install .` or `pip install -e .`), Python won't know where `gh_safe_repo` is. The one-liner `sys.path.insert(0, str(Path(__file__).parent))` makes the script runnable in both modes — directly from the repo and as an installed tool.

## Post-Phase 4

**`_unified_walk()` replaces four separate `os.walk()` calls.** `_scan_large_files`, `_scan_regex`, `_walk_text_files`, and `_scan_ai_context_files` were consolidated into a single `_unified_walk(root_path, scan_secrets)` that covers all checks in one tree traversal. Large files emit a finding and `continue` — they are not read into memory for content scanning. `SKIP_DIRS` is now applied consistently across all checks.

**AI context file detection uses filename matching, not content patterns.** `_unified_walk` flags `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `copilot-instructions.md`, and `.cursor` by filename/dirname with `CRITICAL` severity. This is distinct from truffleHog's content-pattern scanning and cannot be replaced by it. The check is controlled by `warn_ai_context_files` in `[pre_flight_scan]` (default `true`). The finding includes a remediation hint in the `match` field, which `_print_findings` renders inline. See `gh_safe_repo/security_scanner.py`.

**`scanner.skipped_committed_dirs` surfaces scan coverage gaps.** Any `SKIP_DIRS` subdirectory (e.g. `node_modules`, `dist`) that actually exists in the scan target is collected during `_unified_walk()` and stored as a sorted list on the scanner instance after `scan()` returns. `run_preflight_scan()` in `cli.py` checks this and prints a yellow warning if any are present — in clone context the presence implies they were committed. `.git` is excluded since it is always expected.

**`--audit` now runs the pre-flight scan.** Before building the settings plan, `--audit` clones the target repo and runs `run_preflight_scan()`. This catches AI context files and secrets before applying settings. The scan is skipped in `--dry-run` mode (consistent with the no-API-calls dry-run contract).

## AI Context History Check

**`git log --all --full-history --oneline -- <path>` detects deleted-from-history files.** Any stdout output means the path existed in at least one commit. This works for both files and directories because git tracks by path string regardless of type.

**`_AI_CONTEXT_HISTORY_CANDIDATES` uses exact paths, not basename matching.** `git log -- <path>` requires an exact path relative to the repo root, so the case-insensitive basename matching used in `_unified_walk` is not appropriate here.

**Integration tests that exercise git use real subprocess calls, not mocks.** `make_git_repo()` and `git_add_commit()` run actual `git` commands in a temp directory. `git init` (without `-b main`) keeps the tests compatible with git < 2.28.

## `--local` Source Flag

**`push_local()` uses `git clone <local_path>` not `--mirror` for git repos.** `copy_repo()` uses `--mirror` because all refs (including remotes) must be preserved. `push_local()` uses a regular clone so the working copy has a clean ref structure; then `push --all --tags` pushes all local branches and tags to the new remote.

**`git diff --cached --quiet` distinguishes empty from non-empty staged trees.** For plain directories (no `.git`), after `git init` + `git add -A`, exit code 0 means nothing was staged (empty directory) and we return early.

**`_scan_findings_prompt()` is the right extraction boundary.** The display + prompt tail is identical between `run_preflight_scan` (clone-based) and `run_preflight_scan_local` (direct path).

**Mutual exclusion checks before `ConfigManager` means no config file needed for error path.** Tests for `--local/--from` and `--local/--audit` conflicts need no auth mocking — `SystemExit(2)` is raised immediately from argparse.

**`local_default_branch` feeds `_resolve_branches()` as `source_default_branch`.** This puts the local repo's HEAD branch at priority 2, so branch protection targets the right branch.

**`git clone <local_path>` creates an `origin` remote pointing at the local path — `remote add` fails.** Fix: use `git remote set-url origin <github_url>` in the temp clone (for `is_git_repo`), keep `git remote add` for the fresh-init path.

**Remote wiring on the original `local_path` is a separate, post-push step.** Non-fatal (wrapped in `try/except CalledProcessError`) so a pre-existing `origin` doesn't abort the workflow. The public URL (no token) is used.

**`auto_init` must be `false` when `--local` or `--from` is used.** The config default is `auto_init = true`, which makes GitHub create an initial commit. When code is pushed immediately afterward, the push is rejected. Fix: `RepositoryPlugin` accepts an `auto_init: bool = None` constructor parameter.

## `--json` Output

**`format_plan_json()` is a pure function on `Plan` — no mocking needed in tests.** Tests construct `Plan` objects directly with `Change` dataclasses, call the function, and `json.loads()` the result.

**`info()` redirect to stderr keeps stdout clean for piping.** When `--json` is active, all `info()` calls write to `sys.stderr` so that only the JSON blob lands on stdout.

**`summary` only includes change types that are present.** Consumers must use `.get("delete", 0)` etc. rather than assuming all four keys are always present.

## `sha_pinning_required`

**`sha_pinning_required` goes to `PUT /repos/{owner}/{repo}/actions/permissions`, not the `/workflow` subpath.** The `apply()` dispatch in `ActionsPlugin` splits changes into `perms_body` (for `/actions/permissions`) and `workflow_body` (for `/workflow`) and only makes calls for non-empty bodies.

**`enabled: true` must be included alongside `sha_pinning_required` in the request body.** The `/actions/permissions` `PUT` schema marks `enabled` as required. Omitting it causes a 422.

**`sha_pinning_required` is not plan-gated or visibility-gated.** The API spec confirms `githubCloudOnly: false` with no free/paid distinction.

**`fetch_current_state()` now makes two GET calls.** Audit mode reads `sha_pinning_required` from `GET /actions/permissions` and the workflow fields from `GET /actions/permissions/workflow`.

## truffleHog UX Improvements

**`trufflehog_mode` replaces `use_trufflehog` in SAFE_DEFAULTS.** Values: `auto` / `native` / `docker` / `off`. Backwards-compat: if `use_trufflehog = false` is present in the user's config file and `trufflehog_mode` is still `auto`, `SecurityScanner.__init__` detects it and overrides the mode to `off`.

**`_run_discovery()` is the single source of truth for scanner selection.** It follows the discovery chain (native → container → warn + none) and caches the result in `self._discovery`.

**`scanner_description` triggers discovery eagerly on first access.** When building the plan table in create mode, a `SecurityScanner` is created early so its description can be embedded in the SCAN change's `new` field.

**Container subprocess mirrors the shell wrapper's volume-mount logic.** The scan path and optional config file are mounted read-only at the same absolute path inside the container. `TRUFFLEHOG_IMAGE` and `CONTAINER_RUNTIME` env vars are honoured.

**Tests set `_discovery` directly to bypass subprocess calls.** Pre-seeding `scanner._discovery = {"method": "native", "version": "3.99.0"}` avoids mocking `subprocess.run` for the version check.

## Scan Whitelisting

**`scan_exclude_paths` uses regex, matching via `re.search(rel_path)`.** truffleHog's `--exclude-paths` also takes newline-separated regexes — the same strings are written to a temp file and passed to both native and container invocations.

**`.cursor` directory exclusion is handled separately from file exclusion.** The `.cursor` check runs at the directory level (iterating `dirs`), so `_is_excluded()` is called on the computed `rel_path_dir` before appending the finding.

**`email_ignore_domains` is an exact case-insensitive domain match.** The domain is extracted as `email.split("@", 1)[1].lower()` and checked against a `frozenset`. Subdomains are not matched.

**Both new config keys use the same comma/newline split pattern as `banned_strings`.** `re.split(r"[\n,]", raw)` with strip and empty-drop.

## `scrub-ai-context.sh`

**Uses `git filter-branch`, not `git filter-repo`.** `filter-branch` is a git built-in; `filter-repo` requires a separate install.

**Multiple targets are removed in one `filter-branch` pass.** The index-filter is a single `git rm --cached --ignore-unmatch -r <path1> <path2> ...` string.

**`AT_HEAD` parallel array distinguishes present vs. history-only targets.** Targets absent from the working tree are filtered but not re-added.

## Branch Protection Ordering

**`bp_plugin.apply()` must run after `push_local()` / `copy_repo()`, not before.** Classic branch protection requires the branch to exist. For `--local` and `--from`, `auto_init=False` means the repo is empty at creation time. Moved `bp_plugin.apply()` to the end of the apply sequence in `cli.py`.

## Security Plugin Expansion

**Toggle endpoint status checks must use `200 <= status < 300`, not `status == 204`.** `GET /repos/{owner}/{repo}/vulnerability-alerts` returns 204 when enabled, but `GET /repos/{owner}/{repo}/private-vulnerability-reporting` returns 200. Meanwhile, `gh api` sometimes infers status 200 from exit code 0 when stderr has no explicit `HTTP` line. The original `status == 204` check was silently wrong for some endpoints. Fixed by checking the 2xx range.

**Private vulnerability reporting uses a dedicated `PUT` endpoint, not `security_and_analysis`.** Despite appearing in the GitHub UI alongside `security_and_analysis` settings, the REST API spec has `PUT /repos/{owner}/{repo}/private-vulnerability-reporting` as a standalone toggle (204 on success). It is *not* a field in the `PATCH /repos` `security_and_analysis` body.

**Push protection is the only new setting that goes into `security_and_analysis`.** `secret_scanning_push_protection` is a valid field in the `PATCH /repos` `security_and_analysis` body. It is batched with `secret_scanning` in a single PATCH call in `apply()`.

**Dependabot security updates use `PUT /repos/{owner}/{repo}/automated-security-fixes`.** Same pattern as `vulnerability-alerts` — PUT to enable, DELETE to disable, GET to check status. Returns 200 (not 204) when checking status.

**Grouped security updates has no REST API.** The GitHub API spec (`api.github.com.json`) has no per-repo endpoint for this setting. It exists only as a field in org-level code-security configurations (`/orgs/{org}/code-security/configurations`) and in the UI. For per-repo control, use `dependabot.yml` groups with `applies-to: security-updates`.

**Automatic dependency submission and dependency graph have no writable per-repo REST API.** Both exist as fields in org-level code-security configurations but not as per-repo endpoints. `dependency_graph_autosubmit_action` appeared to succeed when sent via `PATCH /repos security_and_analysis` but was silently ignored — the setting did not change. Dependency graph is auto-enabled for public repos.

**The `api.github.com.json` spec file is the authoritative source for endpoint validation.** Browser UI traces show the correct setting names but may use internal endpoints (`/_api/` or CSRF-protected forms) that differ from the public REST API. Always verify against the spec before implementing.

## `--from` Description and Topics Copy

**`description` goes into `PATCH_FIELDS`; topics needs a separate `PUT /topics`.** Topics is a different API surface and must be dispatched separately after the PATCH call.

**`source_data = client.get_repo_data(owner, from_repo)` is free (cached).** Only the topics `GET /repos/{owner}/{from_repo}/topics` is a new API call.

**Description and topics changes are emitted only in create mode, not audit mode.** `is_audit = current_state is not None` gates both additions.
