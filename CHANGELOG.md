# Changelog

All notable changes to gh-safe-repo are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
This file was introduced with 0.2.0; the 0.2.0 entry backfills notable changes
since the initial release.

## [Unreleased]

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

### Changed
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
