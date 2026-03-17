# gh-safe-repo

Create GitHub repositories with safe defaults applied automatically. Replaces the five-minute post-creation settings checklist with a single command.

```
gh-safe-repo my-project
```

Branch protection, Dependabot, restricted Actions permissions, disabled wiki and projects, squash-only merges, and automatic branch cleanup — all configured before you write your first line of code.

---

## Table of Contents

- [Why](#why)
- [What It Changes](#what-it-changes)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [CLI Reference](#cli-reference)
- [Dry Run / Plan Output](#dry-run--plan-output)
- [Audit Mode](#audit-mode)
- [Public Repos from Private (`--from`)](#public-repos-from-private---from)
- [Creating a Repo from a Local Directory (`--local`)](#creating-a-repo-from-a-local-directory---local)
- [Pre-flight Security Scanner](#pre-flight-security-scanner)
  - [Standalone scan](#standalone-scan)
  - [Suppressing false positives](#suppressing-false-positives)
- [Configuration](#configuration)
- [GitHub Plan Limitations](#github-plan-limitations)
- [How It Works](#how-it-works)
- [Development](#development)

---

## Why

GitHub's default repository settings are optimised for discoverability and flexibility, not security. Every new repo ships with:

- Wiki and Projects enabled (attack surface, even if unused)
- Merge commits allowed (messy history, but not the main concern)
- No branch protection (anyone with write access can push directly to `main`)
- No Dependabot alerts
- GitHub Actions with write permissions to the repository
- Actions allowed to approve pull requests

Fixing all of this manually takes minutes per repo and is easy to forget. `gh-safe-repo` applies an opinionated but practical set of defaults in one shot, with a plan preview so you know exactly what will change before anything does.

---

## What It Changes

### Repository settings

| Setting | GitHub default | Safe default | Notes |
|---|---|---|---|
| Visibility | Public | **Private** | Pass `--public` to override |
| Wiki | Enabled | **Disabled** | |
| Projects | Enabled | **Disabled** | |
| Issues | Enabled | Enabled | |
| Delete branch on merge | Off | **On** | Auto-cleanup |
| Allow merge commits | On | **Off** | Squash and rebase only |
| Allow squash merge | On | On | |
| Allow rebase merge | On | On | |

### GitHub Actions

| Setting | GitHub default | Safe default |
|---|---|---|
| Allowed actions | All | **Selected** (GitHub + verified creators) |
| Default workflow permissions | Read/write | **Read-only** |
| Actions can approve PRs | Yes | **No** |
| Require SHA pinning | No | **Yes** (workflows must pin actions to a commit SHA, not a mutable tag) |

### Branch protection (public repos, or any repo on a paid plan)

| Rule | Value |
|---|---|
| Require pull request before merge | Yes |
| Required approving reviews | 1 |
| Dismiss stale reviews on push | Yes |
| Require conversation resolution | Yes |
| Allow force pushes | No |
| Allow branch deletion | No |
| Enforce on admins | No (allows owner tooling to push) |

### Security

| Feature | Behaviour |
|---|---|
| Dependabot alerts | Enabled (public repos / paid plans) |
| Dependabot security updates | Enabled (auto-opens PRs for vulnerable deps) |
| Secret scanning | Automatic on public repos; enabled on private paid plans |
| Push protection | Enabled (blocks commits containing supported secrets) |
| Private vulnerability reporting | Enabled (lets security researchers report privately) |
| Dependency graph | Automatic on public repos; no REST API for private (UI only) |

---

## Requirements

- Python 3.8+
- [`gh` CLI](https://cli.github.com/) installed and authenticated (`gh auth login`), **or** `GITHUB_TOKEN` set in your environment
- [`uv`](https://docs.astral.sh/uv/) for installation from source (recommended)
- `truffleHog` v3 (optional — used by the pre-flight scanner; auto-detected from PATH, or run via podman/docker; falls back to regex if neither is available)

---

## Installation

### From source with uv (recommended)

```bash
git clone https://github.com/your-username/gh-safe-repo
cd gh-safe-repo
uv tool install .
```

This installs `gh-safe-repo` into uv's tool environment and adds it to your `PATH`.

### Run directly without installing

```bash
git clone https://github.com/your-username/gh-safe-repo
cd gh-safe-repo
uv sync           # creates .venv
./gh-safe-repo my-project
```

### Verify

```bash
gh-safe-repo --help
```

---

## Quick Start

```bash
# Create a private repo with all safe defaults
gh-safe-repo my-project

# Preview what would happen — no changes made
gh-safe-repo my-project --dry-run

# Create a public repo (branch protection + security scanning applied)
gh-safe-repo my-public-project --public

# Mirror a private repo to a new public repo (with pre-flight scan)
gh-safe-repo my-public-project --from my-private-project --public

# Create a repo from a local directory (with pre-flight scan)
gh-safe-repo my-project --local ~/projects/myapp

# Same, but make it public (branch protection applied before push)
gh-safe-repo my-project --local ~/projects/myapp --public

# Audit an existing repo and apply any missing safe defaults
gh-safe-repo my-existing-repo --audit

# Audit without making changes
gh-safe-repo my-existing-repo --audit --dry-run

# Scan a local repo for secrets before pushing anywhere
gh-safe-repo --scan .
gh-safe-repo --scan ~/projects/myapp
```

---

## CLI Reference

```
gh-safe-repo REPO_NAME [OPTIONS]
gh-safe-repo --scan PATH [OPTIONS]
```

### Arguments

| Argument | Description |
|---|---|
| `REPO_NAME` | Name of the repository to create or audit (not required with `--scan`) |

### Options

| Option | Description |
|---|---|
| `--scan PATH` | Scan a local directory for secrets and exit. No GitHub interaction. Exit code 0 = clean, 1 = critical findings. |
| `--local PATH` | Push code from a local directory into the new repo. Runs pre-flight scan first. Mutually exclusive with `--from` and `--audit`. |
| `--dry-run` | Print the plan without making any changes |
| `--public` | Create as a public repo (default: private) |
| `--from REPO` | Mirror code from an existing private repo before making public. Requires `--public`. Mutually exclusive with `--local`. |
| `--audit` | Audit an existing repo and apply missing safe defaults. Mutually exclusive with `--local`. |
| `--json` | Emit the plan as JSON to stdout instead of the ANSI table. All other messages go to stderr. Combine with `--dry-run` for clean machine-readable output. |
| `--config PATH` | Path to config file (default: `~/.config/gh-safe-repo/config.ini`) |
| `--debug` | Print every API call and response |
| `--help` | Show help and exit |

---

## Dry Run / Plan Output

`--dry-run` shows exactly what `gh-safe-repo` would do, without making any changes or API calls. Use it before running for real. Combine with `--json` for machine-readable plan output:

```bash
gh-safe-repo my-project --dry-run --json
gh-safe-repo my-existing-repo --audit --dry-run --json
```

When `--json` is active, the plan is written to stdout as a JSON object and all other messages (progress, warnings, the "Dry run" footer) go to stderr, so the output is clean for piping or scripting.

```
$ gh-safe-repo my-project --dry-run

  Plan for my-project (private)

  Category            Action  Setting                          Value
  ──────────────────────────────────────────────────────────────────
  Repository          ADD     repository                       my-project (private)
  Repository          ADD     has_wiki                         false
  Repository          ADD     has_projects                     false
  Repository          ADD     delete_branch_on_merge           true
  Repository          ADD     allow_merge_commit               false
  Actions             ADD     default_workflow_permissions     read
  Actions             ADD     can_approve_pull_request_reviews false
  Branch Protection   SKIP    branch_protection                Not available for private repos on free plan
  Security            SKIP    dependabot_alerts                Not available for private repos on free plan
  1 setting skipped (GitHub plan limitation).
  Dry run — no changes made.
```

**Action colours:**

| Action | Meaning |
|---|---|
| `ADD` (green) | New setting being applied |
| `UPDATE` (yellow) | Existing setting being changed (audit mode) |
| `DELETE` (red) | Setting being removed |
| `SKIP` (dim) | Feature unavailable on your plan/visibility combination |

**JSON output** (`--json`):

```json
{
  "changes": [
    { "type": "add",  "category": "repository",         "key": "has_wiki",  "old": null, "new": false, "reason": null },
    { "type": "skip", "category": "branch_protection",   "key": "branch_protection", "old": null, "new": null, "reason": "Not available for private repos on free plan" }
  ],
  "summary": { "add": 5, "skip": 2 }
}
```

`summary` only includes types that are present in the plan. Consumers should use `.get("delete", 0)` etc. rather than assuming all four keys are present.

---

## Audit Mode

`--audit` compares an existing repo's current settings against the safe defaults and applies any differences.

```bash
# See what's out of compliance
gh-safe-repo existing-repo --audit --dry-run

# Apply missing safe defaults
gh-safe-repo existing-repo --audit
```

Audit mode:

1. Fetches the current value of every setting via the GitHub API
2. Compares against desired safe defaults
3. Shows a plan table with `UPDATE` for changed settings and `SKIP` for settings already at the desired value (no-op detection — it never makes API calls that would change nothing)
4. Prompts for confirmation before applying

Settings that are already correct are silently skipped. Only real changes are shown and applied.

---

## Public Repos from Private (`--from`)

Making a private repo public is the riskiest thing you can do on GitHub. The `--from` workflow is designed to make it safe:

```bash
gh-safe-repo my-public-project --from my-private-project --public
```

**What happens, in order:**

1. The source repo (`my-private-project`) is cloned locally (full clone, no `--depth`, so truffleHog can walk the full commit history)
2. The [pre-flight security scanner](#pre-flight-security-scanner) runs on the local clone
3. You review findings and confirm (or abort)
4. A new repo (`my-public-project`) is created as **public**
5. Branch protection is applied **before any code is pushed**
6. The full history is mirrored: `git clone --mirror` + `git push --mirror`
7. Dependabot and secret scanning are configured

Branch protection is applied before the push intentionally. If the scan reveals a problem and you abort, no code is ever copied to GitHub.

> **Note:** `--from` requires `--public`. Mirroring to a private repo with no branch protection is not supported.

---

## Creating a Repo from a Local Directory (`--local`)

`--local PATH` is the local-to-GitHub counterpart to `--from`. It creates a new GitHub repo and pushes code from a directory on your machine.

```bash
gh-safe-repo my-project --local ~/projects/myapp
gh-safe-repo my-project --local ~/projects/myapp --public
```

**What happens, in order:**

1. The [pre-flight security scanner](#pre-flight-security-scanner) runs on the local directory directly (no clone needed)
2. You review findings and confirm (or abort)
3. A new repo is created with safe defaults applied
4. Branch protection is applied **before any code is pushed** (when `--public`)
5. Code is pushed:
   - If `PATH` is a git repo: the full history is cloned locally and pushed with `push --all --tags` (all branches and tags)
   - If `PATH` is a plain directory: files are staged in a fresh repo and pushed as an initial commit
   - If `PATH` is an empty directory: nothing is pushed (silently skipped)
6. If `PATH` is a git repo, `origin` is added to the **original** local repo pointing at the new GitHub URL, and the current branch's upstream tracking is configured — so `git push` and `git pull` work immediately without extra setup.

Unlike `--from`, `--local` works for both private and public repos. It is mutually exclusive with `--from` and `--audit`.

When `PATH` is a git repo, the local default branch (via `git symbolic-ref HEAD`) is used to target branch protection rules, so protection lands on the right branch even if it isn't `main`.

> **Tip:** Run `gh-safe-repo --scan PATH` first if you want to inspect findings without creating anything.

---

## Pre-flight Security Scanner

The scanner runs locally and never sends code to GitHub. Use it standalone before any push, or it runs automatically as part of the `--from --public` workflow.

### Standalone scan

```bash
# Scan the current directory
gh-safe-repo --scan .

# Scan an explicit path
gh-safe-repo --scan ~/projects/myapp
```

Exit code is `0` if no critical findings, `1` if criticals are found — so it composes cleanly with other commands:

```bash
gh-safe-repo --scan . && git push
```

The full `[pre_flight_scan]` config applies: `banned_strings`, `max_file_size_mb`, `trufflehog_mode`, etc.

### What it detects

| Category | Severity | Examples |
|---|---|---|
| Hardcoded secrets | Critical | AWS keys (`AKIA…`), GitHub tokens (`ghp_…`, `github_pat_…`), private keys, database URLs |
| Banned strings | Critical | Any literal strings you configure (usernames, internal hostnames, codenames) |
| AI context files | Critical | `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `copilot-instructions.md`, `.cursor/` — may contain internal dev notes; git history may be more sensitive than the current version |
| Email addresses | Warning | Any `user@domain.tld` pattern in working tree and git history |
| Large files | Warning | Files over the configured size threshold (default: 100 MB) |
| TODO/FIXME comments | Info | `# TODO`, `# FIXME`, `# HACK`, `# XXX` |

### Scanner engine

`gh-safe-repo` automatically picks the best available scanner using a three-step discovery chain:

1. **truffleHog v3 on PATH** — runs `trufflehog --version`, verifies it is v3, and uses it. A v2 install or an unrecognised version prints a warning and falls through to step 2.
2. **podman or docker** — if no native truffleHog is found, the scanner runs truffleHog in a container (`ghcr.io/trufflesecurity/trufflehog:latest`) using `podman run` or `docker run`, mounting the scan path read-only at the same absolute path so JSON output paths are identical to a native run.
3. **Regex fallback** — if neither a native install nor a container runtime is available, a warning is printed and the regex scanner runs instead. It also always runs in addition to truffleHog for emails and TODOs, and catches lone key-ID patterns that truffleHog deliberately skips (truffleHog requires both halves of a credential pair, e.g. AWS Key ID *and* Secret Access Key, before flagging a finding).

The selected scanner is shown in the "Running pre-flight security scan..." header and in the plan table's SCAN entry, e.g.:

```
Running pre-flight security scan... (truffleHog v3.93.4)
Running pre-flight security scan... (truffleHog via podman)
Running pre-flight security scan... (regex only — see warning above)
```

Environment variables respected by the container path: `CONTAINER_RUNTIME` to override runtime selection (e.g. `CONTAINER_RUNTIME=docker`), and `TRUFFLEHOG_IMAGE` to pin a specific image tag.

### Running truffleHog via podman or Docker (no local install)

No manual setup is required. `gh-safe-repo` detects podman or docker automatically (step 2 above) and runs truffleHog in a container with the correct volume mounts. `CONTAINER_RUNTIME` and `TRUFFLEHOG_IMAGE` environment variables are respected.

A shell wrapper (`tools/trufflehog`) and a `Containerfile` for building a pinned local image are provided in [`tools/`](tools/README.md) for users who want container-based truffleHog available system-wide, or who need an air-gapped image.

### Interactive review

```
Pre-flight scan: my-private-project

  CRITICAL  my_private_project/config.py:12  AWS Access Key ID
            [redacted]

  WARNING   my_private_project/setup.py:3    Email address
            author_email="alice@example.com"

  1 critical finding, 1 warning.

  Critical findings detected. Continue anyway? [y/N]:
```

- **Critical findings:** Default is abort (`N`). You must explicitly type `y` to continue.
- **Warnings only:** Default is continue (`Y`). Press Enter to proceed or type `n` to abort.
- **No findings:** Scan completes silently and the workflow continues.

Secrets are redacted in the output. Email addresses and TODOs show the matching line.

### Scan coverage

Build-artifact directories (`node_modules`, `__pycache__`, `.venv`, `venv`, `dist`, `build`) are skipped by default to keep scans fast. In git repos, this skip is conditional: before pruning a directory, the scanner runs `git ls-files -- <dir>` to check whether any files inside are tracked. If they are, the directory is scanned normally.

This means committed `node_modules` or `dist` trees — unusual, but they happen — are not silently missed. Uncommitted directories (the normal case) continue to be skipped as before.

A warning is still printed when SKIP_DIRS subdirectories are found in a cloned source repo, since their presence may indicate that more content than expected is committed.

### Suppressing false positives

Two config keys let you suppress known-safe findings without disabling entire check categories.

**`scan_exclude_paths`** — skip files or directories entirely. Values are newline/comma-separated regex patterns matched against the relative file path. A matching file is excluded from every check: secrets, emails, TODOs, large files, and AI context file detection. The same patterns are also passed to truffleHog via `--exclude-paths`, so coverage is consistent regardless of which scanner engine is active.

```ini
[pre_flight_scan]
# Exclude the GitHub API spec (example tokens) and all test fixtures
scan_exclude_paths = docs/api\.github\.com\.json
    tests/fixtures/
```

**`exclude_emails`** — suppress email findings for specific addresses or entire domains. Values are newline/comma-separated, case-insensitive. Entries starting with `@` match all emails at that domain; otherwise the entry must match the full address exactly. Applies to both working-tree and git history findings.

```ini
[pre_flight_scan]
# Suppress bot addresses and placeholder domains
exclude_emails = action@github.com, noreply@github.com, @example.com
```

### Scanner configuration

```ini
[pre_flight_scan]
scan_for_secrets = true
scan_for_emails = true
scan_for_todos = true
max_file_size_mb = 100

# Scan git history for email addresses (requires scan_for_emails = true)
# scan_email_history = true

# Scanner selection: auto | native | docker | off
#   auto   — try native truffleHog, fall back to container (podman/docker), then regex (default)
#   native — native truffleHog only; no container fallback
#   docker — container only; skip native PATH check
#   off    — regex scanner only, no truffleHog attempt
# trufflehog_mode = auto

# Flag AI context files (CLAUDE.md, AGENTS.md, .cursorrules, etc.) as critical findings.
# Their git history may contain more sensitive content than the current version.
# warn_ai_context_files = true

# Literal strings to flag as critical findings (case-insensitive).
# Comma-separated or one per line (continuation lines must be indented).
# banned_strings = secret
#     password
#     credential

# Exclude files/directories from all scan checks (regex patterns, comma/newline separated).
# The same patterns are passed to truffleHog via --exclude-paths.
# scan_exclude_paths = docs/api\.github\.com\.json
#     tests/fixtures/

# Suppress email findings for specific addresses or entire domains (case-insensitive).
# Entries starting with @ match all emails at that domain; otherwise exact address match.
# exclude_emails = action@github.com, noreply@github.com, @example.com
```

When banned strings or AI context files are found the scanner prints a ready-to-run `git filter-repo` command to remove them from the source repo's history before re-running.

---

## Configuration

`gh-safe-repo` reads from `~/.config/gh-safe-repo/config.ini`. All values have safe defaults — no config file is required to get started.

```bash
# Use a custom config file
gh-safe-repo my-project --config ./my-config.ini
```

A fully-annotated example config is included in the repository as `config.ini.example`. Copy it to get started:

```bash
cp config.ini.example ~/.config/gh-safe-repo/config.ini
```

### Full configuration reference

```ini
[repo]
# Whether new repos are private by default
private = true

# Disable features that create clutter if unused
has_wiki = false
has_projects = false
has_issues = true

# Clean up merged branches automatically
delete_branch_on_merge = true

# Merge strategy: disable merge commits, keep squash and rebase
allow_squash_merge = true
allow_merge_commit = false
allow_rebase_merge = true

# Do not initialize with a README — keeps the remote empty so pushing is seamless
auto_init = false


[actions]
# Restrict action sources: all | local_only | selected
# "selected" = GitHub-authored + verified marketplace creators
allowed_actions = selected

# Principle of least privilege: read-only by default
# Options: read | write
default_workflow_permissions = read

# Prevent Actions from self-approving pull requests
can_approve_pull_request_reviews = false

# Require workflows to pin actions to a specific commit SHA instead of a mutable tag
sha_pinning_required = true


[branch_protection]
# Applied to public repos on any plan, and private repos on paid plans.

# Branch to protect
protected_branch = main

# Require a pull request before merging
require_pull_request = true

# Number of approvals required
required_approving_reviews = 1

# Dismiss existing approvals when new commits are pushed
dismiss_stale_reviews = true

# Require all review comments to be resolved before merging
require_conversation_resolution = true

# Do not enforce rules on administrators
# false = repo owner can still push directly (needed for --from mirror workflow)
enforce_admins = false

# Block force-pushes
allow_force_pushes = false

# Block branch deletion
allow_deletions = false

# Use the Rulesets API instead of classic branch protection
# Same rules, but supports bypass actors and is the modern GitHub API
# use_rulesets = false


[security]
# Enable Dependabot vulnerability alerts
enable_dependabot_alerts = true

# Auto-open PRs to fix vulnerable dependencies
enable_dependabot_security_updates = true

# Let security researchers report vulnerabilities privately
enable_private_vulnerability_reporting = true

# Block commits that contain supported secrets
enable_secret_scanning_push_protection = true

# Note: The following features have no REST API and must be configured via UI or dependabot.yml:
#   - Grouped security updates: use dependabot.yml groups with applies-to: security-updates
#   - Automatic dependency submission: enable via repository settings UI
#   - Dependency graph: automatic for public repos; enable via UI for private repos


[pre_flight_scan]
scan_for_secrets = true
scan_for_emails = true
scan_for_todos = true

# Flag files larger than this threshold
max_file_size_mb = 100

# Scan git history for email addresses (requires scan_for_emails = true)
# scan_email_history = true

# Scanner selection: auto | native | docker | off
# auto   = try native truffleHog, fall back to container (podman/docker), then regex
# native = native PATH only
# docker = container only
# off    = regex only
# trufflehog_mode = auto

# Flag AI context files (CLAUDE.md, AGENTS.md, .cursorrules, etc.) as critical findings.
# warn_ai_context_files = true

# Literal strings to flag as critical findings (case-insensitive).
# Comma-separated, or one per line with continuation indentation.
# banned_strings = secret
#     password
#     credential

# Exclude files/directories from all scan checks (regex patterns, comma/newline separated).
# Passed to truffleHog via --exclude-paths as well as applied to the regex walk.
# scan_exclude_paths = docs/api\.github\.com\.json
#     tests/fixtures/

# Suppress email findings for specific addresses or entire domains (case-insensitive).
# Entries starting with @ match all emails at that domain; otherwise exact address match.
# exclude_emails = action@github.com, noreply@github.com, @example.com
```

---

## GitHub Plan Limitations

Some features are only available depending on repo visibility and your GitHub plan.

| Feature | Free + Public | Free + Private | Pro/Team + Private |
|---|:---:|:---:|:---:|
| Branch protection / Rulesets | Yes | No | Yes |
| Dependabot alerts | Yes | No | Yes |
| Dependabot security updates | Yes | No | Yes |
| Secret scanning | Auto | No | Yes |
| Push protection | Yes | No | Yes |
| Private vulnerability reporting | Yes | Yes | Yes |
| Dependency graph | Auto | No | Yes |

`gh-safe-repo` detects your plan level and repo visibility at runtime. Unavailable features appear as `SKIP` in the plan output with a clear reason — the tool never fails silently.

---

## How It Works

```
gh-safe-repo my-project
      │
      ├─ Load config (~/.config/gh-safe-repo/config.ini)
      ├─ Apply CLI flag overrides (--public, etc.)
      ├─ Authenticate via gh CLI or GITHUB_TOKEN
      ├─ GET /user → owner login + plan level  (single cached call)
      │
      ├─ Build plan (each plugin compares desired vs. current state)
      │   ├─ RepositoryPlugin  → repo creation + basic settings
      │   ├─ ActionsPlugin     → workflow permissions
      │   ├─ BranchProtectionPlugin → classic or Rulesets API
      │   └─ SecurityPlugin    → Dependabot, secret scanning, push protection, private vuln reporting
      │
      ├─ Print plan table
      │
      └─ Apply (unless --dry-run)
          ├─ POST /user/repos
          ├─ PATCH /repos/{owner}/{repo}       (settings)
          ├─ PUT  /repos/{owner}/{repo}/actions/permissions/workflow
          ├─ PUT  /repos/{owner}/{repo}/branches/main/protection
          │   or POST /repos/{owner}/{repo}/rulesets (if use_rulesets = true)
          ├─ PUT  /repos/{owner}/{repo}/vulnerability-alerts
          ├─ PUT  /repos/{owner}/{repo}/automated-security-fixes
          ├─ PUT  /repos/{owner}/{repo}/private-vulnerability-reporting
          ├─ PATCH /repos/{owner}/{repo}  (security_and_analysis: push protection)
          ├─ git clone --mirror + git push --mirror (if --from)
          └─ git clone <local> + git push --all --tags (if --local, git repo)
              or git init + add -A + commit + push (if --local, plain dir)
```

### Plugin architecture

Each category of settings is a self-contained plugin class (`gh_safe_repo/plugins/`). Every plugin:

1. Fetches current state from the GitHub API
2. Compares against desired state from config
3. Returns a `Plan` (list of `Change` objects: ADD / UPDATE / DELETE / SKIP)
4. Applies only real changes — no API calls for no-ops

This means audit mode and create mode use the same plan/apply path. The only difference is whether current state is fetched from an existing repo or assumed to be GitHub defaults.

### Authentication

1. `gh auth token` — preferred; uses whatever `gh auth login` set up
2. `GITHUB_TOKEN` environment variable — CI/CD fallback
3. Error if neither is available

Tokens are passed to child `gh api` processes as `GH_TOKEN` in the subprocess environment and are never logged.

### API approach

All GitHub API calls go through `gh api` via `subprocess`. This keeps authentication entirely in the `gh` CLI — no token management code, no OAuth flow, no PyGithub version pinning. JSON request bodies are passed via `--input -` (stdin), not `--field` flags.

---

## Development

```bash
# Clone and set up
git clone https://github.com/your-username/gh-safe-repo
cd gh-safe-repo
uv sync                          # creates .venv, installs pytest

# Run tests
uv run pytest tests/ -v

# Run the tool directly (without installing)
./gh-safe-repo my-project --dry-run

# Install globally (picks up the current source)
uv tool install .
```

See [`tests/README.md`](tests/README.md) for test file descriptions, mocking conventions, and how to add new tests.

### Project structure

```
gh-safe-repo/
├── gh-safe-repo          # Thin launcher (entry point for direct use)
├── gh_safe_repo/         # Package — see gh_safe_repo/README.md for internals
├── pyproject.toml        # Build config, entry points
├── config.ini.example    # Fully annotated example config
└── tests/
```

See [`gh_safe_repo/README.md`](gh_safe_repo/README.md) for the module map, plugin architecture, and a guide to adding new settings.

### Dependency policy

There are **no runtime dependencies**. Everything uses the Python standard library (`argparse`, `configparser`, `subprocess`, `json`, `re`). Do not add third-party packages without discussion.

`pytest` is the only dev dependency, declared as a UV-native `[dependency-groups]` entry in `pyproject.toml`.

---

## Prior Art

These projects were studied during design and influenced the architecture of `gh-safe-repo`. They are distinct tools with different scope and user models — see [docs/LEARNINGS.md](docs/LEARNINGS.md) for detailed technical notes on how patterns were adapted.

- **[github/safe-settings](https://github.com/github/safe-settings)** — Org-level GitHub App (Node.js/Probot) that enforces repository settings from a central config. Source of the plugin architecture pattern (one class per setting category, fetch → diff → apply) and the `mergeDeep` comparison approach.

- **[repository-settings/app](https://github.com/repository-settings/app)** — Simpler per-repo variant of safe-settings, also Node.js/Probot. Provided a cleaner reference for the `Diffable` base plugin pattern.

- **[nicholasgasior/gh-repo-settings](https://github.com/nicholasgasior/gh-repo-settings)** — CLI extension written in Go with a `plan`/`apply` workflow. Primary inspiration for the `gh api` subprocess wrapper pattern and the dry-run plan output design.

