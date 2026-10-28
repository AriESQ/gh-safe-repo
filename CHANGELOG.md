# Changelog

All notable changes to gh-safe-repo are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
This file was introduced with 0.2.0; the 0.2.0 entry backfills notable changes
since the initial release.

## [Unreleased]

### Added
- Developer tooling (no change to the CLI): `tools/api_drift/` tracks where
  GitHub's published REST description and GitHub's real behaviour disagree,
  for the endpoints gh-safe-repo calls (#27). `catalog.json` lists the
  tracked endpoints, a snapshot of what the spec says about each, what was
  observed live, and 18 known quirks with evidence and the workaround in
  the code. `api_drift.py check` diffs the catalog against a spec file and
  reports spec changes, quirks whose spec condition no longer holds (the
  workaround may be removable), observed behaviour the spec does not
  document, and endpoints the code calls that the catalog does not track;
  `snapshot` accepts the spec's current state; `probe OWNER/REPO` is a
  live, GET-only comparison. A weekly `API drift` workflow runs `check`
  against the latest `github/rest-api-description` and opens or updates
  one issue on drift.

## [0.11.0] - 2026-10-27

### Added
- The plan warns before an Actions-policy change breaks workflows (#111).
  When `fix`, `audit`, `push` or `copy` would change `allowed_actions`,
  `patterns_allowed`, `verified_allowed`, `github_owned_allowed` or
  `sha_pinning_required`, a `Warnings` block after the plan table (before
  the confirmation prompt) lists each whitelist entry the new
  `patterns_allowed` removes (`⚠ removes whitelisted action <pattern>`) and
  every `uses:` reference in the default branch's workflows that runs today
  and would be blocked (`⚠ would block N actions in M workflows`), with the
  file, line, reason, and for an unpinned reference the SHA its tag resolves
  to now. Actions allowed only through verified-creator status (no API can
  check it) are reported as unknown, not blocked. `docker://` references
  are reported as blocked under `selected`/`local_only`, where GitHub
  refuses them. Matching follows GitHub's: a pattern is compared with the
  full `owner/repo@ref`, so `owner/action` without `@*` matches nothing
  (checked against live GitHub; see
  `docs/2026-10-26_actions-whitelist-probe.md`). Workflows are read
  through the contents API (`fix`, `audit`, `copy`) or with `git` from the
  local default branch (`push`, also under
  `--dry-run`); `copy --dry-run` makes no API calls and reports
  `actions_scan_skipped` instead.
- `--json` plan documents carry a top-level `warnings` list (always
  present, often empty) with the same information as structured records:
  `whitelist_pattern_removed`, `actions_blocked`, `actions_unknown`,
  `actions_scan_skipped`. Under `--json --yes` the warnings are also printed
  on stderr.

Warnings never change what is applied or the exit code: the whitelist is
still replaced rather than merged, and `--yes` runs apply with the warning.

### Fixed
- Every GitHub API read now checks the response body, not just the status
  code (#94):
  - **Rulesets:** a branch or tag ruleset whose enforcement is `disabled`
    (or `evaluate`) no longer counts as protection. `fix` re-enables it.
  - **Tag ruleset updates:** `fix` on a repo that already has the tag
    ruleset now updates it with `PUT`. It used to always `POST`, which
    failed with `422` "Name must be unique". Branch-ruleset updates use
    `PUT` instead of `PATCH`, which GitHub answers with `404`, so `fix`
    could not update an existing branch ruleset either.
  - **Actions disabled:** `fix` no longer turns Actions back on for a repo
    where they are disabled. `allowed_actions` and `sha_pinning_required`
    are `SKIP` there. Null fields in the Actions permission responses now
    read as GitHub's defaults.
  - **Dependabot security updates:** `enabled: true, paused: true` counts
    as off, so `fix` proposes turning updates back on.
  - **Secret scanning on public repos:** read from the API instead of
    assumed on. If it is turned off, `fix` turns it back on.

## [0.10.1] - 2026-10-22

### Fixed
- Private vulnerability reporting is now `SKIP` ("Only available for public
  repositories") on every private repo. GitHub offers it for public repos
  only, but on a paid plan the plugin planned a `PUT` for private repos that
  GitHub would refuse. The plan-limits tables in the README and the agent
  skill said paid + private was supported; they now say it is not (#113).
- `push` from a linked worktree (`git worktree add`) or a submodule checkout
  no longer commits your uncommitted changes into the real repository
  (#103). Their `.git` is a file, not a directory, so the source was
  treated as a plain directory: it was copied along with that `.git` file,
  and the `git add`/`commit` run on the copy landed on your real branch. A
  clean worktree pushed nothing at all. Both now push like a main checkout
  (every local branch and tag; for a worktree that means the main
  repository's branches), and the plain-directory path never copies a
  `.git` entry.
- The pre-flight scan of a worktree or submodule now covers tracked files
  only, as in a main checkout, so gitignored files such as `.env` no longer
  produce findings. Container-mode truffleHog mounts the repository the
  `.git` file points at, so its git-history scan can read it.

## [0.10.0] - 2026-10-20

### Added
- `audit` and `fix` now resolve each setting with a three-way comparison
  (GitHub's default, the repo's current value, the desired value). A setting
  a person changed away from GitHub's default is reported as `PRESERVE` and
  left alone: always for preference-tier settings (`[repo]`, Actions
  `enabled`, `enforce_admins`, `require_conversation_resolution`,
  `use_rulesets`), and for security-tier settings when the current value is
  stricter than the desired one. A security setting that is weaker than
  GitHub's default is still an `UPDATE`, marked `⚠ weakened from default`.
  Declaring a section in the config file enforces every setting in it
  regardless. See "What `fix` leaves alone" in the README.
- `--all` on `audit` and `fix`: turn the merge policy off and treat every
  differing setting as a change, exactly as before this release.
- Plan table: the `PRESERVE` row type with its reason (`changed from
  default`, `stricter than N`), the `⚠ weakened from default` marker, and a
  separate `preserved` count on the summary line.
- `--json`: every change carries `tier`, `github_default` and `weakened`;
  `summary` gains `preserve` when present.
- `config_manager.SETTING_TIERS` / `SAFE_DIRECTIONS` (tier and "stricter"
  direction per setting) and `ConfigManager.declared_sections`.

### Changed
- `audit` exits `3` only for `ADD`/`UPDATE`/`DELETE` rows. A repo whose only
  differences are `PRESERVE` rows prints the table and exits `0`.
- `fix` verification re-applies the merge policy to the re-read, so a
  preserved setting is never reported as a mismatch. Ruleset writes (branch
  and tag protection) carry the preserved value for a kept key rather than
  resetting it to the desired one.
- `new`, `push` and `copy` are unaffected: no merge logic on a repo that
  does not exist yet.
- A sub-setting whose parent is preserved at a value where it has no effect
  is `SKIP`, not `UPDATE`. Found live: with `allowed_actions` kept at
  `local_only`, `verified_allowed` was reported as drift and `audit` exited
  `3` for a setting GitHub ignores. `config_manager.DEPENDENT_SETTINGS`
  records the dependency (the three selected-actions keys on
  `allowed_actions == "selected"`) and `apply_merge_policy` applies it.

## [0.9.0] - 2026-10-19

**Breaking.** The verbs were rewritten. `create`, `create --local`,
`create --from` and bare `--config` no longer exist; see Removed below.
There are no aliases, so scripts and the agent skill must be updated.

### Added
- `new <owner/repo>`: create an empty repo with safe defaults (what plain
  `create` did).
- `push <path> <owner/repo>`: push a local git repository into a new repo
  (what `create --local` did). Source first, destination second.
- `copy <src-owner/repo> <owner/repo>`: copy an existing repository into a
  new one (what `create --from` did). The help says plainly that the result is
  not a GitHub fork.
- `audit <owner/repo>`: read-only drift report for an existing repo. Prints
  the same table as `fix --dry-run`, or `All settings compliant.` when there
  is nothing to change, never prompts, and exits `3` when `fix` would change
  something, `0` when compliant, so it works as a CI gate. `--json` prints
  the plan document.
- `--no-config`: ignore config files and use built-in defaults. Global, like
  `--config` and `--debug`.
- `scan` with no path scans the current directory.
- `docs/2026-10-16_cli-interface-review.md`: the review of the CLI against
  the user personas that motivated these changes.

### Changed
- The options are now uniform by verb class. `new`, `push` and
  `copy` take `--public`, `--dry-run`, `--yes`, `--json`; `fix` takes
  `--dry-run`, `--yes`, `--json`, `--migrate-branch-protection`; `audit` and
  `scan` take `--json`. The top-level `--help` lists the verbs in those
  groups.
- Error messages for a bad `push` path say `Source path '...'` instead of
  `--local: '...'`.

### Removed
- The `create` verb and its `--local PATH` / `--from OWNER/REPO` flags. Use
  `new`, `push` and `copy`. Passing the old verb or flags is a usage error
  (exit `2`); there is no alias or deprecation period.
- Bare `--config` (no value) as the way to ignore config files. `--config`
  now always takes a path; use `--no-config` instead. This removes the
  `gh-safe-repo --config scan ./src` trap where `scan` was read as the path.

## [0.8.0] - 2026-10-15

### Added
- `fix` reads every changed setting back from GitHub after applying it and
  prints a `Verified` section, one `✓`/`✗`/`?` row per setting (#77).
  Settings that read wrong or cannot be read are re-read after 1s, 2s and
  4s before they are reported. A new exit code `3` means the changes were
  applied but at least one did not stick; re-running `fix --yes` once is the
  remedy. A setting that still cannot be read back is a warning, not a
  failure.
- `fix --json` (without `--dry-run`) prints its document once, at the end,
  with a top-level `verification` block: `ok`, one entry per check
  (`status` is `ok`, `mismatch` or `unknown`, with `expected`, `actual` and
  `remediation`), and an `ok`/`mismatch`/`unknown` `summary`. `--dry-run
  --json` is unchanged.
- `create` verifies its settings the same way (#77): after applying, it
  re-reads every setting it changed and prints a `Verified` section before
  the banner. It also checks what only a new repo has: the repo exists and
  has the visibility you asked for, the auto-generated README was removed,
  `--from` copied the description and topics, and branch and tag protection
  cover the default branch and tag patterns. A setting that did not stick
  exits `3`; a missing repo or wrong visibility exits `1` (a repo meant to
  be private that came out public needs attention now, and the remediation
  is the exact `gh repo edit --visibility` command). The banner turns
  yellow `Done with problems`, or red `Failed` for a fatal check.
- `scan` and the `create --local`/`--from` pre-flight print the engine that
  actually ran (`Scan engine: truffleHog v3.93.4`, or `regex (fell back from
  truffleHog: ...)`) after the scan (#77). The header shown before the scan is
  only what discovery expected; truffleHog can still fail mid-run.
- `scan --json` gains an `engine` key: `{"configured": <trufflehog_mode>,
  "used": "trufflehog-native" | "trufflehog-container" | "regex", "fell_back":
  bool}`. Existing keys are unchanged.
- `create --local` and `--from` verify the code they pushed (#77). Every
  branch and tag `--local` pushed must be on GitHub at the pushed SHA
  (`--from`: every branch and tag of the source, at the source's SHA), read
  through the REST API (`git/matching-refs`, paginated) rather than
  `git ls-remote`, so it costs no hardware-key touch. A missing ref or a
  different SHA exits `1` with the push or mirror command to re-run. The
  default branch is checked too (exit `3`, with the `gh repo edit
  --default-branch` command). New checks: `code/branches`, `code/tags`,
  `repo/default_branch`.
- `create --local` reads your repo's git config afterwards to confirm the
  wiring: `remote.origin.url` is the new repo and the current branch tracks
  `origin/<branch>` (`local/origin`, `local/tracking`). A mismatch is a
  warning that never changes the exit code, with the exact `git remote
  add`/`set-url` or `git push -u origin <b>:<b>` command. Verification
  checks gain an `advisory` field in `--json` (`true` for these).

### Changed
- The `create --local` banner's "Local repo wired up" / "Set your tracking
  branch" lines are built from the `local/*` checks (what git config
  actually says) instead of from what the push step reported (#77). Only
  advisory checks failing leaves the banner green.
- `create --from` sets the source's default branch on the new repo after the
  mirror push, and applies branch protection to it (#77). A mirror push
  carries no HEAD, so GitHub used to pick its own default.
- `create --json` (without `--dry-run`) prints its document once, at the
  end, with the `verification` block, instead of printing the plan before
  the prompt (#77). Without `--yes` the plan table goes to stderr before the
  prompt; declining prints the document without `verification`. A run that
  exits `1` before or during the apply (repo exists, blocked scan, prompt
  at end of input, credential pre-flight, failed push) prints no document.
  `--dry-run --json` is unchanged.
- `fix` builds its closing banner from the verification result: green
  `Done` only when every setting checks out, otherwise yellow `Done with
  problems` (#77).
- `trufflehog_mode = native` and `docker` are now strict (#77). If truffleHog
  cannot run (not found, wrong version, or it fails mid-scan), `scan` prints
  its findings and exits `1`, and `create --local`/`--from` exits `1` before
  the repo is created, naming the mode and the reason. Previously both
  printed a warning and fell back to regex. `auto` still falls back, and an
  unrecognised `trufflehog_mode` value is treated as strict instead of as
  `auto`.
- `create --local`/`--from` verify git credentials after the confirmation
  prompt, right before the repo is created, instead of before the scan (#79).
  A hardware security key's touch only proves the key answered at that moment,
  so the check now runs as close to the real push as possible. A credential
  failure still exits `1` before anything is created.
- `create --local` pushes every branch and tag in one `git push` instead of
  three, so a hardware security key needs one touch, not three, and a missed
  touch can no longer leave a half-pushed repo.

### Fixed
- Credential failures over SSH now name the likely cause (#100). A
  pre-flight check that times out says the security key was probably
  waiting for a touch, instead of blaming the network, and the SSH timeout
  is now 45 seconds (it was 15; HTTPS stays at 15). A `Permission denied
  (publickey)` from the pre-flight check, the `--from` scan clone or the
  push now adds which directory the SSH settings were read from and what
  `core.sshCommand` was found there, since `--from` uses the current
  directory and a per-directory key (`includeIf`) silently doesn't apply
  elsewhere. `--debug` prints the discovered `core.sshCommand`.
- `create` and `fix` run without `--yes` and with stdin closed (`< /dev/null`,
  a CI job, an agent subprocess) exited `0` at the confirmation prompt with
  nothing done, indistinguishable from success (#93). They now print
  `--yes is required for unattended runs` on stderr and exit `1`. Typing `n`
  (or an empty line, or Ctrl-C) at the prompt still exits `0`, and an answer
  piped on stdin (`echo y | gh-safe-repo fix ...`) is still honoured.
- The plan-limits tables in the README and the agent skill listed private
  vulnerability reporting as available on free plans with private repos. GitHub
  returns 404 for it there, so `create` and `fix` skip it, as the plan already
  showed; the tables now say so.
- `create --json --local`/`--from` printed the pre-flight scan report on
  stdout ahead of the JSON plan. It now goes to stderr.
- A missed security-key touch (`signing failed for ...-SK`) during the push,
  mirror, or source clone is retried once, after printing `Your SSH security
  key did not confirm. Touch it when it blinks to retry...` on stderr (#79).
  Other git failures are not retried.
- `fix` no longer reports `Repository updated successfully!` when the
  changes failed to apply; it used to print that even when every apply
  raised (#77).
- Under `--json`, `create` and `fix` keep stdout to the JSON document: the
  closing banner, the confirmation prompt and the pre-flight scan report now
  go to stderr (#77).
- `fix` no longer reads private vulnerability reporting as enabled when
  GitHub answers `200 {"enabled": false}`, so a repo with it switched off is
  now planned for an update.

## [0.7.0] - 2026-10-14

### Added
- `scan --json` emits findings as JSON on stdout (progress and warnings move to
  stderr), matching the `--json` convention already used by `create` and `fix`.
  All three severity counts are always present in `summary`.
- An agent skill at `skills/gh-safe-repo/SKILL.md` documenting the
  non-interactive contract, exit codes, plan-limit skips, and the common
  failure modes. It is not under `.claude/skills/`, so it never auto-loads
  while you work in this checkout; point a session at the file directly, or
  symlink it into `~/.claude/skills/` to invoke it from anywhere. See
  `skills/README.md`.

### Fixed
- `create --local` pushes every local branch, not just the checked-out one
  (#86). It used to push only the current branch, which then became the
  repo's default. The default branch is now `init.defaultBranch`, `main`, or
  `master` when present (else the current branch), pushed first and set via
  the API; the plan shows the branch count and default, and branch
  protection targets it.
- The pre-flight regex scan no longer walks ignored files (#84). In a git repo
  it scans only `git ls-files`, matching what is pushed and what truffleHog
  scans; a 7.4 GB working tree with 11 MB tracked used to stall for minutes.
  Files with a NUL byte in their first 8 KB are skipped as binary regardless
  of extension. Ctrl-C now prints `Interrupted.` and exits `130` instead of a
  traceback, and a TTY-only progress line shows the file being scanned.
- A failed code push on `create --local`/`--from` is now fatal (#79). It used
  to print a warning, then the green success banner, and exit `0`, leaving an
  empty repo. It now exits `1`, skips branch/tag protection (there is no
  branch yet), and prints the exact commands to finish: add `origin`, push
  with an explicit `<branch>:<branch>` refspec, then `gh-safe-repo fix`. A
  missed hardware security-key touch (`signing failed for ED25519-SK`/
  `ECDSA-SK`), which git reports as a permissions error, is now named as the
  likely cause.
- `create --local` now actually sets upstream tracking on the original repo
  (#70). The push runs from a temp clone, so the old `git branch
  --set-upstream-to` call always failed (no `origin/<branch>` ref existed) and
  the error was swallowed. The pushed SHAs are now written as
  `refs/remotes/origin/*` so tracking works with no fetch. If wiring can't be
  completed (e.g. an existing `origin` pointing elsewhere, left untouched), a
  warning says why, and the success banner prints `git push -u origin
  <branch>:<branch>` with the real branch name instead of an unusable
  `--set-upstream-to=origin/<branch>` placeholder.
- `--yes` no longer stops at the pre-flight scan prompt. Warnings are accepted
  automatically (matching the prompt's `[Y/n]` default) and critical findings
  stop the run. Previously an unattended `create --local --yes` on a repo with
  any finding printed "Aborted by user." and **exited 0** without creating
  anything, which callers could not distinguish from success. A blocked run now
  exits `1`; a user declining at a real prompt still exits `0`.

### Changed
- ANSI colour is emitted only when the output stream is a terminal and
  `NO_COLOR` is unset, so piped and captured output is plain text.

## [0.6.0] - 2026-08-20

### Changed
- `--config` and `--debug` are now global options: they are accepted before the
  command (`gh-safe-repo --debug fix owner/repo`) as well as after it, matching
  the convention used by `git`, `docker`, and `kubectl`. Previously only the
  post-command form parsed; the pre-command form failed with "unrecognized
  arguments". Existing invocations are unaffected. If both positions are used,
  the later one wins.
- Top-level `--help` lists `--config` and `--debug` under the `options:` heading
  instead of as a trailing line of the description, and colors the `usage:` and
  `examples:` headings to match argparse's own section headings.

## [0.5.0] - 2026-07-05

### Changed
- Plain `create owner/repo` no longer leaves an auto-generated `README.md` in the
  new repo by default. The repo still gets a default branch (still POSTs
  `auto_init=true`, so branch protection works at create time and GitHub assigns
  the account's preferred default-branch name), but the generated `README.md` is
  removed via the Contents API afterward. The previously-ignored `auto_init`
  config key now controls this: set `auto_init = true` to keep the initialized
  README. `--local`/`--from` are unaffected (they always push your own history and
  never create a README). (#54)

## [0.4.0] - 2026-06-13

### Changed
- `create --local PATH` now requires `PATH` to be a git repository. Plain
  directories (non-git) are rejected with a clear error message suggesting
  `git init`. This ensures SSH transport discovery (via `core.sshCommand` /
  `includeIf`) works correctly for multi-account setups. (#59)

## [0.3.0] - 2026-06-12

### Changed
- Branch protection now uses the **Rulesets API by default** (`use_rulesets =
  true`). A single `gh-safe-repo defaults` ruleset covers all configured branches
  and expresses admin bypass via a bypass actor. The classic per-branch path is
  kept for one release cycle behind `use_rulesets = false`. (#43)

### Fixed
- Rulesets read path: `fix`/`scan` now read existing branch rulesets via
  `GET /repos/{owner}/{repo}/rulesets` instead of always reading the classic
  endpoint, so a ruleset-configured repo no longer shows a false "all settings
  missing" diff. (#43)
- Idempotent upsert: `apply()` now `PATCH`es an existing `gh-safe-repo defaults`
  ruleset instead of always `POST`ing, so re-running no longer creates duplicate
  rulesets. (#43)

### Added
- `fix --migrate-branch-protection`: required to convert a repo that still has
  classic branch protection over to a ruleset. Without it, `fix` reports the
  classic protection and skips rather than silently dropping classic-only rules
  (`required_status_checks`, push `restrictions`). With it, the ruleset is created
  and the classic protection is deleted. (#43)

## [0.2.0] - 2026-06-12

### Added
- `[git_transport]` config section with `mode = auto | user_creds | token`,
  restoring a push path for CI/headless environments that have only a
  `GITHUB_TOKEN` (no SSH key, no credential helper). `auto` is the default and
  never switches SSH users to token injection; token-bearing URLs are redacted
  from all output and never written to `.git/config`. (#57)
- Pre-flight credential check (`git ls-remote`) for `--local`/`--from` now
  probes HTTPS as well as SSH, using exactly the same transport the real push
  will use, so credential problems surface before the repo is created. (#48)
- `fix` can target repos the authenticated user does not own (org repos,
  collaborator access) — requires admin permission on the target; `--debug`
  prints the resolved repo identity. (#41)
- `create` success output includes clone/set-upstream instructions for the new
  repo. (#51, #53)

### Changed
- `GITHUB_TOKEN` now takes priority over `gh auth token` for API calls,
  so a specific account can be targeted without switching the active `gh`
  session. (#50)
- `has_wiki`, `has_issues`, and `has_projects` are opt-in: omitted from config
  means the tool never touches them. (#56)
- Git push and clone use the user's own git credentials (SSH key or credential
  helper) instead of the OAuth token, avoiding the `workflow`-scope failure
  when pushing `.github/workflows/*`. (#46)
- Config disabling all three merge strategies is rejected up front instead of
  failing with a GitHub 422. (#36)
- Credential/access errors from `fix` distinguish a missing repo from a
  wrong-account or permissions mismatch. (#55)

### Fixed
- Per-directory `core.sshCommand` (set via `includeIf`) is propagated into the
  temp-dir push subprocess — fixes pushes for multi-account SSH/YubiKey
  setups. (#48)
- Repo creation retries the settings PATCH after a 404 caused by GitHub's
  eventual consistency immediately after repo creation. (#35)
- Owner casing is canonicalized for git push URLs and API calls; redirected
  pushes no longer reject workflow files. (#44)
- Pre-flight scan skips untracked AI context files instead of flagging them.

## [0.1.0] - 2026-02-27

Initial release (Phases 1–4): `create`, `fix`, and `scan` subcommands; plugin
architecture for repository settings, Actions policy, branch protection, tag
protection (Rulesets), and security features; plan/apply flow with `--dry-run`;
pre-flight secret scanning via truffleHog (native or podman/docker) with regex
fallback; GitHub plan/visibility detection with graceful skips.
