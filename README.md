# gh-safe-repo

Create GitHub repositories with safe defaults applied automatically. Replaces the five-minute post-creation settings checklist with a single command.

```
gh-safe-repo create <owner/repo>
```

Branch protection, immutable tags, Dependabot, restricted Actions permissions, and secret scanning with push protection — all configured before you write your first line of code.

> **Status:** gh-safe-repo is undergoing heavy development. It works well for the core use case — creating a new repo with secure defaults — but the CLI options are still being polished to better match user expectations. Expect breaking changes until releases and CI/CD are nailed down. ✌️

---

## What you get

GitHub's defaults are optimised for discoverability and flexibility, not security: every new repo ships with no branch protection, no Dependabot, and Actions that have write access to the repo and can approve their own pull requests.

`gh-safe-repo` fixes all of that in one shot, showing you a plan first:

- **Private by default** — pass `--public` when you want the world to see it
- **Branch protection** — PR required, 1 approving review, stale reviews dismissed, conversations resolved, no force pushes, no branch deletion
- **Immutable tags** — release tags can't be rewritten or deleted
- **Actions locked down** — read-only token, no self-approving PRs, SHA-pinned actions, GitHub-owned and verified publishers only
- **Dependabot on** — vulnerability alerts plus automatic fix PRs
- **Secret scanning with push protection** — commits containing supported secrets are blocked
- **Private vulnerability reporting** — researchers can reach you without going public
- **Pre-flight secret scan** — when pushing existing code (`--local` / `--from`), your working tree and git history are scanned locally before anything reaches GitHub
- **Opt-in extras** — wiki, projects, and issues are left alone unless you ask for them to be managed (`has_wiki = false` in config)

Everything is configurable. See [Safe defaults in full](#safe-defaults-in-full) for the exact settings and [Configuration](#configuration) for how to change them.

## Requirements

- **Python 3.10+**
- **[`gh` CLI](https://cli.github.com/)** authenticated (`gh auth login`), **or** `GITHUB_TOKEN` set in your environment
- **[`uv`](https://docs.astral.sh/uv/)** for installation from source
- **truffleHog v3** *(optional)* — used by the pre-flight scanner; auto-detected from PATH or run via podman/docker, with a regex fallback if neither exists

Pushing code with `--local` or `--from` additionally needs your normal git credentials for GitHub — see [Working from local or existing code](#working-from-local-or-existing-code).

## Install

```bash
git clone https://github.com/AriESQ/gh-safe-repo
cd gh-safe-repo
uv tool install .

gh-safe-repo --help
```

This installs `gh-safe-repo` into uv's tool environment and adds it to your `PATH`.

To run it without installing:

```bash
uv sync                            # creates .venv
./gh-safe-repo create <owner/repo>
```

## Your first repo

**1. Preview.** `--dry-run` makes zero API calls — nothing is created:

```
$ gh-safe-repo create AriESQ/demo-repo --dry-run

Configuring AriESQ/demo-repo...

Planned Changes
Type    Category           Setting                                 Value / Note
------  -----------------  --------------------------------------  ----------------------------------------------------------------
ADD     repo               repository                              AriESQ/demo-repo
DELETE  file               readme                                  README.md
UPDATE  actions            allowed_actions                         'all' → 'selected'
UPDATE  actions            verified_allowed                        False → True
UPDATE  actions            sha_pinning_required                    False → True
UPDATE  actions            default_workflow_permissions            'write' → 'read'
UPDATE  actions            can_approve_pull_request_reviews        True → False
SKIP    branch_protection  branch_protection                       Branch protection requires a public repo or paid GitHub plan
SKIP    security           dependabot_alerts                       Requires a public repo or paid GitHub plan
SKIP    security           secret_scanning                         Requires a public repo or paid GitHub plan
SKIP    security           dependabot_security_updates             Requires a public repo or paid GitHub plan
SKIP    security           private_vulnerability_reporting         Requires a public repo or paid GitHub plan
SKIP    security           enable_dependency_graph                 Requires a public repo or paid GitHub plan
SKIP    security           enable_secret_scanning_push_protection  Requires a public repo or paid GitHub plan
SKIP    tag_protection     tag_protection                          Tag protection rulesets require a public repo or paid GitHub plan

7 change(s) to apply, 8 skipped

Dry run — no changes made.
```

The `SKIP` rows above are a free-plan private repo: those features need a public repo or a paid plan. Nothing fails silently — see [GitHub plan limitations](#github-plan-limitations).

**2. Apply.** Same command without `--dry-run`. You get the same plan, then a confirmation prompt (`--yes` skips it):

```bash
gh-safe-repo create AriESQ/demo-repo
```

**3. Start working.** On success you get the repo URL and ready-to-paste remote commands:

```
╭─ Done ───────────────────────────────────────────────────╮
│   Repository created successfully!                       │
│   https://github.com/AriESQ/demo-repo                    │
│                                                          │
│   Add remote to existing repo:                           │
│   SSH  : git remote add origin git@github.com:AriESQ/…   │
│   HTTPS: git remote add origin https://github.com/…      │
│                                                          │
│   Clone fresh:                                           │
│   SSH  : git clone git@github.com:AriESQ/demo-repo.git   │
│   HTTPS: git clone https://github.com/AriESQ/demo-…      │
╰──────────────────────────────────────────────────────────╯
```

Remotes are listed with your preferred protocol first (`gh config get -h github.com git_protocol`).

## Common tasks

### Create a repo

```bash
gh-safe-repo create <owner/repo>            # private (default)
gh-safe-repo create <owner/repo> --public   # public — branch and tag protection apply
```

A plain `create` initializes the repo so a default branch exists (branch protection needs one), then deletes the auto-generated `README.md` so you start clean. Set `auto_init = true` in config to keep it.

### Push an existing local project

```bash
gh-safe-repo create <owner/repo> --local ~/projects/myapp
```

Scans the directory for secrets first, creates the repo, pushes all branches and tags, then wires up `origin` and upstream tracking in your original directory so `git push` works immediately. See [Working from local or existing code](#working-from-local-or-existing-code).

### Mirror an existing repo

```bash
gh-safe-repo create <owner/repo> --from <owner/source>
gh-safe-repo create <owner/pub> --from <owner/priv> --public   # the riskiest move — scanned thoroughly
```

Clones the source with full history, scans it, and mirrors it into a fresh repo with safe defaults. If you abort at the scan, no code ever reaches the new repo.

### Audit a repo you already have

```bash
gh-safe-repo fix <owner/repo> --dry-run   # what's out of compliance?
gh-safe-repo fix <owner/repo>             # apply the missing defaults
gh-safe-repo fix <owner/repo> --yes       # no prompt, for scripts
```

`fix` fetches each setting's current value, shows `UPDATE` for what differs and `SKIP` for what's already correct, and makes no API calls for no-ops. It's settings-only — no secret scanning. You need admin on the repo, which can be one owned by an org or another account.

### Scan without touching GitHub

```bash
gh-safe-repo scan .
gh-safe-repo scan ~/projects/myapp
```

Local-only. Exit code `0` when there are no critical findings, `1` when there are, so it composes:

```bash
gh-safe-repo scan . && git push
```

See [Pre-flight security scanner](#pre-flight-security-scanner).

---

## Reference

### CLI reference

```
gh-safe-repo [--config PATH] [--debug] create <owner/repo> [OPTIONS]
gh-safe-repo [--config PATH] [--debug] fix <owner/repo> [OPTIONS]
gh-safe-repo [--config PATH] [--debug] scan <path> [OPTIONS]
```

`--config` and `--debug` are global: they may be given before or after the
command (`gh-safe-repo --debug fix owner/repo` and
`gh-safe-repo fix owner/repo --debug` are equivalent), and the later one wins
if both are given. All other options are command-specific and must follow the
command. Note that `--config` takes an optional value, so a bare `--config`
should come last — `gh-safe-repo --config scan ./src` reads `scan` as the path.

All commands that interact with GitHub require the `owner/repo` format (e.g. `myuser/my-repo`). For `create`, the owner is validated against your authenticated GitHub account to prevent mistakes on multi-account systems. For `fix`, admin permissions on the target repo are required instead, allowing you to fix repos owned by organizations or other accounts where you have admin access.

#### `create` — create a new repo

| Option | Description |
|---|---|
| `--public` | Create as a public repo (default: private) |
| `--local PATH` | Push code from a local git repository into the new repo. Runs pre-flight scan first. Mutually exclusive with `--from`. |
| `--from OWNER/REPO` | Mirror code from an existing repo into the new repo. Runs pre-flight scan. Mutually exclusive with `--local`. |
| `--yes` / `-y` | Skip confirmation prompt and apply immediately (for scripting/batch use). Pre-flight scan warnings are accepted automatically; **critical** findings still stop the run (exit `1`). |
| `--dry-run` | Print the plan without making any changes |
| `--json` | Emit the plan as JSON to stdout instead of the ANSI table |
| `--config [PATH]` | *(global)* Path to config file; bare `--config` uses built-in defaults only |
| `--debug` | *(global)* Print every API call and response |

#### `fix` — audit and fix an existing repo

| Option | Description |
|---|---|
| `--yes` / `-y` | Skip confirmation prompt and apply immediately (for scripting/batch use) |
| `--dry-run` | Show settings diff without applying changes |
| `--migrate-branch-protection` | Convert existing classic branch protection to a ruleset (see [Branch protection](#branch-protection-public-repos-or-any-repo-on-a-paid-plan)) |
| `--json` | Emit the plan as JSON to stdout instead of the ANSI table |
| `--config [PATH]` | *(global)* Path to config file; bare `--config` uses built-in defaults only |
| `--debug` | *(global)* Print every API call and response, plus resolved repo identity (id, full name, owner type) |

#### `scan` — local secret scanning

| Option | Description |
|---|---|
| `--json` | Emit findings as JSON to stdout instead of the ANSI report |
| `--config [PATH]` | *(global)* Path to config file; bare `--config` uses built-in defaults only |
| `--debug` | *(global)* Show scanner details |

Exit code is `0` if no critical findings, `1` if criticals are found.

### Plan output and JSON

`--dry-run` shows exactly what `gh-safe-repo` would do, without making any changes or API calls. Combine with `--json` for machine-readable plan output:

```bash
gh-safe-repo create <owner/repo> --dry-run --json
gh-safe-repo fix <owner/repo> --dry-run --json
```

When `--json` is active, the plan is written to stdout as a JSON object and all other messages (progress, warnings, the "Dry run" footer) go to stderr, so the output is clean for piping or scripting.

**Change types** (the `Type` column, colourised in a terminal):

| Type | Meaning |
|---|---|
| `ADD` (green) | New setting being applied |
| `UPDATE` (yellow) | Existing setting being changed |
| `DELETE` (red) | Setting or file being removed |
| `SKIP` (dim) | No action needed — already at the desired value, or feature unavailable on your plan/visibility combination |

**JSON output** (`--json`):

```json
{
  "changes": [
    { "type": "update", "category": "actions",         "key": "default_workflow_permissions", "old": "write", "new": "read", "reason": null },
    { "type": "skip", "category": "branch_protection", "key": "branch_protection", "old": null, "new": null,  "reason": "Branch protection requires a public repo or paid GitHub plan" }
  ],
  "summary": { "add": 5, "skip": 2 }
}
```

`summary` only includes types that are present in the plan. Consumers should use `.get("delete", 0)` etc. rather than assuming all four keys are present.

**Scan findings** (`scan --json`):

```json
{
  "findings": [
    { "severity": "critical", "category": "secret", "file_path": "app/config.py", "line_number": 12,
      "rule": "AWS Access Key", "match": "[redacted]", "commit": "", "timestamp": "" }
  ],
  "summary": { "critical": 1, "warning": 0, "info": 0 },
  "skipped_committed_dirs": []
}
```

Unlike the plan `summary`, all three severity keys are always present. Secret
matches are `[redacted]`; `commit` and `timestamp` are populated only for
findings truffleHog located in git history.

### Using with an AI agent

Everything the tool does is available non-interactively, and
[`skills/gh-safe-repo/SKILL.md`](skills/gh-safe-repo/SKILL.md) teaches a coding
agent the rules. It deliberately does **not** live in `.claude/skills/`, so it
never auto-loads while you are working in this checkout — you invoke it
explicitly, either way:

```bash
# Ad hoc, no install: point a session at the file
#   "follow ~/path/to/gh-safe-repo/skills/gh-safe-repo/SKILL.md"

# Or install it once, to invoke as /gh-safe-repo from any directory
ln -s "$PWD/skills/gh-safe-repo" ~/.claude/skills/gh-safe-repo
```

The recipe the skill enforces — plan, show the user, then apply:

```bash
gh-safe-repo create alice/my-repo --local . --dry-run --json   # zero API calls
gh-safe-repo create alice/my-repo --local . --yes              # apply
```

ANSI colour is dropped automatically when stdout is not a terminal, and
honours `NO_COLOR`. With `--json`, the machine-readable document is the only
thing on stdout; progress, warnings and errors go to stderr.

**Exit codes**, common to all three commands:

| Code | Meaning |
|---|---|
| `0` | Success, or the user declined at an interactive prompt |
| `1` | Operational failure — auth, permissions, API error, or a pre-flight scan that blocked the run. For `scan`, also "critical findings present" |
| `2` | Usage error — bad `owner/repo`, path is not a directory, `--local` together with `--from` |

A run that cannot ask for confirmation never exits `0` without doing the work:
if there is no terminal and no `--yes`, or `--yes` is set but the pre-flight
scan found critical secrets, the command reports the reason and exits `1`.

### Safe defaults in full

#### Repository settings

| Setting | GitHub default | Safe default | Notes |
|---|---|---|---|
| Visibility | Public | **Private** | Pass `--public` to override |
| Wiki | Enabled | Not managed | Opt in with `has_wiki = false` in `[repo]` |
| Projects | Enabled | Not managed | Opt in with `has_projects = false` |
| Issues | Enabled | Not managed | Opt in with `has_issues = false` |
| Delete branch on merge | Off | Off | Set to `true` in config for auto-cleanup |
| Allow merge commits | On | On | Set to `false` in config for squash-only |
| Allow squash merge | On | On | |
| Allow rebase merge | On | On | |

#### GitHub Actions

| Setting | GitHub default | Safe default |
|---|---|---|
| Allowed actions | All | **Selected** (GitHub-owned + verified creators; customisable) |
| Default workflow permissions | Read/write | **Read-only** |
| Actions can approve PRs | Yes | **No** |
| Require SHA pinning | No | **Yes** (workflows must pin actions to a commit SHA, not a mutable tag) |
| Fork PR approval policy | First-time contributors new to GitHub | **All external contributors** — require approval before fork PR workflows run CI. Options: brand-new GitHub accounts only (GitHub default), first-time repo contributors, or all fork PRs (safest) |

#### Branch protection (public repos, or any repo on a paid plan)

| Rule | Value |
|---|---|
| Require pull request before merge | Yes |
| Required approving reviews | 1 |
| Dismiss stale reviews on push | Yes |
| Require conversation resolution | Yes |
| Allow force pushes | No |
| Allow branch deletion | No |
| Enforce on admins | No (allows owner tooling to push) |

Branch protection is applied via the **Rulesets API** by default (`use_rulesets = true`): a single `gh-safe-repo defaults` ruleset covers every configured branch and expresses "admins can bypass" through a bypass actor rather than the classic `enforce_admins` flag. Set `use_rulesets = false` for the legacy classic per-branch path (kept for one release cycle).

**Migrating an existing repo from classic protection:** if `fix` finds classic branch protection on a repo, it refuses to convert it to a ruleset unless you pass `--migrate-branch-protection`. Classic-only rules have no equivalent in the ruleset this tool builds and would be dropped silently otherwise — known gaps:

- `required_status_checks` — required CI checks are not modelled in the ruleset body.
- `restrictions` (push restrictions by user/team) — Rulesets model this differently via bypass actors; not a 1:1 map.
- Per-branch divergence — a single shared-condition ruleset can't express different rules for `master` vs `main`.

With the flag, `fix` creates/updates the ruleset and then deletes the classic protection on each branch so the two layers don't stack.

#### Tag protection (public repos, or any repo on a paid plan)

Tag protection creates a GitHub Ruleset targeting all tags (`*` by default, configurable via `protected_tags`). The following rules are enforced:

| Ruleset rule | Enforced? | Notes |
|---|---|---|
| Restrict creations | No | |
| **Restrict updates** | **Yes** | Prevents rewriting / force-pushing tags |
| **Restrict deletions** | **Yes** | Prevents `git push --delete` of tags |
| Require linear history | No | |
| Require deployments to succeed | No | |
| Require signed commits | No | |
| Require status checks to pass | No | |
| Block force pushes | No | |

Repository admins are on the bypass list (consistent with the branch protection `enforce_admins = false` default). Only works on public repos or paid GitHub plans (same restriction as branch protection). Free-plan private repos will see this skipped in the plan output.

#### Security

| Feature | Behaviour |
|---|---|
| Dependabot alerts | Enabled (public repos / paid plans) |
| Dependabot security updates | Enabled (auto-opens PRs for vulnerable deps) |
| Secret scanning | Automatic on public repos; enabled on private paid plans |
| Push protection | Enabled (blocks commits containing supported secrets) |
| Private vulnerability reporting | Enabled (lets security researchers report privately) |
| Dependency graph | Automatic on public repos; no REST API for private (UI only) |

### Working from local or existing code

`--local PATH` and `--from OWNER/REPO` both create a new repo *and* populate it with code. They are mutually exclusive, and both work for private and public destinations.

**Git credentials.** Both push over git transport using your own credentials, not the API token: an SSH key loaded into `ssh-agent` when `gh config get -h github.com git_protocol` is `ssh`, or an HTTPS credential helper (`gh auth setup-git` configures one). This is deliberate — the OAuth token would need the `workflow` scope to push `.github/workflows/*` files. In environments with neither (e.g. CI with only `GITHUB_TOKEN`), the tool falls back to HTTPS with the token in the URL; see `[git_transport] mode` in [Configuration](#configuration).

**`--local PATH`** — `PATH` must be an initialized git repository (`git init` or a clone):

1. Your git credentials for `github.com` are verified up front (SSH probe when the protocol is `ssh`; HTTPS is trusted), so a missing key fails fast before any repo is created
2. The [pre-flight security scanner](#pre-flight-security-scanner) runs on the local directory directly (no clone needed)
3. You review findings and confirm (or abort)
4. The repo is created, and actions permissions and security settings are applied
5. History is pushed with `push --all --tags` (all branches and tags)
6. Branch and tag protection are applied — after the push, so the target branch exists
7. `origin` is added to your **original** local repo pointing at the new GitHub URL, and the current branch's upstream is configured, so `git push` and `git pull` work immediately

The local default branch (via `git -C PATH symbolic-ref HEAD`) is used to target branch protection, so protection lands on the right branch even if it isn't `main`.

**`--from OWNER/REPO`** — the remote-to-remote counterpart:

1. Git credentials are verified up front, same as above
2. The source repo is cloned locally (full clone, no `--depth`, so truffleHog can walk the entire commit history)
3. The [pre-flight security scanner](#pre-flight-security-scanner) runs on the clone
4. You review findings and confirm (or abort)
5. The new repo is created (private by default, or public with `--public`)
6. Actions permissions and security settings are applied
7. Full history is mirrored: `git clone --mirror` + `git push --mirror`
8. Branch and tag protection are applied

If the scan reveals a problem and you abort, no code is ever copied to GitHub.

> **Tip:** run `gh-safe-repo scan PATH` first if you want to inspect findings without creating anything.

### Pre-flight security scanner

The scanner runs locally and never sends code to GitHub. Use it standalone (`gh-safe-repo scan <path>`) before any push, or let it run automatically as part of the `--from` and `--local` workflows. The full `[pre_flight_scan]` config applies in both cases.

#### What it detects

| Category | Severity | Examples |
|---|---|---|
| Hardcoded secrets | Critical | AWS keys (`AKIA…`), GitHub tokens (`ghp_…`, `github_pat_…`), private keys, database URLs |
| Banned strings | Critical | Any literal strings you configure (usernames, internal hostnames, codenames) |
| AI context files | Critical | `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `copilot-instructions.md`, `.cursor/` — may contain internal dev notes; git history may be more sensitive than the current version |
| Email addresses | Warning | Any `user@domain.tld` pattern in working tree and git history |
| Large files | Warning | Files over the configured size threshold (default: 100 MB) |
| TODO/FIXME comments | Info | `# TODO`, `# FIXME`, `# HACK`, `# XXX` |

#### Interactive review

```
Pre-flight scan: my-private-project

  CRITICAL  my_private_project/config.py:12  AWS Access Key ID
            [redacted]

  WARNING   my_private_project/setup.py:3    Email address
            author_email="alice@example.com"

  1 critical finding, 1 warning.

  Critical findings detected. Continue anyway? [y/N]:
```

- **Critical findings:** default is abort (`N`). You must explicitly type `y` to continue.
- **Warnings only:** default is continue (`Y`). Press Enter to proceed or type `n` to abort.
- **No findings:** the scan completes silently and the workflow continues.

Secrets are redacted in the output. Email addresses and TODOs show the matching line. When banned strings or AI context files are found, the scanner prints a ready-to-run `git filter-repo` command to remove them from the source repo's history before re-running.

#### Scanner engine

`gh-safe-repo` automatically picks the best available scanner using a three-step discovery chain:

1. **truffleHog v3 on PATH** — runs `trufflehog --version`, verifies it is v3, and uses it. A v2 install or an unrecognised version prints a warning and falls through to step 2.
2. **podman or docker** — if no native truffleHog is found, the scanner runs truffleHog in a container (`ghcr.io/trufflesecurity/trufflehog:latest`), mounting the scan path read-only at the same absolute path so JSON output paths match a native run.
3. **Regex fallback** — if neither is available, a warning is printed and the regex scanner runs instead. It also always runs *in addition to* truffleHog for emails and TODOs, and catches lone key-ID patterns that truffleHog deliberately skips (truffleHog requires both halves of a credential pair, e.g. AWS Key ID *and* Secret Access Key, before flagging a finding).

The selected scanner is shown in the scan header and in the plan table's SCAN entry:

```
Running pre-flight security scan... (truffleHog v3.93.4)
Running pre-flight security scan... (truffleHog via podman)
Running pre-flight security scan... (regex only — see warning above)
```

Set `trufflehog_mode` in config to pin a specific engine. Two environment variables affect the container path: `CONTAINER_RUNTIME` overrides runtime selection (e.g. `CONTAINER_RUNTIME=docker`), and `TRUFFLEHOG_IMAGE` pins a specific image tag. No manual container setup is required, but a shell wrapper (`tools/trufflehog`) and a `Containerfile` for building a pinned local image are provided in [`tools/`](tools/README.md) for system-wide or air-gapped use.

#### Scan coverage

Build-artifact directories (`node_modules`, `__pycache__`, `.venv`, `venv`, `dist`, `build`) are skipped by default to keep scans fast. In git repos this skip is conditional: before pruning a directory, the scanner runs `git ls-files -- <dir>` to check whether any files inside are tracked. If they are, the directory is scanned normally — so committed `node_modules` or `dist` trees are not silently missed. A warning is still printed when such directories are found in a cloned source repo, since their presence may indicate more content is committed than expected.

#### Suppressing false positives

Two config keys suppress known-safe findings without disabling entire check categories.

**`scan_exclude_paths`** — skip files or directories entirely. Values are newline/comma-separated regex patterns matched against the relative file path. A matching file is excluded from every check: secrets, emails, TODOs, large files, and AI context file detection. The same patterns are passed to truffleHog via `--exclude-paths`, so coverage is consistent regardless of engine.

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

The remaining scanner keys are documented in [Configuration](#configuration) below.

### Configuration

`gh-safe-repo` looks for configuration in this order (first match wins):

1. **`--config PATH`** — explicit override
2. **`./gh-safe-repo.ini`** — current working directory
3. **`$XDG_CONFIG_HOME/gh-safe-repo/gh-safe-repo.ini`** — defaults to `~/.config` when `$XDG_CONFIG_HOME` is unset

Bare `--config` (no path) skips file lookup entirely and uses built-in defaults only. All values have safe defaults — no config file is required to get started.

**`gh-safe-repo.ini.example` in the repository root is the canonical, fully-annotated reference** — every key with its default and a comment. `tests/test_config_consistency.py` fails if it drifts from `SAFE_DEFAULTS` in `gh_safe_repo/config_manager.py`, so it is always current. Copy it to get started:

```bash
# User-level config (XDG)
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/gh-safe-repo"
cp gh-safe-repo.ini.example "${XDG_CONFIG_HOME:-$HOME/.config}/gh-safe-repo/gh-safe-repo.ini"

# Or project-level config (current directory)
cp gh-safe-repo.ini.example ./gh-safe-repo.ini
```

The abridged reference below shows the keys and their defaults; see the example file for the full commentary.

```ini
[repo]
private = true
delete_branch_on_merge = false

# Not managed unless you set them. Uncomment to have gh-safe-repo enforce them:
# has_wiki = false
# has_projects = false
# has_issues = false

# Merge strategies — at least one must stay true (GitHub rejects all-false with a 422)
allow_squash_merge = true
allow_merge_commit = true
allow_rebase_merge = true

# Whether a plain `create` leaves the initialized README in the new repo.
# false (default): the repo still gets a default branch (needed for branch
#   protection), but the auto-generated README.md is removed afterward.
# (Ignored for --local/--from, which push your own history instead.)
auto_init = false


[actions]
enabled = true

# all | local_only | selected
allowed_actions = selected

# Only consulted when allowed_actions = selected:
github_owned_allowed = true       # actions maintained by GitHub (e.g. actions/checkout)
verified_allowed = true           # actions from Marketplace verified creators
# patterns_allowed = myorg/*      # comma-separated allowlist (wildcards OK)

# read | write
default_workflow_permissions = read
can_approve_pull_request_reviews = false
sha_pinning_required = true

# Who needs approval before their fork PR runs CI:
#   all_external_contributors | first_time_contributors | first_time_contributors_new_to_github
fork_pr_approval_policy = all_external_contributors


[branch_protection]
# Applied to public repos on any plan, and private repos on paid plans.
# Comma-separated; branches that don't exist are ignored.
protected_branch = master, main
require_pull_request = true
required_approving_reviews = 1
dismiss_stale_reviews = true
require_conversation_resolution = true
allow_force_pushes = false
allow_deletions = false

# false = repo owner can still push directly (needed for the --from mirror workflow)
enforce_admins = false

# Use the Rulesets API (default) instead of the legacy classic branch-protection
# path. Set false to fall back to the classic per-branch API, kept for one
# release cycle.
use_rulesets = true


[tag_protection]
# Immutable tags via Rulesets API. Public repos or paid plans only.
protected_tags = *                # comma-separated glob patterns
prevent_tag_deletion = true       # blocks git push --delete
prevent_tag_update = true         # blocks git tag -f / force-push


[security]
enable_dependabot_alerts = true
enable_dependabot_security_updates = true
enable_private_vulnerability_reporting = true
enable_secret_scanning_push_protection = true

# No REST API exists for these — configure via UI or dependabot.yml:
#   grouped security updates (dependabot.yml groups with applies-to: security-updates),
#   automatic dependency submission (UI), dependency graph on private repos (UI)


[pre_flight_scan]
scan_for_secrets = true
scan_for_emails = true
scan_for_todos = true
max_file_size_mb = 100
scan_email_history = true         # also scan git history for addresses
warn_ai_context_files = true      # flag CLAUDE.md, .cursorrules, etc. as critical

# auto | native | docker | off — see "Scanner engine" above
trufflehog_mode = auto

# Comma-separated or one per line with continuation indentation:
# banned_strings = secret
#     password
# scan_exclude_paths = docs/api\.github\.com\.json
#     tests/fixtures/
# exclude_emails = action@github.com, noreply@github.com, @example.com


[git_transport]
# How git push/clone authenticates for --local / --from: auto | user_creds | token
#   auto       — your own credentials (SSH key or credential helper) when available;
#                fall back to HTTPS with the API token in the URL only when there is
#                no SSH setup and no credential helper (e.g. CI with GITHUB_TOKEN)
#   user_creds — never use the API token for git; avoids needing the `workflow`
#                scope to push .github/workflows files
#   token      — always HTTPS with the API token in the URL, for CI that granted
#                the `workflow` scope intentionally
mode = auto
```

### GitHub plan limitations

Some features are only available depending on repo visibility and your GitHub plan.

| Feature | Free + Public | Free + Private | Pro/Team + Private |
|---|:---:|:---:|:---:|
| Branch protection / Rulesets | Yes | No | Yes |
| Tag protection (Rulesets) | Yes | No | Yes |
| Dependabot alerts | Yes | No | Yes |
| Dependabot security updates | Yes | No | Yes |
| Secret scanning | Auto | No | Yes |
| Push protection | Yes | No | Yes |
| Private vulnerability reporting | Yes | Yes | Yes |
| Dependency graph | Auto | No | Yes |

`gh-safe-repo` detects your plan level and repo visibility at runtime. Unavailable features appear as `SKIP` in the plan output with a clear reason — the tool never fails silently.

### How it works

```
gh-safe-repo create <owner/repo>
      │
      ├─ Parse owner/repo, validate owner matches authenticated user (create only)
      ├─ Load config (./gh-safe-repo.ini or $XDG_CONFIG_HOME/gh-safe-repo/gh-safe-repo.ini)
      ├─ Apply CLI flag overrides (--public, etc.)
      ├─ Authenticate via gh CLI or GITHUB_TOKEN
      ├─ GET /user → owner login + plan level  (single cached call)
      │
      ├─ Build plan (each plugin compares desired vs. current state)
      │   ├─ RepositoryPlugin  → repo creation + basic settings
      │   ├─ ActionsPlugin     → allowed actions, workflow permissions, SHA pinning
      │   ├─ BranchProtectionPlugin → Rulesets API (default; classic if use_rulesets = false)
      │   ├─ SecurityPlugin    → Dependabot, secret scanning, push protection, private vuln reporting
      │   └─ TagProtectionPlugin → immutable tags via Rulesets API
      │
      ├─ Print plan table
      │
      └─ Apply (unless --dry-run)
          ├─ POST /user/repos
          ├─ PATCH /repos/{owner}/{repo}       (settings)
          ├─ PUT  /repos/{owner}/{repo}/actions/permissions/workflow
          ├─ POST/PATCH /repos/{owner}/{repo}/rulesets  (branch protection; default)
          │   or PUT /repos/{owner}/{repo}/branches/main/protection (if use_rulesets = false)
          ├─ PUT  /repos/{owner}/{repo}/vulnerability-alerts
          ├─ PUT  /repos/{owner}/{repo}/automated-security-fixes
          ├─ PUT  /repos/{owner}/{repo}/private-vulnerability-reporting
          ├─ PATCH /repos/{owner}/{repo}  (security_and_analysis: push protection)
          ├─ POST /repos/{owner}/{repo}/rulesets  (tag protection ruleset)
          ├─ git clone --mirror + git push --mirror (if --from)
          └─ git clone <local> + git push --all --tags (if --local, git repo)
              or git init + add -A + commit + push (if --local, plain dir)
```

Each category of settings is a self-contained plugin (`gh_safe_repo/plugins/`) that fetches current state, diffs it against config, returns a `Plan`, and applies only real changes. Create mode and audit mode therefore share one code path — the only difference is whether current state comes from an existing repo or from GitHub's defaults. See [`gh_safe_repo/README.md`](gh_safe_repo/README.md) for the module map and a guide to adding new settings.

**Authentication.** API calls resolve a token in this order:

1. `GITHUB_TOKEN` environment variable — lets you target a specific account without switching the active `gh` session (and is the only credential needed in CI)
2. `gh auth token` — whatever `gh auth login` set up
3. Error if neither is available

Tokens are passed to child `gh api` processes as `GH_TOKEN` in the subprocess environment and are never logged. Git operations use your own credentials instead — see [Working from local or existing code](#working-from-local-or-existing-code). Token-bearing URLs are never written to your repo's `.git/config` and are redacted from all output.

**API approach.** All GitHub API calls go through `gh api` via `subprocess`. This keeps authentication entirely in the `gh` CLI — no token management code, no OAuth flow, no PyGithub version pinning. JSON request bodies are passed via `--input -` (stdin), not `--field` flags.

### Development

```bash
git clone https://github.com/AriESQ/gh-safe-repo
cd gh-safe-repo
uv sync                          # creates .venv, installs pytest

uv run pytest tests/ -v          # run tests
./gh-safe-repo create <owner/repo> --dry-run   # run without installing
uv tool install .                # install globally from current source
```

There are **no runtime dependencies** — everything uses the standard library (`argparse`, `configparser`, `subprocess`, `json`, `re`). Do not add third-party packages without discussion. `pytest` is the only dev dependency, declared as a UV-native `[dependency-groups]` entry in `pyproject.toml`.

```
gh-safe-repo/
├── gh-safe-repo          # Thin launcher (entry point for direct use)
├── gh_safe_repo/         # Package — see gh_safe_repo/README.md for internals
│   ├── cli.py            # Subparser dispatch (create, fix, scan)
│   ├── commands/         # Subcommand implementations
│   │   ├── _common.py    # Shared helpers, CLIContext, plan formatting
│   │   ├── create.py     # create subcommand
│   │   ├── fix.py        # fix subcommand
│   │   └── scan.py       # scan subcommand
│   └── plugins/          # Settings plugins (one per category)
├── pyproject.toml        # Build config, entry points
├── gh-safe-repo.ini.example  # Fully annotated example config
└── tests/
```

See [`tests/README.md`](tests/README.md) for test file descriptions, mocking conventions, and how to add new tests, and [`gh_safe_repo/README.md`](gh_safe_repo/README.md) for the module map and plugin architecture.

### Prior art

These projects were studied during design and influenced the architecture of `gh-safe-repo`. They are distinct tools with different scope and user models — see [docs/LEARNINGS.md](docs/LEARNINGS.md) for detailed technical notes on how patterns were adapted.

- **[github/safe-settings](https://github.com/github/safe-settings)** — Org-level GitHub App (Node.js/Probot) that enforces repository settings from a central config. Source of the plugin architecture pattern (one class per setting category, fetch → diff → apply) and the `mergeDeep` comparison approach.

- **[repository-settings/app](https://github.com/repository-settings/app)** — Simpler per-repo variant of safe-settings, also Node.js/Probot. Provided a cleaner reference for the `Diffable` base plugin pattern.

- **[nicholasgasior/gh-repo-settings](https://github.com/nicholasgasior/gh-repo-settings)** — CLI extension written in Go with a `plan`/`apply` workflow. Primary inspiration for the `gh api` subprocess wrapper pattern and the dry-run plan output design.
