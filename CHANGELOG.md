# Changelog

All notable changes to gh-safe-repo are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
This file was introduced with 0.2.0; the 0.2.0 entry backfills notable changes
since the initial release.

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
