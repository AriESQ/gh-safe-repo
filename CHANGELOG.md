# Changelog

All notable changes to gh-safe-repo are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
This file was introduced with 0.2.0; the 0.2.0 entry backfills notable changes
since the initial release.

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
