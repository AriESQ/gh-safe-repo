# Implementation Learnings

Technical notes accumulated during development. Moved from CLAUDE.md to keep the LLM context lean. These are preserved for reference — consult them when working on related areas.

## Phase 1 (MVP)

**`allow_merge_commit` was reverted to match GitHub's default (`true`).** Originally set to `false` as a safe default, it was later removed because disabling merge commits is a workflow preference, not a security setting. Users who want squash-only can set `allow_merge_commit = false` in their config. **GitHub requires at least one merge strategy to be enabled** — disabling all three returns a 422. `repo_settings()` now validates this and raises `ConfigError` before any API call.

**`gh api` body passing pattern.** JSON request bodies are passed via `--input -` with the JSON written to stdin — not via `--field` flags. `--field` only handles simple key=value pairs and doesn't support nested objects. See `gh_safe_repo/github_client.py:call_api()`.

**Status code parsing is regex on stderr.** `gh api` exits non-zero on 4xx/5xx but doesn't expose the HTTP status code cleanly — it's embedded in stderr text. We regex-match `HTTP (\d{3})` with a fallback to any 3-digit number. This is fragile; if `gh` changes its error format it will break. See `gh_safe_repo/github_client.py:_parse_status()`.

**`GH_TOKEN` env var in subprocess calls.** When calling `gh api` via subprocess, the token is injected as `GH_TOKEN` in the child process environment. This works whether the token came from `gh auth token` or `GITHUB_TOKEN`. The child `gh` process picks it up without needing interactive auth.

**`--dry-run` skips the repo-exists check.** No API calls are made at all during a dry-run — including the GET that checks whether the repo already exists. This is intentional: dry-run is a pure planning operation. The trade-off is that a dry-run won't warn you if the repo already exists.

**`PATCH /repos` can 404 immediately after `POST /user/repos` succeeds.** GitHub's repo creation is eventually consistent — the repo object exists but may not be routable for settings updates for a brief window. `RepositoryPlugin.apply()` retries the PATCH up to 3 times with exponential backoff (1s, 2s, 4s) when it follows a creation POST. The retry only applies in the create path (`has_create`); standalone `fix` PATCHes are not retried.

## Phase 2

**`enforce_admins = false` is the correct default for owner workflows.** `enforce_admins = false` means the repo owner's token bypasses branch protection rules, which is intentional — protect against external contributors, not the owner's own tooling. This matters for `--from` mirror pushes and `--local` pushes where the owner pushes directly to the default branch.

**Secret scanning requires no API call for public repos.** GitHub enables it automatically on all public repositories. We model this as a SKIP with reason "Automatically enabled for public repositories by GitHub" rather than an ADD — so it appears in the plan output for visibility but makes no API call. See `gh_safe_repo/plugins/security.py`.

**Git operations use the user's git credentials, not the OAuth token.** Originally we injected the `gh` OAuth token into HTTPS URLs (`https://x-access-token:{token}@github.com/...`). That works for plain code, but pushing a `.github/workflows/*.yml` is rejected unless the OAuth App token has the `workflow` scope — even when the user pushes workflow files via SSH every day without issue. We now respect `gh config get git_protocol` (`ssh` → `git@github.com:owner/repo.git`, otherwise plain HTTPS via credential helper). `verify_git_credentials()` runs `ssh -T git@github.com` before any API call when `--local` or `--from` is used, so we fail fast instead of leaving an empty repo behind. See `gh_safe_repo/github_client.py:git_remote_url()`/`verify_git_credentials()`.

**`is_public` is derived from config after overrides, not from `args.public` directly.** The main executable computes `is_public = not config.getbool('repo', 'private', fallback=True)` after `apply_overrides()` has run. This means both `--public` on the CLI and `private = false` in the config file produce the correct behaviour.

**`--from` no longer requires `--public`.** The original constraint was removed because mirroring into a private repo is a valid use case (e.g., forking an upstream project into a private repo with safe defaults). The pre-flight scan runs regardless of destination visibility, so secrets are still caught. Branch protection is plan-gated (free+private repos skip it with a warning), which is the correct behaviour — the tool informs the user rather than refusing to run.

## Phase 3

**Scan runs before `print_plan()`, not after.** The pre-flight scan is called in `main()` after source-repo validation but before the plugins build the plan. This means the user sees scan findings and either aborts or continues — then sees the full plan table. If the user aborts, we never do the work of running all the plugins. The SCAN entry still appears in the plan table (it is added unconditionally during the plan-build phase) so the dry-run plan correctly shows the scan as a pending step.

**truffleHog exit code 1 means "findings found", not "error".** `returncode == 0` means no findings; `returncode == 1` means findings were found (both are valid). Any other return code (e.g., wrong version, crash) is treated as failure and falls back to regex. This matches truffleHog v3 behaviour; v2 has a different exit-code convention, which is why the fallback triggers for v2 installs.

**`_try_trufflehog` returns `None` vs `[]` to distinguish failure from "no findings".** Returning `None` tells `scan()` to fall back to regex for secrets. Returning `[]` (empty list) tells `scan()` that truffleHog ran successfully and found nothing — regex still runs for emails/TODOs but not for secrets. This avoids double-reporting secrets when both tools are available.

**`_try_trufflehog` routes to `trufflehog git` vs `trufflehog filesystem` based on `.git` presence.** When the scanned path contains a `.git` directory it is a real repo; we use `trufflehog git file://<path>` so truffleHog walks the full commit history. For a plain directory (e.g. `--scan` on an arbitrary folder) we fall back to `trufflehog filesystem`. The JSON output differs: git mode uses `SourceMetadata.Data.Git`; filesystem mode uses `SourceMetadata.Data.Filesystem`. The parser tries both keys.

**`FakeConfig` in scanner tests avoids coupling to `ConfigManager`.** The scanner only calls `config.getbool()` and `config.get()` — so a lightweight dict-backed `FakeConfig` in the test file is cleaner than instantiating a real `ConfigManager` with a temp INI file. The pattern also makes it easy to override individual keys per test via the `overrides` dict.

**Decimal `max_file_size_mb` values need `float()` before `int()`.** `configparser` returns all values as strings. `int("0.001")` raises `ValueError`; `int(float("0.001") * 1024 * 1024)` works correctly. This matters for tests that use small thresholds (like `0.001` MB) to avoid writing actual large files to disk.

**`ChangeCategory.SCAN` is needed for plan output, not just for completeness.** The `print_plan()` table uses `change.category.value` as a display column. Without the SCAN category, the plan entry for the pre-flight scan would need to be forced into an existing category (e.g., FILE), which would be misleading. The new category makes the plan output self-describing.

**`clone_for_scan` uses a full clone (no `--depth`), unlike `copy_repo` which uses `--bare`.** truffleHog's `git` subcommand must walk the full commit graph to find secrets that were introduced and later deleted — a shallow clone would miss them entirely. `copy_repo` originally used `--mirror` to replicate all refs; since #171 it uses `--bare` (every branch and tag, no `refs/pull/*`). The two methods are intentionally separate with different clone strategies.

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

## CLI redesign (new/push/copy/audit/fix/scan)

**The verbs changed, the engines did not.** `push` and `copy` are ~30-line modules that parse two positionals, set `args.local_path` / `args.from_repo`, and call `create.run`; `audit` sets `dry_run=True` and calls `fix.run(args, audit=True)`. Every branch in `create.run` that used to key off `--local`/`--from` still keys off those two attributes, so the push, scan, verification and recovery paths were untouched. `create` itself declares `set_defaults(local_path=None, from_repo=None)` so the engine never needs `getattr`.

**argparse cannot group subcommands, so the grouped list lives in `description` and the flat `commands` group is dropped from `parser._action_groups`.** `help=argparse.SUPPRESS` on `add_parser` is not an option: Python 3.14 renders it as the literal `==SUPPRESS==`. Removing the group only affects `--help`; the subparsers action is still registered and parsing is unchanged.

**`TestNoDeadFlags` follows `args`-taking callables into their defining module.** The thin verbs reference almost none of their flags directly; the test now walks from `push.run` to `create.run` to `build_context` and `resolve_config_arg`, so a flag accepted by `push` but read nowhere is still caught. `inspect.getsource` raises `TypeError` on `typing` aliases found in module globals; skip those.

**`audit` exits `3` on drift, not `1`.** `3` already means "GitHub holds something other than the desired state" after a `create`/`fix`; drift found before applying is the same statement. `1` stays "the tool could not run", which is what a CI job needs to tell apart (story #14). `scan` keeps `1` for findings for compatibility; it predates the convention.

## `--local` Source Flag (historical; now `publish`)

**`push_local()` uses `git clone <local_path>` not `--mirror` for git repos.** `copy_repo()` used `--mirror` at the time (replaced by `--bare` plus explicit refspecs in #171, because a mirror also carries GitHub's unpushable `refs/pull/*`). `push_local()` uses a regular clone so the working copy has a clean ref structure; then `push --all --tags` pushes all local branches and tags to the new remote.

**`git diff --cached --quiet` distinguishes empty from non-empty staged trees.** For plain directories (no `.git`), after `git init` + `git add -A`, exit code 0 means nothing was staged (empty directory) and we return early.

**`_scan_findings_prompt()` is the right extraction boundary.** The display + prompt tail is identical between `run_preflight_scan` (clone-based) and `run_preflight_scan_local` (direct path).

**Mutual exclusion checks before `ConfigManager` means no config file needed for error path.** Tests for `--local/--from` and `--local/--audit` conflicts need no auth mocking — `SystemExit(2)` is raised immediately from argparse.

**`local_default_branch` feeds `_resolve_branches()` as `source_default_branch`.** This puts the `--local` path's HEAD branch at priority 2, so branch protection targets the right branch. The local CWD is never consulted — only the explicit `--local PATH` directory (via `git -C`).

**`git clone <local_path>` creates an `origin` remote pointing at the local path — `remote add` fails.** Fix: use `git remote set-url origin <github_url>` in the temp clone (for `is_git_repo`), keep `git remote add` for the fresh-init path.

**Remote wiring on the original `local_path` is a separate, post-push step.** Non-fatal (wrapped in `try/except CalledProcessError`) so a pre-existing `origin` doesn't abort the workflow. The public URL (no token) is used.

**`auto_init` must be `false` when `--local` or `--from` is used.** With `auto_init = true` GitHub creates an initial commit; when code is pushed immediately afterward, the push is rejected. Fix: `RepositoryPlugin` accepts an `auto_init: bool = None` constructor parameter, and `create.py` forces `False` for the `--local`/`--from` paths.

**Plain `create` removes the README that `auto_init` creates (issue #54, Option D).** A branch cannot exist without a commit, and a commit needs content — so to get a default branch at create time (branch protection needs one, and GitHub assigns the account's preferred branch name from the initial commit) we must POST `auto_init=true`, which creates a `README.md`. To keep the repo clean, `create.py` computes `suppress_readme = is_plain_create and not config.getbool("repo", "auto_init")` and passes it to `RepositoryPlugin`; `remove_readme()` does a `GET .../contents/README.md` (404-retry backoff for post-create eventual consistency) then `DELETE` on the captured `created_default_branch`. It's best-effort — `create.py` wraps it and `warn()`s on failure so a delete hiccup doesn't fail the whole create. The `auto_init` config key (default `false`) now gates whether the README survives; alternatives (`.gitkeep` via Contents API, low-level git-data empty-tree commit, or local `git commit --allow-empty`) were rejected because the file-less ones force *us* to name the branch, losing the account's default-branch-name preference, and drag git transport onto the API-only plain-create path.

## `--json` Output

**`format_plan_json()` is a pure function on `Plan` — no mocking needed in tests.** Tests construct `Plan` objects directly with `Change` dataclasses, call the function, and `json.loads()` the result.

**`info()` redirect to stderr keeps stdout clean for piping.** When `--json` is active, all `info()` calls write to `sys.stderr` so that only the JSON blob lands on stdout.

**`summary` only includes change types that are present.** Consumers must use `.get("delete", 0)` etc. rather than assuming all four keys are always present.

## `sha_pinning_required`

**`sha_pinning_required` goes to `PUT /repos/{owner}/{repo}/actions/permissions`, not the `/workflow` subpath.** The `apply()` dispatch in `ActionsPlugin` splits changes into `perms_body` (for `/actions/permissions`) and `workflow_body` (for `/workflow`) and only makes calls for non-empty bodies.

**`enabled: true` must be included alongside `sha_pinning_required` in the request body.** The `/actions/permissions` `PUT` schema marks `enabled` as required. Omitting it causes a 422.

**`sha_pinning_required` is not plan-gated or visibility-gated.** The API spec confirms `githubCloudOnly: false` with no free/paid distinction.

**`fetch_current_state()` makes two or three GET calls.** Audit mode reads `allowed_actions` and `sha_pinning_required` from `GET /actions/permissions`, the workflow fields from `GET /actions/permissions/workflow`, and — when `allowed_actions` is `"selected"` — the sub-settings (`github_owned_allowed`, `verified_allowed`, `patterns_allowed`) from `GET /actions/permissions/selected-actions`.

**`allowed_actions` and selected-actions sub-settings use the same `/actions/permissions` PUT.** `allowed_actions` is sent alongside `sha_pinning_required` and `enabled: true` to `PUT /actions/permissions`. The sub-settings (`github_owned_allowed`, `verified_allowed`, `patterns_allowed`) go to a separate `PUT /actions/permissions/selected-actions` endpoint. The `selected-actions` endpoint only works when `allowed_actions` is already `"selected"` — apply order matters (permissions first, then selected-actions).

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

**Now uses `git filter-repo` (via `git-filter-file.sh`); originally used `git filter-branch`.** `filter-branch` was first chosen because it ships with git (zero dependencies). It was replaced (#52) because git's own documentation warns against it (many pitfalls, very slow) and recommends git-filter-repo. The dependency cost is small: the script uses an installed `git filter-repo` if present, otherwise `uvx git-filter-repo`, which needs no install beyond uv. It never falls back to `filter-branch`.

**git-filter-repo needs `--force` outside a fresh clone.** It refuses to run unless the repo looks freshly cloned (one reflog entry, no stashes, packed). These tools run on the user's working repo and do their own checks (worktree guard, clean tree, remote sync, confirmation), so `--force` is passed deliberately.

**git-filter-repo removes the `origin` remote.** After a non-`--partial` rewrite it deletes `origin` (and its tracking refs) so rewritten history is not pushed by accident. Because the whole point here is to force-push back to the same remote, `git-filter-file.sh` saves `remote.*` and `branch.*.{remote,merge,pushremote}` config first, restores what was removed, then fetches so `--force-with-lease` has real tracking refs. This also keeps `scrub-ai-context.sh --push` and its later per-target passes working.

**`--force-with-lease --tags` rejects rewritten tags (`stale info`).** There are no remote-tracking refs for tags, so the lease check fails. This happened with `filter-branch` too; it is not specific to git-filter-repo.

**Fix (#135): an explicit lease per tag, recorded before the rewrite.** `--force-with-lease=<ref>` with no value compares against `refs/remotes/<remote>/...`; git maintains those for branches only, so for a tag there is nothing to compare and the push is refused. `--force-with-lease=refs/tags/<t>:<sha>` supplies the expected value directly, and an empty value (`refs/tags/<t>:`) means "must not exist". `git-filter-file.sh` now runs `git ls-remote --tags <remote>` right after its up-front fetch (the remote is known to be reachable then), keeps each `refs/tags/<t>` line's SHA (for an annotated tag that is the tag object, which is what the remote ref holds; the `^{}` peeled line only marks it as annotated), and after the rewrite prints a single `git push <leases...> <remote> <refspecs...>` for every tag that differs, also saved to `.git/filter-file-push-tags.sh` (printed as `bash <file>` instead of inline above 10 tags). Tag names may contain `$`, `'`, `&` and similar, so the command is shell-quoted. Things learned on the way:
- Tags whose rewritten SHA equals the remote's are left out; git would report them as up to date anyway.
- git-filter-repo deletes a tag when every commit it could point to is pruned (e.g. a branchless commit that held only the file). The remote copy still reaches the file, so the push deletes it (`:refs/tags/<t>` with a lease). A tag that is only on the remote, and was never local, is left alone and listed: we cannot tell what it reaches.
- A commit that only added the file is pruned and its tag moves to the parent. In the safety test, moving the remote tag to that same parent made the push a silent no-op rather than a rejection; the test moves it elsewhere.
- Both fetches now use `--no-tags`. Tag auto-following would otherwise bring back, with the old history, a tag that an unpushed rewrite deleted (the remote still has it and the tracking refs still reach it), which would then look "unchanged" and never be deleted remotely.
- `scrub-ai-context.sh` runs one rewrite per target. Each pass only sees the previous pass's output, so it would miss a tag pass 1 deleted. The wrapper sets `GIT_FILTER_FILE_TAG_SNAPSHOT` to a temp file: pass 1 writes the remote and local tag snapshot there, later passes reuse it, and the last pass's push file covers every pass. `--push` runs that file and is skipped if any target failed.
- When the remote tags cannot be recorded (fetch declined, or no remote `git push` would pick), there is no safe lease; the script says so and prints `git ls-remote --tags` plus `git push --force <remote> --tags` to run after checking.

**Each target is a separate rewrite pass.** `scrub-ai-context.sh` calls `git-filter-file.sh` once per target (the earlier version built a single `git rm --cached --ignore-unmatch -r <path1> <path2> ...` index-filter for one `filter-branch` pass).

**`AT_HEAD` parallel array distinguishes present vs. history-only targets.** Targets absent from the working tree are filtered but not re-added.

## Visibility Changes and Rulesets

**Branch and tag rulesets survive a public → private → public round-trip.** Tested 2026-03-28: creating rulesets (target `"branch"` and `"tag"`) on a free-plan public repo, flipping to private, then back to public — both rulesets retained their original IDs, enforcement status (`active`), and rules. GitHub's "All push rulesets will be disabled" warning during the private→public transition refers specifically to rulesets with target `"push"` (file size/path/extension restrictions, Team+ only), not branch or tag rulesets.

**Free+private blocks all ruleset API access, not just creation.** `GET /repos/{owner}/{repo}/rulesets` returns 403 on free+private repos — you cannot list, read, or create rulesets. The rulesets are not deleted, just inaccessible until the repo is public again (or the plan is upgraded). The `fix` command's existing plan-gating (SKIP on free+private) already handles this correctly.

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

## Config Consistency

**`SAFE_DEFAULTS` must stay in sync with `gh-safe-repo.ini.example` — enforced by `test_config_consistency.py`.** Before this test existed, several keys consumed by `security_scanner.py` (`scan_email_history`, `exclude_emails`, `warn_ai_context_files`, `scan_exclude_paths`) relied on hardcoded `fallback=` values in the code but had no entry in `SAFE_DEFAULTS`. The `actions.enabled` key was in `SAFE_DEFAULTS` but missing from the example file. The test suite catches three classes of drift: missing/extra sections or keys, value mismatches, and config reads with neither a `SAFE_DEFAULTS` entry nor an explicit `fallback=`.

## `fix` Non-Owner Repo Support

**`fix` skips the `build_context()` owner check and verifies admin permissions from the repo API response instead.** The original owner check (`actual_owner.lower() != expected_owner.lower()`) was a UX guard for multi-account systems, not a security mechanism. For `fix`, requiring ownership is too strict — users need to fix org repos and repos where they are collaborators with admin access. The `permissions.admin` field from `GET /repos/{owner}/{repo}` is the correct check because admin access is what's actually needed to modify repo settings.

**`fix --debug` emits the resolved repo identity (id, full_name, owner_type) after fetching repo data.** When multiple repos share the same name under different owners (e.g. `user/quest` and `org/quest`), the standard debug output (`[debug] GET /repos/{owner}/{repo}`) doesn't confirm which concrete repo was resolved. The identity line (`[debug] repo: owner/repo (id=N, owner_type=User|Organization)`) makes this immediately visible.

**A 404 on `GET /repos/{owner}/{repo}` is ambiguous and the error message must say so.** GitHub returns 404 (not 403) for private repos a credential can't see — deliberate, to avoid disclosing their existence. So a 404 means *either* the repo doesn't exist *or* it's private and the active credential can't see it. This bites multi-account users: with the wrong `gh` account active (e.g. `rootnotez` active while the repo is private under the inactive `AriESQ` persona), the old message ("Repository '...' does not exist. Use create...") was a misdiagnosis. The fix names the active account (`ctx.owner`, already fetched via `GET /user` in `build_context`) and hints the user may be authenticated as the wrong account. We deliberately do **not** parse `gh auth status` to name the *right* account (fragile, no JSON output) and do **not** print explicit `gh auth switch` / `GITHUB_TOKEN` commands — just the hint. The message keeps the `does not exist` substring so the e2e contract (`tests/e2e/test_live_api.py`) holds.

**Auditing a public repo you don't own is not feasible via the API path — deferred.** `fix` requires `permissions.admin`, and that's not just to *modify*: most settings/security read endpoints (branch protection, `actions/permissions`, security-and-analysis) return 403/404 without admin, so even a read-only `--dry-run` audit of a non-owned public repo would have most plugins fail to fetch state. Hence `fix` keeps the admin gate and the message reads "required to read or modify". A genuine third-party audit would be a different, file/clone-based feature (closer to `scan`), not a small change to `fix`.

## Git Transport Refactor (Rec A + Rec B)

**Background:** Nine commits between March and May 2026 patched git transport bugs in `github_client.py`. Each was correct in isolation but each new user setup (per-directory `core.sshCommand`, HTTPS without a credential helper, SAML SSO orgs, GCM browser hangs) produced a new patch. The design review at `docs/2026-05-06_auth-architecture-review.md` traced 13 user personas and found 5 structural gaps. This refactor implements Rec A (introduce `GitTransport`) and Rec B (unify preflight). Rec C (CI/token escape hatch) was implemented as a follow-up (see "Git Transport Mode" below); Rec D (persona test matrix) is deferred.

**`gh_safe_repo/git_transport.py` is now the single owner of git transport state.** `GitTransport` is a frozen dataclass holding `protocol`, `source_dir`, `ssh_command`, `has_credential_helper`, and `host`. `discover_transport(source_dir)` constructs one by inspecting `gh config get -h <host> git_protocol` and the source dir's `git config core.sshCommand` / `credential.helper`. `GitHubClient.transport` is set by the caller (currently only `commands/create.py`) and used by `copy_repo` / `push_local` / `clone_for_scan`.

**`transport.env()` is what fixes P1.** The user's per-directory `core.sshCommand` (set via `includeIf "gitdir:..."`) is captured at discovery time from the source dir, then propagated as `GIT_SSH_COMMAND` into every git subprocess — including those that run in a temp dir under `/var/folders/...`, where the original `includeIf` would no longer match.

**HTTPS preflight env vars are non-obvious but correct.**
- `GIT_TERMINAL_PROMPT=0` (Git 2.3+) makes git fail fast with "could not read Username" instead of blocking on a TTY username prompt.
- `GCM_INTERACTIVE=false` (modern GCM; legacy `never` still works) blocks Git Credential Manager from showing GUI/TTY prompts.
- `GCM_GUI_PROMPT=false` blocks just the browser/window even if interactive prompts are otherwise enabled.

Without these, an HTTPS preflight against a GCM-using machine with an expired token would launch a browser and hang the subprocess. The transport sets all three on every HTTPS subprocess.

**`transport.preflight()` runs the same transport as the real push.** Probes `git ls-remote git@github.com:gh-safe-repo-preflight/nonexistent.git` (or HTTPS equivalent). Maps "Repository not found" → success, "Permission denied / publickey" → SSH AuthError, "could not read Username / Authentication failed" → HTTPS AuthError with hint that varies based on whether `credential.helper` is configured.

**`source_dir` for `--from` is `os.getcwd()`, not the to-be-cloned remote.** The user has no local checkout yet, but their shell-CWD `includeIf` would match the same way it does for any other git command they run from that shell. This is the semantically-correct mapping.

**Tests:** `tests/test_git_transport.py` covers URL construction, env propagation, run cwd semantics, preflight error parsing for SSH and HTTPS, and `discover_transport` factory behavior. The existing `test_github_client.py` push/clone tests continue working unchanged because `transport.run()` internally calls `subprocess.run` — `mock_run.call_args_list` inspection still sees the same git commands. Only `TestVerifyGitCredentials` and `TestGitRemoteUrl` were deleted (their concerns moved to `test_git_transport.py`).

**What this does NOT fix (deferred to follow-ups):**
- P4 / P6 (CI / GitHub App with token but no SSH key / no helper) — requires Rec C (since implemented, see below)
- P7 (GHES) — `host` field is plumbed through but not exposed

## Git Transport Mode (Rec C)

**`[git_transport] mode = auto | user_creds | token`** restores a push path for CI/headless environments (P4, P6) without re-introducing the P2 workflow-scope bug that got token injection removed in commit b822e9c.

**The `auto` fallback heuristic is deliberately narrow.** Token-in-URL injection only happens when *all* of: mode is `auto`, protocol is HTTPS, no `credential.helper` is configured (any scope), and a token is available. SSH users (P1, P2, P10) can never be switched to token injection — `gh config git_protocol = ssh` short-circuits the fallback entirely. This is what protects P2: their push stays on their own SSH key, which has no workflow-scope restriction. The design review suggested probing `git credential fill` for detection; we deliberately don't — invoking helpers can itself prompt or hang, while checking config presence is inert.

**`mode = token` skips discovery entirely.** No `gh config` / `git config` subprocess calls; protocol is forced to HTTPS. Errors if no token is available. CI sets `GITHUB_TOKEN` and either relies on `auto` (no helper present on runners → fallback fires) or pins `mode = token` for explicitness.

**`mode = user_creds` is the P2 pin.** Guarantees the API token never touches git, even on HTTPS with no helper — push fails fast at preflight instead, with the credential-helper hint.

**Token redaction is a hard requirement once tokens can appear in URLs.** Three leak paths were closed:
1. `transport.run()` debug output prints the full git command — redacted via `transport.redact()`.
2. `APIError` messages in `copy_repo`/`push_local`/`clone_for_scan` embed remote URLs and git stderr (git echoes the full URL in "unable to access" errors) — all pass through `redact()`.
3. **`push_local`'s convenience step writes `origin` into the user's long-lived `.git/config`** — this must use `transport.persistent_url()` (clean URL), never `remote_url()` (token-injected). A token in the user's repo config would outlive the process and the token's own validity.

**Preflight in token mode names the scopes.** The P4/P6 friction (per the user-stories doc, story #14) is that token scope requirements are trial-and-error. The token-mode auth failure explicitly names `repo` and `workflow` scopes.

**`[git_transport]` is tool-run behavior, not repo state.** Like `[pre_flight_scan]`, it must stay out of any future fix-enforcement / settings-tier logic (the three-way-merge design in `docs/2026-03-28-user-stories.md` on the `user-stories-design` branch).
- P8 (identity-mismatch warning) — flagged as a one-line follow-up
- P9 (multi-account `gh auth` with different protocols per account)
- The persona test matrix (Rec D)

Each of these is a self-contained follow-up branch off `master` once `GitTransport` is merged.

## Non-Interactive / Agent Surface

**The dangerous bug class here is "exit 0 without doing the work."** `--yes` skipped the final confirmation but not the pre-flight scan prompt, so `_scan_findings_prompt` still called `input()`. Headless, the `EOFError` handler returned `False` and `create.py` printed "Aborted by user." and exited **0** — indistinguishable from success to a script, a CI job, or an agent. A warning-severity finding was enough to trigger it, even though the interactive prompt for warnings defaults to *yes*. The fix is not "suppress the prompt": it is to make the two failure modes distinguishable.

**`ScanDecision` exists because a bool cannot carry that distinction.** `proceed=False` alone conflates "a human looked at the findings and declined" (a normal outcome, exit 0) with "we could not ask, or must not proceed unattended" (a failure, exit 1). `_scan_findings_prompt` returns `ScanDecision(proceed, blocked, reason)`; `create._abort_on_scan()` is the only place that maps it to an exit code. Any new caller of the pre-flight path must go through that helper rather than re-deriving the rule.

**Unattended policy mirrors the prompt defaults, except for criticals.** `--yes` accepts warnings (the interactive prompt is `[Y/n]`) but always blocks on criticals (the prompt is `[y/N]`, and an unattended run must never push a leaked secret because nobody was watching). The no-TTY case without `--yes` blocks on *any* finding — the user never opted into anything.

**Non-TTY detection is on `sys.stdin`, not stdout.** The question is whether a prompt can be *answered*; a piped stdout with a live terminal on stdin is still interactive. Colour suppression asks the opposite question and keys off `sys.stdout.isatty()` (plus `NO_COLOR`), evaluated once at import in `commands/_common.py` so the constants themselves go empty — that way the many direct `f"{_BOLD}..."` uses are covered without auditing every call site. `security_scanner._WARN` does the same for stderr.

**The final confirmation prompt had the same bug (#93).** `create` and `fix` caught `(EOFError, KeyboardInterrupt)` together and exited 0, so `fix OWNER/REPO < /dev/null` printed the plan and "succeeded" with nothing applied. `_common.confirm()` now owns that prompt for both commands: EOF → error naming `--yes`, exit 1; `n`/empty line/Ctrl-C → `False` (the caller prints "Aborted." and exits 0). Unlike the scan prompt, it does **not** pre-check `sys.stdin.isatty()`: an answer read from a pipe (`echo y | fix ...`) is an explicit answer and is honoured, and only "no answer at all" (EOF) is treated as unattended. The cost is that a pipe that never closes and never writes still blocks on `input()`; that is the caller holding stdin open, not something the tool can tell apart from a slow human. Ctrl-C *at the prompt* stays a decline (exit 0) rather than the global 130, matching the scan prompt — the user is answering the question, not interrupting work in progress.

**truffleHog writes its progress log to stderr**, which is what makes `scan --json` safe to pipe. Worth re-checking if the scanner ever gains another engine.

**The skill is documentation with no failure mode.** `skills/gh-safe-repo/SKILL.md` is the contract agents get *instead of* the README — but no test covers it and no human reads it, so it rots silently and then actively teaches wrong flags. It lives outside `.claude/skills/` deliberately: that path auto-loads, which would activate the "how to use the CLI" skill for everyone working *on* the CLI.

## Upstream Tracking After `push_local` (#70)

`push_local` pushes from a temp clone, so the user's own repo never sees the
push and has no `refs/remotes/origin/*`. `git branch --set-upstream-to=origin/<b>`
requires that ref and fails; it was run with `check=False`, so the failure was
invisible and repos were left with `origin` but no upstream. Setting
`branch.<b>.merge` config alone is not enough either: `git status` then reports
"the upstream is gone". The fix reads the SHAs actually pushed
(`for-each-ref refs/heads` in the temp clone) and writes them with `update-ref`
into the user's repo, which makes the state identical to a post-push fetch
without a network round trip or credentials. Hints use
`git push -u origin <b>:<b>`: it works from any state and the explicit refspec
cannot follow a stray upstream onto the default branch.

## Failed Code Push Is Fatal (#79)

For `--local`/`--from` the push is the point of the run, so a push failure
exits 1 instead of warning, unlike the settings plugins, where a partial
result is still a usable repo that `fix` can converge. By then the repo
already exists, so re-running `create` cannot recover; the error prints
recovery commands instead. `push_local` raises before it wires `origin` into
the user's repo, so the recovery adds `origin` itself.

Hardware-backed SSH keys (`*-SK`) need a touch for every signature. A missed
touch shows up as `sign_and_send_pubkey: signing failed for ED25519-SK ...:
invalid format`, then git's stock "check your access rights" text, which
points at the wrong cause. `git_transport.security_key_hint()` matches that
string. A passing pre-flight only proves one touch happened; it does not
guarantee the later push will get one.

Resolved (#79 item 4) without trying to make pre-flight durable, which it
cannot be: the check still runs, but after the scan and the confirmation
prompt, just before the repo is created, so the gap to the push is seconds,
not minutes. `push_local` sends every branch and tag in one `git push` with
explicit refspecs (`refs/heads/<b>:refs/heads/<b>` per branch, default
first, plus `refs/tags/*:refs/tags/*`). Three pushes meant three touches,
and a miss on the second left a half-pushed repo that the recovery text
called empty. Remote git calls go through `GitTransport.run_remote()`, which
retries once when `security_key_hint()` matches and re-raises anything else.
The `discover_transport()` call stays early because the `--from` scan
clones through it.

## `push_local` Pushed Only One Branch (#86)

A plain `git clone <local>` creates a local head only for the source's
checked-out branch; every other branch exists only as
`refs/remotes/origin/*`, which `git push --all` ignores. Running
`create --local` from a feature branch therefore published just that
branch, and GitHub made it the default (the first branch pushed to an
empty repo becomes the default). The temp clone is now `--bare`, which
copies all heads as `refs/heads/*`. The default branch is chosen as
`init.defaultBranch` → `main` → `master` → current branch, pushed alone
first, then set explicitly with `PATCH /repos {default_branch}` instead of
relying on push order.

## Worktrees and Submodules Have a `.git` File (#103)

In a linked worktree `.git` is a file (`gitdir: <main>/.git/worktrees/<name>`);
in a submodule it is `gitdir: ../.git/modules/<name>`. Validation tested
`exists(.git)` but everything after it tested `isdir(.git)`, so such a source
took `push_local`'s non-git path: `copytree` copied the `.git` file, and
`git init` / `add -A` / `commit` in the temp copy then acted on the **real**
repo through it (`git init` says "Reinitialized existing Git repository"),
committing the user's WIP onto their branch. One helper,
`local_repo.is_git_checkout()`, is now the only test (a `.git` dir, or a
`.git` file starting `gitdir:`); it is filesystem-only on purpose, because
`git -C PATH rev-parse` walks up into a parent repo. The non-git path also
copies with `ignore_patterns(".git")` so no `.git` entry at any depth can aim
git at another repository.

- `git clone --bare <worktree>` and `<submodule>` both work: clone follows
  the `.git` file. A worktree's bare clone has **all** of the main repo's
  heads (refs are shared); that is the intended "every local branch".
  A submodule's has only its own heads, not its upstream's remote-tracking
  refs.
- Wiring (`remote add origin`, `update-ref refs/remotes/origin/*`) from a
  worktree writes to the shared config and refs, so the main checkout gets
  the same `origin`.
- Container truffleHog mounts only the scanned dir, so the `.git` file
  pointed outside the container. `external_git_dir()` (`rev-parse
  --path-format=absolute --git-common-dir`, needs git ≥ 2.31) is mounted
  too. The common dir, not the per-worktree gitdir: the latter has no
  objects and sits inside the common dir anyway. The path is kept as git
  reports it, not `realpath`'d, since that is what the `.git` file names.

## Post-Apply Verification (#77)

Success used to mean "no exception escaped the apply loop". `fix` caught
every `APIError` from `apply()` as a warning and then printed a green
"Repository updated successfully!" regardless, so a run where every request
failed looked identical to one where every request worked. A request GitHub
accepts but ignores (the `security_and_analysis` PATCH without
`secret_scanning`, `dependency_graph_autosubmit_action`) also never raised.
`verify.py` now re-reads the settings and compares.

**The `GET /repos` cache is a trap for anything read after an apply.**
`GitHubClient.get_repo_data()` caches for the life of the process, and
`RepositoryPlugin` and `SecurityPlugin` read through it, so a post-apply
audit silently compared against the pre-apply state and reported every
`PATCH /repos` change as still pending. `get_repo_data(refresh=True)` and
`forget_repo()` exist for this; `verify._audit()` forgets the entry before
every read, including each retry. A failed refresh leaves the cache empty,
not stale.

**Planning reads and verification reads need different strictness.** When
planning, treating a 403 or 5xx from a toggle endpoint as "off" is harmless:
`fix` proposes the change and the PUT either works or warns. After an apply,
the same guess turns "could not read" into a false mismatch and exit `3`, and
an agent told to re-run `fix` would loop. `fetch_current_state(strict=True)`
raises instead of guessing (security toggles: anything but 2xx/404; tag and
branch rulesets: 403 and failed detail reads), and the verifier turns a read
that keeps failing into `unknown`, which warns but exits `0`. Planning keeps
`strict=False`, so `fix --dry-run` output is unchanged.

**Private vulnerability reporting is not available on free + private.** The
plan-limits tables said it was; `GET private-vulnerability-reporting` on a
free-plan private repo returns 404 (checked live 2026-10-14), so the plugin's
SKIP there is correct and the tables were wrong.

**…nor on paid + private: it is public-only.** GitHub's docs gate the feature
as "available for public repositories" (`data/reusables/gated-features/
private-vulnerability-reporting.md` in the docs source), with no plan that
extends it to private repos, and the REST spec lists `422` for the enable
`PUT`. The plugin used to plan that `PUT` for paid + private; it now SKIPs on
any private repo. `fetch_current_state()` still reads it (a 404 there reads
as off), so `audit` state is unchanged.

**`GET private-vulnerability-reporting` answers 200 when the feature is off.**
The body is `{"enabled": false}`. The bare 2xx check above (Security Plugin
Expansion) read that as enabled, so `fix` never proposed turning it on, and
a verifier would have passed a PUT that did not stick. `_is_enabled()` now
honours a boolean `enabled` field in a 2xx body and falls back to "2xx = on"
when there is none (`vulnerability-alerts` answers 204 with no body).

**Retry budget: 1s/2s/4s, in rounds, about 7s worst case per run.** GitHub is
briefly inconsistent after a write; the same budget already covers the PATCH
after `POST /user/repos` in `repository.py`. Retries run in rounds across all
pending plugins rather than per plugin, so five slow plugins still cost about
7s, not 35s. Only plugins whose applied plan had a non-SKIP row are read back;
an all-SKIP plugin (free+private branch protection, say) made no writes and is
never fetched. The cost is roughly one plugin audit per changed plugin, about
the same number of GETs as the planning pass.

**Why API reads, not `git ls-remote`.** Verification only uses REST GETs. Over
SSH, `ls-remote` is a network git operation that can cost a hardware-key touch,
and a missed touch there would show up as an unknown that says nothing about
the repo. The code-push checks in `create` follow the same rule:
read refs via `GET git/matching-refs/heads/` and `.../tags/`.

**`create` verifies with the create plan, re-audited.** `create` plans with
`current_state=None` (rows are `ADD` against GitHub defaults), so it keeps one
`Plan` per plugin and hands those to the verifier, which re-reads each plugin
with `fetch_current_state(strict=True)` + `plan(current_state=...)` exactly as
for `fix`. Rows are matched by `(category, key)`, so a create-mode `ADD` and
an audit-mode `UPDATE` line up. Rows only a create plan has (`repository`,
`description`, `topics`, `readme`, `protected_branches`, `protected_tags`)
are in `CREATE_ONLY_KEYS`; absence from the audit says nothing about them, so
each gets a dedicated `Probe` instead. The probes are built from the rows the
plan actually contained, so a SKIPped plugin or a plain create adds none.

**Visibility is fatal, not a mismatch.** Visibility is a `POST /user/repos`
field (`CREATE_FIELDS`), not a `PATCH_FIELDS` one, so the repository audit
never compares it. A repo meant to be private that came out public is a
disclosure that `fix` will not undo, so `repo/visibility` is a fatal probe
(exit `1`) with the exact `gh repo edit --visibility` command as remediation.
A fatal probe that cannot be read is still `unknown` (never fatal), keeping
"unknown never fails the run" true.

**Check the repo exists before anything else.** If `GET /repos` keeps
404ing, every plugin audit and probe would also fail, and running them in
the same rounds would only add noise; running the existence probe on its own
first costs nothing when it passes (one round), and when it fails the rest
are skipped, so the worst case stays at one ~7s budget.

**Presence is not the same as values.** If every protection value equals
GitHub's defaults (a user who turned everything off), the value audit has
nothing to compare, but `apply()` still creates the ruleset. The
`protected_branches` / `protected_tags` probes read the ruleset's
`conditions.ref_name.include` (or classic protection per branch), using the
already-narrowed `bp_plugin.branches`, not the plan row's `master, main`.

**`create --json` used to print before applying.** The plan document went to
stdout before the prompt, so a script got a plan with no outcome and could
not tell an applied run from a declined one. It now follows `fix`: one
document at the end with `verification`, the table on stderr before an
interactive prompt, the plain plan on decline, and nothing on stdout for an
exit-`1` abort before or during the apply (those already explain themselves
on stderr, and a plan document there would look like a result).

**Code push is verified against what was pushed, through the API.**
`push_local` already listed the bare clone's heads to write remote-tracking
refs (#70); it now also lists `refs/tags` and returns both maps on
`LocalWiring` (`pushed`, `tags`). After the apply, `GET
/repos/{o}/{r}/git/matching-refs/heads/` and `.../tags/` must have every
pushed name at the pushed SHA (`--from`: the source repo's refs, read the
same way). A missing ref or a different SHA is fatal (exit `1`): the push
step reported success, but the point of `--local`/`--from` was not achieved.
`git ls-remote` would answer the same question with one network git
connection per call, which over SSH with a hardware key is a touch the user
did not expect, so it is not used.

**Annotated tags: compare the tag object, not the commit.** For an annotated
tag, matching-refs reports `object.type = "tag"` and `object.sha` = the tag
object; the peeled commit is only reachable through a second `GET
git/tags/{sha}`. Locally, `for-each-ref --format=%(objectname)` gives the
same tag object SHA (`%(*objectname)` would give the peeled commit). Both
sides therefore use the ref's own target, and lightweight tags (`type:
"commit"`) compare equal the same way. A git push copies tag objects
verbatim (`--mirror` then, `refs/tags/*:refs/tags/*` since #171), so source
and destination tag SHAs match too.

**`refname:short` is ambiguous.** When a branch and a tag share a name,
`%(refname:short)` renders the branch as `heads/<name>`, which then became
the refspec `refs/heads/heads/<name>`. `_local_refs()` uses
`%(refname:lstrip=2)`.

**matching-refs pagination and empty repos.** The endpoint is paginated
(`per_page` up to 100, `Link` headers), so a repo with hundreds of branches
needs `gh api --paginate`. Without `--slurp` (not in older `gh`), gh prints
each page as its own JSON document, back to back: `[...][...]`, which
`json.loads` rejects. `get_json_pages()` decodes the output as a stream with
`JSONDecoder.raw_decode`. On a repository with no commits the git-data
endpoints answer 409 "Git Repository is empty" rather than `[]`;
`matching_refs()` maps that to `{}` so it reads as "nothing pushed" (a
mismatch), not as "could not read" (unknown).

**A mirror push has no default branch.** `push --mirror` (and the
explicit-refspec push that replaced it in #171) sends refs, not `HEAD`, so GitHub picked its own default for a `--from` repo, and branch
protection went to the name the empty repo was created with. `copy_repo()`
now PATCHes the source's `default_branch` afterwards (as `push_local` does
for `--local`), protection targets it, and `repo/default_branch` checks it
(exit `3`, `gh repo edit --default-branch`).

**Local wiring is advisory and read from config only.** `local/origin`
compares `git config remote.origin.url` with `transport.persistent_url()`
(what `_wire_local_repo` writes, never the token-bearing URL);
`local/tracking` compares `branch.<b>.remote` / `.merge`. `@{u}` is not used:
it also needs `refs/remotes/origin/<b>` and says nothing more about the
config. A mismatch never changes the exit code (`advisory`), because the new
repo is fine and the fix is one local command; probes are `retry=False`, as
local config is not eventually consistent and re-reading it would only spend
the 7s budget. An `origin` that already pointed elsewhere was left untouched
on purpose (#70): it is reported with that explanation, and no tracking
check is made, since any upstream would belong to the old remote.

**Patching `module.time.sleep` patches every sleep in the process (#109).**
The `create` tests counted retry rounds with
`patch("gh_safe_repo.verify.time.sleep")`, which resolves to the shared
`time` module and swaps its `sleep` attribute globally. The local-wiring
probes run real `git config` through `subprocess.run(..., timeout=5)`, and
with a timeout `Popen._wait()` polls `waitpid(WNOHANG)` with `time.sleep`
between attempts. Normally the child has exited by the time its pipes reach
EOF, but on a loaded runner it sometimes has not, so one poll landed on the
mock and a 3-round run counted 4 sleeps (CI, Python 3.14, once). A `git`
wrapper that closes its pipes and lingers 50ms before exiting reproduces it
every time. Tests now patch verify's own `time` reference
(`patch_verify_sleep()` in `tests/test_cli.py`) so stdlib sleeps are
untouched. The `plugins.repository.time.sleep` patches in `test_plugins.py`
share the shape but run no subprocess.

## Scan Engine Reporting (#77)

`scanner_description` is a plan-time guess: it runs discovery
(`trufflehog --version`, `which podman`) but not truffleHog itself. A
discovered engine can still fail at run time (non-0/1 exit, a container that
cannot start, a binary that vanished), and `_try_trufflehog` then returns
`None` and `scan()` quietly used regex, visible only under `--debug`. So the
plan row and scan header could say "truffleHog via podman" for a scan that
never ran it. `scan()` now records `engine_used` / `fell_back` /
`fallback_reason`, and both `scan` and the `create` pre-flight print a
`Scan engine:` line after the scan.

`native` and `docker` are strict (user decision on #77): a fallback is an
exit `1`, not a warning, because the user asked for a specific engine and
regex misses most credential types. In `create` this returns a blocked
`ScanDecision` before the findings prompt, so it rides the existing
`_abort_on_scan` exit-1 path and nothing is created. In strict modes the
discovery-time "falling back to regex" warnings are suppressed: they would
contradict the error that follows. An unrecognised `trufflehog_mode` value is
treated as strict too, rather than as `auto` (a typo of `native` should not
silently weaken the scan). `off` and `scan_for_secrets = false` are regex by
choice and report `fell_back: false`.

The strict-mode wording used to contradict itself (#153): the error said
"refusing to fall back to the regex scanner" while the report above it said
`Scan engine: regex (fell back ...)` and `No issues found.`. The findings are
kept, because the regex patterns run on every scan, truffleHog or not; what a
strict mode refuses is to count a regex-only scan as a pass. So the engine
line says `regex only (truffleHog failed: ...)`, an empty report says the
scan is incomplete, and `scan --json` carries the message in `engine.error`.

Under `create --json` the pre-flight report used to print to stdout ahead of
the plan document. It now runs inside `prose_to_stderr(json_mode)` (the shared
`_common.py` helper), so stdout carries only JSON.

## Credential Errors That Point the Wrong Way (#100)

Found in a live `create` test. Two failures were reported with the wrong
cause:

- **A pre-flight check that times out on an unanswered security-key touch**
  said "Network issue, or the credential helper is hanging". Over SSH a
  FIDO key prompt is the common reason, and 15s was too short for someone
  who has to notice the blink, now that pre-flight runs right after the scan
  (#89). SSH now gets 45s (`PREFLIGHT_TIMEOUT`); HTTPS stays at 15s, since
  there a hang means a stuck helper.
- **`--from` run outside the directory that sets `core.sshCommand`** fails
  with a bare `Permission denied (publickey)` and no key prompt at all: ssh
  used the default identity. `--from` has no local repo, so `source_dir` is
  the current directory, and an `includeIf`-scoped key doesn't apply
  elsewhere. `GitTransport.auth_hint()` adds the directory and the
  discovered `core.sshCommand` (or "not set") to publickey failures; a
  missed-touch hint takes priority when both apply.

## Three-Way Merge for `fix` / `audit` (PRESERVE, `--all`)

Implements the design in `docs/2026-03-28-user-stories.md` ("Architectural
Vision") and section 8 of `docs/2026-10-16_cli-interface-review.md`. The
policy is one function, `commands/fix.py:apply_merge_policy()`; the metadata
is two dicts in `config_manager.py` (`SETTING_TIERS`, `SAFE_DIRECTIONS`);
plugins only annotate rows (`BasePlugin.annotate()`).

**Decisions on the doc's open questions.** (1) Tier and direction are data
in `config_manager.py`, keyed by `(section, key)` where the key is the *plan
row* a plugin emits — for the security plugin that is `dependabot_alerts`,
not `enable_dependabot_alerts`, so both spellings are listed. A test checks
every `SAFE_DEFAULTS` key in a merge section has an explicit tier and every
security key a direction. (2) `PRESERVE` rows are always shown, in the table
and in JSON. (3) `--all` means "apply everything in the config regardless of
divergence", not a factory reset to `SAFE_DEFAULTS`. (4) A section declared
in the user's INI file wins over the policy, including over a
diverged-stricter security setting. Only sections present in the file count:
`ConfigManager` parses the file a second time just to record
`declared_sections`, because the main parser is pre-seeded with every
default section and cannot tell them apart.

**Composite settings: per-key rows, overlay on write.** Branch and tag
protection plugins already emit one row per key (`require_pull_request`,
`required_approving_reviews`, ...) and the policy works per key, so a hand
tuned ruleset can be partially preserved (reviews=3 kept, force-push rule
added). The catch is `apply()`: both plugins write one ruleset body built from
`_desired()`, not from the rows, so without care "fix allow_force_pushes"
would silently reset reviews from 3 to 1. `BasePlugin.preserved_values(plan)`
returns `{key: change.old}` for the plugin's `PRESERVE` rows and `apply()`
overlays it on the desired dict before building the body. The classic
branch-protection path does the same. Repository and Actions bodies are built
from actionable rows only, so a preserved key is simply absent there.

**Verification must run the policy too.** `verify_plugins()` re-runs each
plugin's audit after apply and treats any non-`SKIP` row as a mismatch. A
preserved `required_approving_reviews = 3` would re-audit as `UPDATE 3 → 1`
and fail the run with exit 3. `verify()` / `verify_plugins()` take
`policy=`, and `fix` passes the same `apply_merge_policy` closure, so the
re-read is judged exactly as the plan was. `create` passes nothing.

**`⚠ weakened` fires less often than the design example suggests.** The flag
needs the current value to differ from GitHub's default *and* be weaker than
ours. For most security settings GitHub's default is already the weakest
state (`allow_force_pushes = true`, `required_approving_reviews = 0`,
`allowed_actions = all`, `default_workflow_permissions = write`), so a weak
current value equals the default and is a plain `UPDATE` ("nobody chose
it"). The marker appears where the default is not the weakest value:
`verified_allowed` (default `false`), `patterns_allowed` (default empty, a
superset is weaker), and any setting where the user's config asks for more
than the safe default. The doc's `allow_force_pushes ... ⚠ weakened` line is
not reproducible under the base/theirs/ours definition and was not forced.

**Security plugin base is "off".** GitHub turns some toggles on by itself
(secret scanning on public repos, Dependabot alerts when the account opted
into "enable for new repositories") and the API cannot distinguish that from
a person doing it, so `SecurityPlugin.GITHUB_DEFAULTS` is `False` for every
key: a disabled toggle is enabled without the `weakened` marker, and the
security plugin behaves identically with and without `--all`.

**`allowed_actions` ordering.** `all < selected < local_only`: `local_only`
(only actions defined in the repo) admits less third-party code than
`selected` (GitHub-owned + verified creators + patterns). A repo on
`local_only` is therefore preserved against a config asking for `selected`.

**`[pre_flight_scan]` and `[git_transport]` are outside the merge** —
`MERGE_SECTIONS` lists the five repo-state sections and `setting_tier()`
returns `None` for anything else; a test pins that.

**Dependent settings must follow a preserved parent (found live, 2026-10-19).** The Actions plugin plans the selected-actions sub-settings whenever the *config* wants `allowed_actions = selected`, without looking at the current value, because before the merge policy the parent was always about to be set to `selected` in the same run and the sub-settings had to be written with it. The policy broke that assumption: it kept `allowed_actions` at `local_only` (stricter, so `PRESERVE`) and the orphaned `verified_allowed` row stayed `UPDATE`, so `audit` exited `3` for a setting GitHub ignores under `local_only` and `fix` would have PUT a selected-actions body that changes nothing. The fix is data plus one policy pass, not a plugin change: `config_manager.DEPENDENT_SETTINGS` maps a dependent `(section, key)` to `(parent key, parent value it needs)`, and `apply_merge_policy` turns a dependent `UPDATE` into `SKIP` when its parent came out `PRESERVE` at any other value. Only the preserved case is special: when the parent is `UPDATE` (at GitHub's default, declared section, or `--all`) the sub-settings are written with it exactly as before. The same table is the place for any future parent/child pair (e.g. a ruleset knob that only matters under `use_rulesets = true`).

## API Read Shapes (#94)

**Read the body, not the status code.** Several endpoints answer `200` when
a feature is off and put the real answer in the body. Each row below was
observed live on 2026-10-22. "On" is from AriESQ/gh-safe-repo (public),
"free+private" from a private repo on the free plan, and "off" from a
throwaway public repo. A test in `tests/test_plugins.py::TestApiReadShapes`
pins every row where the status alone would have been misread.
Full evidence (every endpoint, every state, the probe script) is in
`docs/2026-10-22_github-api-read-shapes.md`.

| Read | On | Off | Free + private |
|---|---|---|---|
| `GET …/vulnerability-alerts` | `204`, no body | `404` "Vulnerability alerts are disabled." | `404` (same) |
| `GET …/automated-security-fixes` | `200 {"enabled":true,"paused":false}` | `200 {"enabled":false,"paused":false}` | `200 {"enabled":false,…}` |
| `GET …/private-vulnerability-reporting` | `200 {"enabled":true}` | `200 {"enabled":false}` | `404` |
| `GET /repos` `security_and_analysis` | `{"status":"enabled"}` per key | `{"status":"disabled"}` per key; an admin can turn secret scanning off on a public repo | `null` |
| `GET …/actions/permissions` | `200 {"enabled":true,"allowed_actions":…,"sha_pinning_required":…}` | `200 {"enabled":false,"sha_pinning_required":false}` (no `allowed_actions`) | `200`, same shape |
| `GET …/actions/permissions/selected-actions` | `200 {"github_owned_allowed":…,"patterns_allowed":[…],"verified_allowed":…}` | `409` "GitHub Actions is disabled…" / "Only local actions…" when disabled or `local_only` | — |
| `GET …/actions/permissions/workflow` | `200 {"default_workflow_permissions":…,"can_approve_pull_request_reviews":…}` | — | `200`, same shape |
| `GET …/actions/permissions/fork-pr-contributor-approval` | `200 {"approval_policy":…}` | — | `422` "not allowed for private repositories" |
| `GET …/rulesets` | `200 [ {id,name,target,source_type,enforcement,…} ]` | `200 []` | `403` "Upgrade to GitHub Pro…" |
| `GET …/rulesets/{id}` | `enforcement: "active"` | `enforcement: "disabled"`, rules still listed (`evaluate` → `422` "not supported on this plan" outside Enterprise) | — |
| `GET …/branches/{b}/protection` | `200` + body | `404` "Branch not protected" / "Branch not found" | `403` "Upgrade to GitHub Pro…" |

Decisions that follow from the shapes:

- **`paused: true` counts as off.** GitHub pauses Dependabot security
  updates on inactive repos. While paused it opens no PRs, so `audit`
  reports drift and `fix` re-PUTs `automated-security-fixes`. Not verified
  live: GitHub decides when to pause, so whether the PUT un-pauses could not
  be reproduced.
- **Ruleset `enforcement` must be `active`** (`plugins/base.ruleset_enforced`).
  `disabled` and `evaluate` keep their rules but enforce none, and a missing
  field is not trusted (the spec marks it required). Both plugins read a
  disabled ruleset as GitHub's defaults, so `fix` writes our body, which
  carries `enforcement: active`, over the existing ruleset.
- **Ruleset updates are `PUT`.** The REST description lists only
  GET/PUT/DELETE on `rulesets/{id}`, and live `PATCH` answers **404**, so
  the branch plugin's `PATCH` meant `fix` could never update an existing
  branch ruleset. The tag plugin always `POST`ed, so a second `fix` on a
  drifted tag ruleset got `422` "Name must be unique". Both now find the existing id
  and `PUT`.
- **Actions disabled is left alone.** With `enabled: false` the
  permissions body has no `allowed_actions`, which used to default to
  `"all"`. `fix` then planned `selected` and PUT `enabled: true` (the
  endpoint requires the field), which switched Actions back on. Disabled is
  stricter than any policy, so the policy keys are `SKIP`. Workflow
  permissions and fork PR approval are still planned.
- **Public secret scanning is read, not assumed.** `security_and_analysis`
  is populated on public repos, so a `"disabled"` status is believed. Only
  a missing key falls back to "on". The dependency graph has no read
  endpoint, so it stays assumed on for public repos.
- **Null fields read as GitHub's default.** The spec marks only `enabled`
  required on `actions/permissions`, and nothing on `selected-actions`, so
  `actions._field()` type-checks each value before using it.

## Actions in Use: Warn Before a Policy Change Blocks Them (#111)

`gh_safe_repo/actions_usage.py` turns an Actions-policy change into
`PlanWarning`s (`diff.py`; `Plan.warnings`, merged by `Plan.merge()`,
rendered by `_common.print_plan_warnings()` after the table and emitted as
`warnings` by `format_plan_json()`). `fix.run()` calls `plan_warnings()` on
the Actions plugin's post-merge-policy plan; `create.run()` calls it on the
create-mode plan with `GITHUB_DEFAULTS` as the baseline. `plugins/actions.py`
is untouched: the scan is a consumer of its rows, not part of the diff.

**Decisions on #111's open questions (all "warn, change nothing"):**
(1) replace-and-warn, no union: `patterns_allowed` is still replaced by the
config value, and each dropped entry is a `whitelist_pattern_removed`
warning. A union would silently make the config non-authoritative; the
user can add the pattern to their config or leave `[actions]` undeclared so
the merge policy decides. (2) Unattended runs (`--yes`, weekly CI `fix`)
apply with the warning: no new flag, no new exit code; `--json --yes`
repeats the warnings on stderr so a CI log shows them. (3) Verified
creators: no REST endpoint says whether a publisher is verified, so a
reference allowed *only* via `verified_allowed` is `actions_unknown`, never
blocked. (4) Default branch only: one contents listing plus one GET per
workflow file (and per local composite action), versus a branch-times-files
fan-out. (5) Remediation: `GET /repos/{o}/{r}/commits/{ref}` resolves a tag
or branch to the commit SHA in one call (annotated tags peeled); at most
`MAX_PIN_LOOKUPS` (25) distinct refs, failures are silent.

**Only newly broken references are reported.** Each reference is evaluated
under the policy before (the repo's current Actions state, or GitHub's
defaults for a new repo) and after (before + the plan's actionable policy
rows). A reference already blocked today is not the change's fault and is
not listed. `PRESERVE` / `SKIP` rows do not change the after-policy, so a
preserved `local_only` triggers no scan.

**Whitelist semantics as implemented** (GitHub docs, "Allowing select
actions and reusable workflows to run"): under `selected`, local `./` / `$/`
actions, actions owned by the repo owner, GitHub-owned actions (`actions/*`,
`github/*`) when `github_owned_allowed`, and pattern matches run; a
`!pattern` (a blacklist entry) blocks. `local_only` allows only local and same-owner actions.
SHA pinning applies to every action including GitHub-owned ones, but
reusable workflows "can still be referenced by tag". All of this was
checked against live GitHub on 2026-10-26 (9 workflows × 6 policies, each
prediction compared with what GitHub ran; evidence in
`docs/2026-10-26_actions-whitelist-probe.md`):

- **A pattern is matched against the full `owner/repo[/path]@ref`.** The
  first implementation matched an `@`-less pattern against the name
  alone, on the strength of the #62 field note that `astral-sh/setup-uv`
  "worked". Live, GitHub refused `astral-sh/setup-uv@v5` under exactly
  that whitelist. The field repo's workflow really did run, but through
  `verified_allowed = true` (`astral-sh` is a verified creator); the bare
  entry matched nothing. `*` crosses `/` and `@` (`gradle/*` allowed
  `gradle/actions/setup-gradle@v4`). GitHub lowercases names in its error
  text; matching stays case-insensitive.
- **A `!pattern` blacklist entry overrides GitHub-owned.** `!actions/checkout@*` blocked
  checkout by tag and by SHA with `github_owned_allowed = true`.
- **Same-owner actions run under `selected` and `local_only`** without a
  pattern (an action in another repo of the same user account), and are
  still subject to SHA pinning.
- **Reusable workflows are exempt from SHA pinning** (an `@master`
  reusable workflow ran with pinning on).
- **`docker://` runs only under `all`.** It was blocked under `selected`
  (verified creators allowed too) and `local_only`, so it is `blocked`,
  not `unknown`.
- **Verified creators:** `astral-sh`, `gradle` and `hashicorp` all ran with
  only `verified_allowed = true`; they stay `unknown` because there is still
  no API to check that status.
- **Startup failures carry no reason in the REST API.** A blocked run ends
  `conclusion: startup_failure` with no jobs, no check runs and no logs;
  the "is not allowed" message is only on the run's web page.

**Line-based `uses:` parsing, no YAML.** `^\s*(?:-\s+)?uses\s*:` with
quotes and ` #` comments stripped; `${{ }}` expressions skipped. A flow
mapping (`- {uses: x}`) or a multi-line scalar is missed; acceptable for a
warning. Local composite actions are followed through `<dir>/action.yml` or
`action.yaml` (cached per directory, recursion guarded per chain, depth 3)
and attributed to every workflow that uses them; remote composite actions
are not fetched.

**`--dry-run` boundary.** The creating verbs keep "zero API calls": `copy
--dry-run` adds `actions_scan_skipped` instead of reading the source;
`push --dry-run` scans because it only runs `git ls-tree` / `git show`
locally (no pin lookups). `fix --dry-run` and `audit` scan, like the rest
of their audit: they already GET every setting. Tests for `GitSource` patch
`GitSource._git` in command-level tests rather than `subprocess.run`, which
is the shared module attribute the scanner's discovery also calls (the #109
lesson, in another shape).

## REST Description vs. Behaviour Tracker (#27)

Issue #27 asked for a workflow that discovers GitHub's settings surface and
generates a features file and ini defaults. That was reframed: the useful
signal is where the published REST description (OpenAPI, from
`github/rest-api-description`) and GitHub's real behaviour disagree for the
endpoints we call. Codegen and ini sync were dropped. The tracker is
`tools/api_drift/` (catalog + stdlib script + weekly workflow); usage is in
`tools/README.md`.

**Check the `2022-11-28` file, not the undated one.** `gh api` sends no
`X-GitHub-Api-Version`, so GitHub answers as its default, `2022-11-28`. The
undated `api.github.com.json` tracks the newest version, and so does
`docs/api.github.com.json` (the February copy used for endpoint research).
Diffing the catalog against the February copy shows `PATCH /repos` gaining
`has_pull_requests` and `pull_request_creation_policy` and
`GET commits/{ref}` gaining `429` since then: the detector works, and the
research copy in `docs/` is stale.

**The newer dated version (`2026-03-10`) drops fields we receive.** It
removes `has_downloads` and `use_squash_pr_title_as_default` from the repo
responses and adds `451` to `POST /user/repos`. Nothing in `gh_safe_repo`
reads either field, so a version bump would not break us today. The weekly
workflow prints this diff for information.

**Gaps found while building the catalog (spec as of
`rest-api-description@a5c38c1`, 2026-10-27), all from the #94 live
evidence:**

- `GET private-vulnerability-reporting` on a free-plan private repo answers
  `404`; the spec lists only `200` and `422`.
- `GET automated-security-fixes` is documented as `404` "if Dependabot is
  not enabled", but off and free+private both answer `200
  {"enabled":false}`; the `404` was never seen.
- `GET actions/permissions/selected-actions` answers `409` when Actions is
  disabled or `local_only`; the spec lists only `200`.
- `GET actions/permissions/fork-pr-contributor-approval` answers `422` on a
  private repo; the spec lists `200` and `404`.
- `GET rulesets` and `GET branches/{branch}/protection` answer `403`
  "Upgrade to GitHub Pro" on free+private; neither GET lists `403` (the
  classic protection PUT and DELETE do).
- `PATCH rulesets/{id}` is not in the spec at all, and GitHub answers `404`,
  not `405`, so it looks like "ruleset missing".
- The `patterns_allowed` description says the setting "only applies to
  public repositories". Not verified: the whitelist probe (#111) ran on
  public repos only, and the #62 private field repo got its action through
  `verified_allowed`, so it says nothing either way.

Several items people call "inconsistencies" are spec-consistent: the
toggle GETs' `204` vs `200` split is documented exactly; `enabled` is
documented as required on `PUT actions/permissions` (the undocumented part
is the `422` when it is missing, and the side effect of re-enabling
Actions); duplicate ruleset names fall under the generic `422`. The catalog
records them with `kind` so the report does not overstate the spec's
errors.

**A live GET probe of AriESQ/gh-safe-repo (2026-10-27) matched the spec on
every tracked GET:** documented status, no undocumented top-level fields,
no missing required fields. The gaps above only show in the off and
plan-unavailable states, which a GET probe of a hardened public repo
cannot reach; that is why mutation probes stay manual.

## Security Settings on Private Free Repos (2026-11-01)

Two live probes on scratch private repos on GitHub Free, checked against
GitHub's docs (`data/reusables/gated-features/`) and the REST description.
The full matrix, with every dated observation, is in
`docs/2026-11-01_security-settings-availability.md`.

**Dependabot alerts and security updates work on private Free.** `PUT
vulnerability-alerts` and `PUT automated-security-fixes` both answer 204 and
read back as on; real alerts arrived within about 75 s. The docs never gated
them ("available for organization-owned and user-owned repositories"; "All
repositories on GitHub"). `SecurityPlugin.plan()` SKIPs both on free+private
with "Requires a public repo or paid GitHub plan", a premise carried over
from Phase 4 (2026-02-22) that the 2026-03-02 research note already
contradicted.

**The dependency graph is switched by Dependabot alerts, nothing else.**
`security_and_analysis` has no `dependency_graph` property in the spec, and
sending one answers 200 and changes nothing (drift
`unknown-security-and-analysis-key-ignored`). `PUT vulnerability-alerts`
"enables dependency alerts and the dependency graph" and `DELETE` disables
both (spec; confirmed live). `GET dependency-graph/sbom` is a usable read:
200 on, 404 off.

**The Dependabot dependency is one-way.** `DELETE vulnerability-alerts` also
turns security updates off (and the graph). `DELETE automated-security-fixes`
leaves alerts and the graph on. Apply alerts before security updates.

**Secret scanning refuses explicitly; push protection does not.** On private
Free, `PATCH security_and_analysis.secret_scanning` answers `422 "Secret
scanning is not available for this repository."`. Push protection sent alone
answers 200 and is ignored, as it is everywhere
(`push-protection-needs-secret-scanning`). Pro does not unlock secret
scanning for user-owned repos: the docs give user-owned repos secret scanning
only on GHEC with Enterprise Managed Users.

**Private vulnerability reporting: `PUT` answers 404 on private repos**, like
the `GET` (`pvr-put-private-404`, `pvr-free-private-404`); the spec lists
422.

**`GET /user` returns `plan: null` with the default `gh` scopes** (`gist`,
`read:org`, `repo`), so `get_plan_name()` falls back to `"free"` and
`is_paid_plan` is False for every standard login
(`user-plan-null-without-user-scope`). It is also the authenticated user's
plan, not the repo owner's. The clean plan signal is the repo's own answer:
`403 "Upgrade to GitHub Pro or make this repository public…"` from
`GET rulesets` (`rulesets-free-private-403`).

**Off is not gated.** On private Free the Dependabot endpoints answer
"disabled" (404 "Vulnerability alerts are disabled.", 200
`{"enabled":false}`, 403 "Dependabot alerts are disabled for this
repository."), never the "Upgrade to GitHub Pro" 403. On that last 403, `gh`
adds a hint asking for the `admin:repo_hook` scope; it is wrong, and the
same token lists alerts once they are on.

**Org security configurations can silently override `fix` (docs only, not
probed).** Under an *enforced* configuration, REST writes "appear to succeed,
but no enablement statuses will change"
(`reference/security-at-scale/configuration-enforcement.md`). Under an
*unenforced* one, a repo admin's change moves the repo to status `removed`,
out of the org's managed set (`configuration-statuses.md`).
`GET /repos/{o}/{r}/code-security-configuration` reports the status, but
needs an org admin or security manager. See section 7 of
`docs/2026-11-01_security-settings-availability.md`.

## `push` on a Repository With No Commits (#134)

A `git init`-ed directory with nothing committed is a git checkout
(`is_git_checkout()` is true) with an unborn HEAD, no refs and no
`.git/index`. Every stage of `push` accepted it and each one reported
success:

- `git clone --bare` of it succeeds (git only warns "You appear to have
  cloned an empty repository").
- `git push origin 'refs/tags/*:refs/tags/*'` with no branch refspecs and no
  tags exits `0` having sent nothing, so `push_local` returned normally and
  `create` printed "Code pushed successfully".
- `git branch --set-upstream-to` then failed with
  `fatal: no commit on branch '<b>' yet`, shown raw in the wiring warning.
- `git ls-files` is empty, so the regex walk read no files (#84 restricts it
  to tracked files).

Only verification (`code/branches`: "at least one branch") caught it, after
the repo existed.

**The fix is a local check before anything else.**
`local_repo.branch_state()` runs `git for-each-ref --count=1 refs/heads`, and
`rev-parse --verify HEAD^{commit}` only when that is empty, to tell
`NO_COMMITS` from `NO_BRANCHES` (commits on a detached HEAD, no branch).
`create._validate_local_source()` calls it at the top of `run()`, ahead of
`build_context()`, so the refusal costs no API call and `--dry-run` shows it.
This moved the two existing path checks (not a directory, not a git repo)
ahead of authentication as well; their exit code (`2`) is unchanged. The
refusal uses `2` too, as another "this path cannot be pushed" input error.

**Fail open on `UNKNOWN`.** Many tests (and some real directories) have a
`.git` that is not a usable repository. `git -C` would then walk up and
answer for a parent repo, so the call sets `GIT_CEILING_DIRECTORIES` to the
path's parent; git fails and the result is `UNKNOWN`, which never refuses.
The ceiling limits upward discovery only: a worktree's or submodule's `.git`
file is still followed.

**Branches, not commits, are what `push` needs.** The refspecs are built from
`refs/heads`, so a repo with commits but no branch pushes nothing as well.
Tags alone do not satisfy `code/branches` either. Staged files do not count:
`NO_COMMITS` holds until the first commit.

**truffleHog reports a failed scan in its log, not its exit code.** On that
directory `trufflehog git file://...` logged
`{"level":"error","msg":"error running scan","error":"... failed to read
index file ..."}` on stderr and still exited with a status the scanner
accepts (`0` or `1`; the issue's output shows the run was taken as clean).
stderr was inherited, not captured, so nothing could look at it.
`_run_teeing_stderr()` now hands the child a pipe we own, a thread echoes it
to our stderr in raw chunks (progress output is not held back for a newline)
and keeps a copy, and `communicate()` reads stdout as before. Existing tests
that mock `subprocess.Popen` keep working: the mock writes nothing, the write
end closes, the reader sees EOF. `_trufflehog_scan_error()` treats two shapes
as "did not scan": an error/fatal line with msg `error running scan` at any
exit code, and exit `1` with no finding on stdout plus any error/fatal line.
Other error-level lines do not fail a scan that exited `0`: truffleHog logs
recoverable per-detector problems at that level, and failing on them would
turn `native`/`docker` runs into exit `1` for noise. The exact exit code of
the #134 run was not re-measured against a live truffleHog; the rule covers
both `0` and `1`.

**`copy` has the same shape (by reading the code; not reproduced live).**
Nothing in the `copy` path checked that the source had any ref: a mirror
clone and mirror push of an empty repository have nothing to send, and
`mirrored_ref_probes` would compare an empty source with an empty
destination. `create` now reads the source's heads with
`matching_refs()` (409 "Git Repository is empty" → `{}`) right after the
source metadata and refuses on an empty map. A failed read does not refuse:
it is not evidence of emptiness. This is one more API call per `copy`, and
none under `--dry-run`.

**The message matters as much as the check (#152).** "Use `new` instead" is a
dead end for someone who has a local directory: `new` creates the GitHub repo
and touches nothing local. The refusal prints the sequence that finishes the
job from where the user is (commit, then the same `push` again) ahead of the
`new` + `gh repo clone` alternative. `gh repo clone` is used because the
transport (ssh or https) has not been discovered at that point.

## Terminal Injection From Scanned Content (#125)

Anything printed from the scanned repo, from git or from the API goes through
`display.safe_display()` at the point of printing. Notes on why it is built
the way it is:

**Escape at the output boundary, not at collection.** `Finding` keeps the raw
path and match, so `--json` consumers get the exact file name back.
`json.dumps` is always called with the default `ensure_ascii=True`, which
escapes every control and non-ASCII character (C1, bidi, lone surrogates
included), so the JSON documents need no sanitizing. Passing
`ensure_ascii=False` anywhere would let raw C1 and bidi characters through;
do not.

**The one collection-time exception is `_ai_context_hint()`.** Its `match` is
multi-line prose with the path embedded. At print time a `\n` inside a
directory name can no longer be told apart from the hint's own line breaks,
so the path is escaped when the hint is built. `file_path` stays raw.

**`str.splitlines()` is not a safe way to split untrusted text.** It also
breaks on `\r`, `\x0b`, `\x0c`, `\x1c`-`\x1e`, `\x85`, U+2028 and U+2029. That
happened to neutralise those characters in the CRITICAL match lines, but only
by accident. `_print_findings()` splits on `"\n"` and escapes each piece.

**What is escaped.** Unicode categories `Cc` (C0, DEL, C1), `Cf` (bidi
overrides and isolates, zero-width characters, BOM, soft hyphen, the tag
block), `Cs` (lone surrogates), `Zl` and `Zp`. Tab is kept. U+009B matters
even in a UTF-8 terminal: some emulators treat the decoded C1 code point as
CSI. `Cf` includes ZWJ, so an emoji ZWJ sequence in a file name is shown with
a `\u200d` in it; that is the price of not maintaining a list of "harmless"
format characters. Private-use and unassigned code points are left alone:
they do nothing to a terminal, and `Cn` membership changes with the Python
version's Unicode tables.

**Lone surrogates were a crash, not just a display problem.** `os.walk()` and
`git ls-files -z` (decoded with `surrogateescape`) turn a file-name byte that
is not valid UTF-8 into U+DC80..U+DCFF, and `print()` raises
`UnicodeEncodeError` on those. `safe_display()` shows the original byte
(`\x9b`). macOS (APFS) refuses to create such names, so the tests build the
string directly instead of the file.

**`repr()` is already safe.** It escapes everything `str.isprintable()`
rejects, which is a superset of the set above. Plan `UPDATE`/`PRESERVE`
values and verification expected/actual values go through `repr()`; only the
`str()` paths (`ADD`, `DELETE`, keys, reasons) needed the helper.

**Backslashes are not doubled.** `a\x1b` in the output can mean an ESC or the
four literal characters. The output is for reading; doubling every backslash
would make Windows-style paths and regexes unreadable for no security gain.
The exact value is in `--json`.

**`error()` / `warn()` sanitize centrally with `multiline=True`.** Their
messages are plain text (colour is added around them) and carry git stderr
and API bodies, which are legitimately multi-line. So a remote can still add
*lines* to an error message, but not move the cursor, recolour or overwrite.
`info()` cannot do the same, because callers pass it text that already holds
the tool's colour codes; its callers escape the untrusted part themselves.

**truffleHog's stderr is escaped on its way through.** #134 replaced the
inherited `stderr=None` with a pipe that `_run_teeing_stderr()` echoes, so
the echo goes through `safe_display(multiline=True)` as well. truffleHog's
JSON log lines escape C0 controls (ESC, BEL) but write valid UTF-8 raw,
including C1 (U+009B, which some terminals treat as CSI) and bidi overrides.
Verified live on 2026-11-25 against truffleHog 3.99.8 (podman): with a
committed file whose name held ESC, BEL, U+009B and U+202E, a scan at
`--log-level=5` named the file three times on stderr, and before this change
`gh-safe-repo scan` relayed all three U+202E characters raw. The default log
level did not name the file, so the hole needs a verbose truffleHog (for
example a wrapper script on PATH in `native` mode) to show.
Because the child now sees a pipe rather than a TTY, the container runtime
prints plain pull lines instead of `\r` progress bars, so escaping garbles
nothing. An incremental UTF-8 decoder keeps a character split across two
4096-byte reads intact. An uncaught exception's traceback is still
unsanitized.

## truffleHog Status Line (#10)

**Why it was needed after #134.** With stderr on a pipe the container
runtime no longer draws progress. Checked live on 2026-11-25 (podman,
truffleHog 3.99.8) with `TRUFFLEHOG_IMAGE` pointing at an unreachable
registry: podman printed one plain `Trying to pull <image>...` line and then
nothing until it failed, with no `\r` and no escape sequences. A real pull
was not measured (the cached image was not deleted); podman's non-TTY pull
output also has `Copying blob ...` lines, which are relayed as they come.

**truffleHog has nothing to count.** At the default log level, a
filesystem scan logs exactly two lines on stderr: `"msg":"running source"`
at the start and `"msg":"finished scanning"` (with `chunks`/`bytes` totals)
at the end. There is no per-file or per-commit line to turn into a count, so
the status shows a phase and the elapsed time. Matching is on the compact
`"msg":"..."` form zap writes; if truffleHog changes it, the phase simply
stays at `starting`, and the elapsed time still advances.

**Drawing rules.** `_TruffleHogStatus` uses the same `\r\033[K` line as the
regex walk's `_draw_progress()` and the same gate (`self._progress`: TTY and
not `--json`). NO_COLOR does not apply, as it doesn't to the file progress
line: it carries no colour. A lock is shared by the 1-second ticker thread
and the relay thread. Each relayed chunk is written as clear, chunk, redraw,
and the redraw is skipped while the last chunk did not end in `\n`: drawing
there would append the status to half a relayed line. The status is started
before the reader thread, so the first chunk finds it running, and stopped
in a `finally` after the reader joins, so the line is cleared on every exit
path. Phase detection reads the decoded text, but only fixed strings are
drawn; the runtime name in the label goes through `safe_display()` because
`CONTAINER_RUNTIME` sets it.
## Hidden Characters and UTF-16 in the Regex Scan (#157)

**Test set.** 25 obfuscations of an AWS key pair, now `TestEvasionVariants`
in `tests/test_security_scanner.py`. The live runs used truffleHog's public
canary key, which truffleHog can verify; the committed tests use AWS's
documented example pair (`AKIAIOSFODNN7EXAMPLE`) instead, because GitHub push
protection rejected the branch while it held the canary (GH013 on 2026-11-26).
Never commit the canary, even as test data. Checked 2026-11-25 with
`gh-safe-repo scan --json` on a committed git repo. Regex alone (`trufflehog_mode = off`): 4
caught before, 15 after. Combined with truffleHog 3.x via podman
(`trufflehog_mode = docker`): 17. truffleHog adds the two base64 variants (it
decodes base64) and reports the plain pair; the regex half adds every
hidden-character variant, the lone key ID and UTF-16. Still missed by both:
Cyrillic `А`, fullwidth digits, string concatenation, a backslash line
continuation, a reversed literal, `\x` escapes, URL encoding, a key split
across lines. Those are #157's non-goals.

**Two normalized forms, not one.** "Remove the hidden characters" is wrong
for backspace: `X\b` shows the next character over `X`, so stripping only the
`\b` leaves an extra `X` (the AWS pattern still matches the first 20
characters, but a pattern anchored at the end would not). "Render like a
terminal" is wrong on its own too: a trailing `\r` plus spaces blanks the
rendered line and hides a key the bytes still carry. So each line is matched
raw, then stripped (every `Cc`/`Cf` character and ANSI escape removed), then
rendered (CR to column 0, backspace one left, later characters overwrite,
RLO spans reversed). Lines that pass `str.isprintable()` after their line
ending is removed skip both, so CRLF files and ordinary text cost nothing
extra. NBSP and U+2028 fail `isprintable()` but are not hidden, so they
return no forms.

**Severity design.** A hit that needs normalization is the same finding at
the same severity (a CRITICAL key stays CRITICAL), not an extra WARNING:
the key is in the file and is published either way, and a second "possible
obfuscated secret" finding on the same line would double-count. The
obfuscation is reported in the rule (`AWS Access Key ID (obfuscated with
U+200B ZERO WIDTH SPACE)`), named by Unicode name, so the report is ASCII,
the match stays `[redacted]` like every secret, and `--json` needs no new
key. `Finding.hidden` carries the names only to add `| cat -v` to the
`View with:` hint, because `sed` alone lets the terminal hide them again.
Emails and TODOs stay raw-only: they are not secrets and need no
evasion-proofing.

**No homoglyph or NFKC folding.** NFKC would fold fullwidth digits
trivially, but not Cyrillic `А`, and it would put every non-ASCII line on the
slow path. Listed as a non-goal; left out.

**False positives.** Stripping `Cf` can only create a match where the
remaining text spells a key, so ordinary text with ZWJ emoji sequences,
Persian ZWNJ, Arabic bidi marks, a BOM, soft hyphens, coloured logs or `\r`
progress lines produces nothing (`TestNormalizationFalsePositives`). The
quoted-value warning patterns (`password = '...'`) already matched through
a hidden character inside the value, since the value class accepts anything.

**Binary sniff.** The old rule (a NUL in the first 8 KB means binary) also
skipped UTF-16 and text with a stray NUL, which is how the NUL variant hid a
key (git and GitHub also call such a file binary, but it is still pushed).
A NUL block is now text when it is UTF-16 or UTF-32 with a BOM, BOM-less
UTF-16 (at least 90% NULs in the high bytes and at most 10% in the low ones),
or valid UTF-8 with no C0 control other than NUL, tab, LF, CR, VT, FF,
backspace and ESC, and at most `max(4, size/256)` NULs. A decoded UTF-16/32
sample must also be at most 5% control characters. The existing `.toe`
test (`\x00\x01...`) stays binary through the C0 rule. BOM-less UTF-16 of
mostly non-Latin text is not recognized; Windows writes a BOM.

**Line numbers.** The walk used to read with universal newlines, so a lone
`\r` started a new line and every later line number was off by one against
`sed -n` and git. It now splits on `\n` only. In the CR-overwrite variant
both engines now report line 1, so the `(file, line)` dedup against
truffleHog drops the regex duplicate instead of listing the key twice.

## Python 3.10 Floor in CI (#148)

**Code and tests must run on Python 3.10.** `requires-python = ">=3.10"`
and the CI matrix tests 3.10 and 3.14. Standard-library modules added in
3.11+ (e.g. `tomllib`) fail only the 3.10 job. A local run on a newer Python
will not catch it. In #149, `TestVersion.test_matches_pyproject` imported
`tomllib` and failed on 3.10 with `ModuleNotFoundError`. The fix reads the
version from `pyproject.toml` with a regex (`tests/test_cli.py`). Use a
3.10-safe path for anything newer (regex, a guarded import, or a
dependency), and run the suite on 3.10 before pushing:

```bash
uv run --isolated --python 3.10 pytest tests/
```

`--isolated` builds a throwaway environment, so the project's `.venv` keeps
its own interpreter. Checked 2026-11-25: 1520 passed on 3.10 and `.venv`
stayed on 3.13. Add the same `trufflehog_mode = off` config the regular run
uses (see `tests/README.md`).

## Immutable Releases (#127)

`plugins/releases.py`, config `[releases] immutable = true`. Sources, all from
the local mirror of GitHub's docs and REST description (not yet live-tested):

- REST description `api.github.com.2022-11-28.json`, operations
  `repos/check-immutable-releases`, `repos/enable-immutable-releases`,
  `repos/disable-immutable-releases` on `/repos/{owner}/{repo}/immutable-releases`.
  GET: 200 with schema `check-immutable-releases` (`enabled`,
  `enforced_by_owner`, both required) "if immutable releases are enabled",
  **404 "if immutable releases are not enabled"**. PUT/DELETE: 204, or 409.
- `docs/data/features/immutable-releases.yml`: `fpt: '*'`, `ghec: '*'`,
  `ghes: '>=3.20'`. No plan gating in the docs, so the plugin never SKIPs
  by plan.
- `content/code-security/concepts/supply-chain-security/immutable-releases.md`:
  only assets and the tag are locked; title, notes, pre-release and latest
  stay editable. A deleted immutable release's tag can be deleted, but the
  name can never be reused, even in a recreated repo. Recommended flow:
  draft → attach assets → publish.
- `gh release create` help (cli `pkg/cmd/release/create/create.go`): with
  asset files it creates a draft, uploads, then publishes (separate API
  requests, not atomic); protection starts at publish.

Design decisions:

- **"Off" is `200 {"enabled": false}` live, 404 in the spec.** Probed
  2026-11-26 (`docs/2026-11-26_immutable-releases-probe.md`): GitHub answered
  200 with `enabled: false` on a fresh repo and after `DELETE`, never 404,
  the same spec/live gap as `automated-security-fixes` (#94). The plugin
  reads `enabled` from any 2xx, so it was already right. 404 is still "off",
  never an error, even under `strict=True`, in case GitHub starts following
  the spec. A 2xx without a body counts as on.
- **`enforced_by_owner: true` → `SKIP`**, compliant, never written.
- **`immutable = false` never DELETEs.** Same as the `[security]` toggles:
  false means "leave it alone". The tool has no reason to weaken it.
- **Order in `create`:** applied right after the security settings, before
  the code push. Immutability only locks *published releases*; a freshly
  pushed tag has none, so pushing tags afterwards is unaffected (unlike
  branch protection, which needs the branch to exist).
- **No retry on the PUT.** It runs after README removal, Actions and
  security writes, by which time the eventual-consistency 404 seen on the
  first `PATCH /repos` has passed. The probe sent a PUT immediately after
  `POST /user/repos` and got 204 (one sample per visibility), so even an
  early PUT did not race. If it ever 404s, the apply warns and verification
  reports a mismatch (exit 3), and `fix` repairs it.
- **Tier: security, direction True.** GitHub's default is off, so an `audit`
  of an untouched repo shows `UPDATE releases/immutable False → True`, never
  `PRESERVE` or ⚠ weakened.

Checked live 2026-11-26 on a public and a Free private repo (evidence doc
above; `observed` blocks in `tools/api_drift/catalog.json`): GET is 200 for
on and off, PUT is 204 and idempotent, a Free private repo accepts the
setting (so no plan `SKIP`), and an immediate PUT after creation needs no
retry. Still untested: `enforced_by_owner: true` (needs an org, #9).

## Releases From CI (#129)

`.github/workflows/release.yml`, on `push: tags: ["v*.*.*"]`. Decisions:

- **Notes source: the tag's CHANGELOG section** (`tools/release_notes.py`),
  plus a compare link. Not the annotated tag message (the tags say only
  `vX.Y.Z`), and not a separate notes file (one more thing to forget). The
  v0.12.0+ hand-written Highlights/Upgrading files are dropped; if a release
  needs highlights, put them in its CHANGELOG section. The real sections for
  0.14.0 and 0.13.0 are covered by `tests/test_release_notes.py`.
- **Three jobs, write token only where needed.** `check` and `test` are
  `contents: read`; `publish` is `contents: write` and runs no repo code
  (it downloads the notes and dist artifact and calls `gh`). The workflow
  default is `permissions: {}`.
- **Tests reuse `ci.yml`** via `workflow_call` instead of a copy of the
  matrix. In a called workflow `github.workflow` is the caller's name, so
  `ci.yml`'s concurrency group becomes `Release-refs/tags/vX.Y.Z`; the
  release workflow's own group is `release-refs/tags/vX.Y.Z` so the two never
  wait on each other. Reusable workflows are exempt from SHA pinning (docs:
  "Reusable workflows can still be referenced by tag"), and a same-repo one is
  allowed under the `selected` Actions policy.
- **Actions used:** `actions/checkout`, `actions/setup-python` (same pins as
  CI), `actions/upload-artifact@cf430e0…` (v7.0.2) and
  `actions/download-artifact@9000827…` (v8.0.2), all GitHub-owned, so the
  repo's GitHub-owned-only policy admits them. SHAs looked up with
  `gh api repos/actions/<name>/commits/<tag>` on 2026-11-25.
- **Annotated-tag check reads the API** (`git/ref/tags/X` → `object.type`),
  because `actions/checkout` can rewrite a tag ref locally. The signature
  check (`git/tags/{sha}` → `verification.verified`) only warns: an SSH
  signing key GitHub does not know about is not a reason to block a release.
- **Existing release:** published → notice, exit 0 (re-runs are no-ops, and a
  published release may already be immutable); draft → fail and ask a human.
  `gh release view` failing for any reason other than "release not found"
  fails the run rather than risking a duplicate.
- **Immutable releases (#127):** passing asset files to `gh release create`
  makes gh create a draft, upload, then publish (cli
  `pkg/cmd/release/create/create.go`), and delete the draft if an upload or
  the publish fails. Protection starts at publish, so nothing locked is ever
  incomplete. gh also refuses when a *published* release with the tag exists
  but not when a draft does, hence the draft check above.
- **Latest:** `--latest=false` when a higher `vX.Y.Z` tag exists, so a
  backfill does not take over Latest (the API default marks every new
  non-draft release Latest).

Unverified until a real tag push: the reusable-workflow call under this
repo's Actions policy, the artifact round trip, `gh release view`'s
"release not found" text on the runner's gh, and `isImmutable` in its JSON.

## `copy` Pushed to the Source Owner (#168)

- `GitHubClient.copy_repo` took one `owner` for both sides ("both repos must
  belong to owner"), so `copy some-org/src me/dst` created `me/dst` and then
  ran `git push --mirror` at `some-org/dst`, plus the default-branch `PATCH`.
  A mirror push to a writable repo deletes every ref the source lacks, so
  this could destroy an unrelated repo.
- Tests missed it because every `copy` test used one owner, or mocked
  `copy_repo` away entirely. The fix makes the owners keyword-only
  (`source_owner=`, `dest_owner=`), so a positional call raises `TypeError`
  rather than silently mixing them up. The regression tests in
  `tests/test_cli.py` run the real `copy_repo` through `main()`, mocking only
  git subprocesses and the API.
- `dest_owner` is `ctx.owner` (the canonical login), as for `push` since
  4c657fc: git push URLs are case-sensitive. The source owner keeps the
  user's spelling. A clone reaches the repo either way, and the pre-flight scan,
  source reads and `mirrored_ref_probes` all read the source owner.

## `copy` Failed on Repos With Pull Requests (#171)

- `git clone --mirror` fetches `+refs/*:refs/*`, which on GitHub includes
  the read-only `refs/pull/<n>/head` and `refs/pull/<n>/merge`. `git push
  --mirror` then sends them, GitHub answers `! [remote rejected] ... (deny
  updating a hidden ref)` for each, and git exits non-zero after the
  branches and tags have already landed. Seen live with
  `copy octocat/Hello-World <me>/<dest> --yes`. Any source with a PR hits it.
- Checked locally (git 2.56, `file://` source holding `refs/pull/1/head` and
  `refs/pull/1/merge` on a commit reachable from nothing else): `git clone
  --bare` copies `refs/heads/*` and `refs/tags/*` (annotated tags keep their
  tag object, lightweight tags their commit) and neither the pull refs nor
  the PR-only commit's objects. A mirror clone of the same source has both
  pull refs. So the bare clone also avoids downloading every PR's objects.
- `copy_repo` now runs `clone --bare` and one `git push origin
  'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'` (`COPY_REFSPECS`):
  one connection, one security-key touch, and no `--mirror` prune
  semantics, so #168's delete-extra-refs risk is gone too. The destination
  is new, so there is nothing to prune.
- `mirrored_ref_probes` already compared `heads` and `tags` only (via
  `matching_refs`), so verification needed no change.
- A failed single push can still be partial, so `_abort_on_push_failure` no
  longer says the repo "is empty". The printed recovery (and the
  `code/*` remediation) is the same bare clone and refspec push; the old
  `--mirror` recovery failed exactly like the original run.
- `tests/test_github_client.py` runs the real `copy_repo` against local repos
  with pull refs, patching only `GitTransport.remote_url` to return paths.
