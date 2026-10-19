# gh-safe-repo

[![CI](https://github.com/AriESQ/gh-safe-repo/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/AriESQ/gh-safe-repo/actions/workflows/ci.yml)

Create GitHub repositories with safe defaults applied, in one command.

```
gh-safe-repo new <owner/repo>
```

Branch protection, immutable tags, locked-down Actions, Dependabot, secret
scanning with push protection, and private vulnerability reporting are all
configured before you push your first commit. The same tool audits and fixes
repos you already have, and scans local code for secrets before it leaves
your machine.

> **Status:** the core workflow is stable and verified end to end, but the
> CLI is still being polished. Expect occasional breaking changes before 1.0.

## Contents

- [What you get](#what-you-get)
- [Install](#install)
- [Quick start](#quick-start)
- [Commands](#commands)
- [CLI reference](#cli-reference)
- [Output](#output) — plan table, JSON, verification, exit codes
- [Safe defaults](#safe-defaults)
- [Pushing code](#pushing-code) — `push` and `copy`
- [Pre-flight scanner](#pre-flight-scanner)
- [Configuration](#configuration)
- [GitHub plan limitations](#github-plan-limitations)
- [Using with an AI agent](#using-with-an-ai-agent)
- [How it works](#how-it-works)
- [Development](#development)

## What you get

GitHub's defaults favour discoverability over safety: a new repo has no
branch protection, no Dependabot, and Actions that can write to the repo and
approve their own pull requests. `gh-safe-repo` changes that, and shows you
the plan before it touches anything:

- **Private by default.** Pass `--public` to make it public.
- **Branch protection.** PR required, one approving review, stale reviews
  dismissed, conversations resolved, no force pushes, no branch deletion.
- **Immutable tags.** Release tags cannot be rewritten or deleted.
- **Actions locked down.** Read-only token, no self-approving PRs, SHA-pinned
  actions, GitHub-owned and verified publishers only.
- **Dependabot on.** Vulnerability alerts plus automatic fix PRs.
- **Secret scanning with push protection.** Commits containing supported
  secrets are blocked.
- **Private vulnerability reporting.** Researchers can reach you privately.
- **Pre-flight secret scan.** When pushing existing code, the working tree
  and git history are scanned locally first.
- **Opt-in extras.** Wiki, projects and issues are left alone unless you
  manage them in config.

Every setting is configurable. See [Safe defaults](#safe-defaults) for the
exact values and [Configuration](#configuration) for how to change them.

## Install

Requirements:

- Python 3.10+
- [`gh` CLI](https://cli.github.com/) logged in (`gh auth login`), or
  `GITHUB_TOKEN` set in the environment
- [`uv`](https://docs.astral.sh/uv/) to install from source
- truffleHog v3 (optional) for the pre-flight scanner. It is found on `PATH`
  or run via podman/docker; without either, a regex scanner is used.

Pushing code with `push` or `copy` also needs your normal git
credentials for GitHub. See [Pushing code](#pushing-code).

```bash
git clone https://github.com/AriESQ/gh-safe-repo
cd gh-safe-repo
uv tool install .
gh-safe-repo --help
```

To run without installing:

```bash
uv sync
./gh-safe-repo new <owner/repo>
```

## Quick start

**1. Preview.** `--dry-run` makes no API calls and creates nothing.

```
$ gh-safe-repo new AriESQ/demo-repo --dry-run

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

The `SKIP` rows are what a private repo on a free plan looks like. Those
features need a public repo or a paid plan, and the tool says so rather than
failing silently. See [GitHub plan limitations](#github-plan-limitations).

**2. Apply.** Drop `--dry-run`. You see the same plan, then a confirmation
prompt (`--yes` skips it).

```bash
gh-safe-repo new AriESQ/demo-repo
```

**3. Start working.** On success you get a **Verified** section (every
setting read back from GitHub) and a banner with ready-to-paste commands:

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

Your preferred protocol (`gh config get -h github.com git_protocol`) is
listed first.

## Commands

### Create an empty repo

```bash
gh-safe-repo new <owner/repo>            # private (default)
gh-safe-repo new <owner/repo> --public   # public: branch and tag protection apply
```

The repo is initialized so a default branch exists (branch protection needs
one), then the auto-generated `README.md` is deleted so you start clean. Set
`auto_init = true` in config to keep it. The owner must match your
authenticated account; this guards against mistakes on multi-account
systems.

### Push an existing local project

```bash
gh-safe-repo push ~/projects/myapp <owner/repo>
```

Scans the directory for secrets, creates the repo, pushes every branch and
tag, then sets `origin` and upstream tracking in your original directory so
`git push` works immediately. An `origin` that already points elsewhere is
left alone and the banner tells you what to run instead. Afterwards every
pushed branch and tag is checked against GitHub SHA for SHA. Details in
[Pushing code](#pushing-code).

### Mirror an existing repo

```bash
gh-safe-repo copy <owner/source> <owner/repo>
gh-safe-repo copy <owner/priv> <owner/pub> --public   # scanned thoroughly first
```

Clones the source with full history, scans it, mirrors it into the new repo,
and copies the source's default branch, description and topics. The result
is a new repository, not a GitHub fork: it has no "forked from" link. If you
abort at the scan, no code reaches GitHub.

### Audit a repo you already have

```bash
gh-safe-repo audit <owner/repo>           # what is out of compliance? exit 3 if anything
gh-safe-repo audit <owner/repo> --json    # the same, as a plan document
```

Read-only. `audit` fetches each setting and prints `UPDATE` for what differs,
or one line, `All settings compliant.`, when nothing does. It never prompts
and never changes anything. The exit code makes it a CI gate: `0` when
compliant, `3` when `fix` would change something. You need admin on the
repo, which may be owned by an org or another account.

### Fix a repo you already have

```bash
gh-safe-repo fix <owner/repo>             # apply the missing defaults
gh-safe-repo fix <owner/repo> --yes       # no prompt, for scripts
gh-safe-repo fix <owner/repo> --dry-run   # the audit, exit 0 like every dry run
```

`fix` runs the same audit, shows the plan, confirms, and only calls the API
for real changes. It is settings-only (no secret scanning). Afterwards every
changed setting is read back from GitHub.

### Scan local code

```bash
gh-safe-repo scan                   # the current directory
gh-safe-repo scan ./src && git push # exit 1 on critical findings
```

Local only. Nothing is sent to GitHub. See [Pre-flight scanner](#pre-flight-scanner).

## CLI reference

```
gh-safe-repo [--config PATH | --no-config] [--debug] <command> ...

new    <owner/repo>                   [--public] [--dry-run] [--yes] [--json]
push   <path> <owner/repo>            [--public] [--dry-run] [--yes] [--json]
copy   <src-owner/repo> <owner/repo>  [--public] [--dry-run] [--yes] [--json]
audit  <owner/repo>                   [--json]
fix    <owner/repo>                   [--dry-run] [--yes] [--json] [--migrate-branch-protection]
scan   [path]                         [--json]
```

Two rules cover the options. Every command that creates a repo takes
`--public`. Every command that changes GitHub takes `--dry-run`, `--yes` and
`--json`; `audit` and `scan` are read-only and take only `--json`. The one
command-specific flag is `fix --migrate-branch-protection`.

Where a command takes two positionals, the source comes first and the new
repo second, as with `cp`. Every GitHub-targeting command takes `owner/repo`
(for example `myuser/my-repo`). For `new`, `push` and `copy`, the
owner of the new repo must be your authenticated account.

**Global options**

`--config`, `--no-config` and `--debug` may go before or after the command;
if given twice, the later one wins.

| Option | Description |
|---|---|
| `--config PATH` | Config file to use (default: `./gh-safe-repo.ini`, then XDG; see [Configuration](#configuration)) |
| `--no-config` | Ignore config files and use built-in defaults only. Cannot be combined with `--config` |
| `--debug` | Print every API call and response. For `audit` and `fix`, also the resolved repo identity (id, full name, owner type) |

**`new <owner/repo>`, `push <path> <owner/repo>`, `copy <src-owner/repo> <owner/repo>`**

| Option | Description |
|---|---|
| `--public` | Create a public repo (default: private) |
| `--dry-run` | Print the plan and make no API calls. `push` and `copy` skip the pre-flight scan too |
| `--yes`, `-y` | Skip the confirmation prompt. Scan warnings are accepted; critical findings still stop the run (exit `1`). Required when stdin is not a terminal |
| `--json` | Emit the plan as JSON on stdout. On a real run it is printed once at the end with a `verification` block |

`push` requires `path` to be an initialized git repository and runs the
pre-flight scan on it. `copy` clones the source with full history and
scans the clone. Both work for private and public destinations.

**`audit <owner/repo>`**

| Option | Description |
|---|---|
| `--json` | Emit the plan as JSON on stdout |

Exits `3` when anything would change, `0` when compliant.

**`fix <owner/repo>`**

| Option | Description |
|---|---|
| `--dry-run` | Show the settings diff without applying it (exit `0`) |
| `--yes`, `-y` | Skip the confirmation prompt. Required when stdin is not a terminal |
| `--migrate-branch-protection` | Convert classic branch protection to a ruleset. See [Branch protection](#branch-protection) |
| `--json` | Emit the plan as JSON on stdout. On a real run it is printed once at the end with a `verification` block |

**`scan [path]`**

| Option | Description |
|---|---|
| `--json` | Emit findings as JSON on stdout |

`path` defaults to the current directory.

## Output

### Plan table

The `Type` column is colourised in a terminal. Colour is dropped when stdout
is not a terminal or `NO_COLOR` is set.

| Type | Meaning |
|---|---|
| `ADD` | New setting being applied |
| `UPDATE` | Existing setting being changed |
| `DELETE` | Setting or file being removed |
| `SKIP` | Nothing to do: already correct, or unavailable on this plan/visibility |

### JSON

With `--json`, the document is the only thing on stdout. Progress, warnings,
prompts and the banner go to stderr.

- `audit --json` and `--dry-run --json` print the plan immediately.
- A real `new`/`push`/`copy`/`fix` run prints the plan once at the end with a
  `verification` block. Without `--yes` the plan table is shown on stderr
  before the prompt. Declining the prompt prints the document without
  `verification`.
- A run that stops with exit `1` before or during the apply prints no
  document: stdout is empty and the error is on stderr.

```json
{
  "changes": [
    { "type": "update", "category": "actions",         "key": "default_workflow_permissions", "old": "write", "new": "read", "reason": null },
    { "type": "skip", "category": "branch_protection", "key": "branch_protection", "old": null, "new": null,  "reason": "Branch protection requires a public repo or paid GitHub plan" }
  ],
  "summary": { "add": 5, "skip": 2 }
}
```

`summary` only lists types present in the plan; use `.get("delete", 0)`
rather than assuming all four keys.

`scan --json`:

```json
{
  "findings": [
    { "severity": "critical", "category": "secret", "file_path": "app/config.py", "line_number": 12,
      "rule": "AWS Access Key", "match": "[redacted]", "commit": "", "timestamp": "" }
  ],
  "summary": { "critical": 1, "warning": 0, "info": 0 },
  "skipped_committed_dirs": [],
  "engine": { "configured": "auto", "used": "trufflehog-native", "fell_back": false }
}
```

All three severity keys are always present. Secret matches are `[redacted]`.
`commit` and `timestamp` are set only for findings truffleHog located in git
history. `engine` records what actually ran: `configured` is the effective
`trufflehog_mode`, `used` is `trufflehog-native`, `trufflehog-container` or
`regex`, and `fell_back` is `true` when truffleHog was attempted but could
not run.

### Verification

After applying, the creating commands and `fix` read every changed setting back from
GitHub and print one row per setting. Only settings the run changed are
re-read; `SKIP` rows are not. GitHub is briefly inconsistent after a write,
so a wrong or unreadable value is re-read after 1s, 2s and 4s before it is
reported.

```
Verified
  ✓ security/dependabot_alerts
  ✗ repo/has_wiki: expected False, GitHub has True
  ? actions/fork_pr_approval_policy: could not read it back
  → Run `gh-safe-repo fix alice/demo --yes` once more; if it still does not stick, ...
```

| Mark | Status | Meaning | Exit |
|---|---|---|---|
| `✓` | `ok` | GitHub holds the requested value | `0` |
| `✗` | `mismatch` | GitHub holds something else | `3` |
| `?` | `unknown` | Could not be read back (permission or server error) | `0`, with a warning |

The creating commands add checks that only a new repo needs:

| Check | Passes when | On failure |
|---|---|---|
| `repo/repository` | the new repo is found | exit `1`; nothing else is checked |
| `repo/visibility` | visibility is what you asked for | exit `1`, with the `gh repo edit --visibility` command |
| `file/readme` | the auto-generated README is gone (`new` without `auto_init = true`) | exit `3`, with the `gh api` command to delete it |
| `repo/description`, `repo/topics` | `copy`: copied from the source | exit `3`, with the `gh repo edit` command |
| `branch_protection/protected_branches`, `tag_protection/protected_tags` | the ruleset covers the default branch and configured tag patterns | exit `3` |
| `code/branches`, `code/tags` | `push`: every pushed ref is on GitHub at the pushed SHA. `copy`: every ref of the source is on the new repo at the source's SHA. Annotated tags compare by tag object SHA | exit `1`, with the push or mirror command to re-run |
| `repo/default_branch` | `push`/`copy`: the default branch is the one chosen | exit `3`, with the `gh repo edit --default-branch` command |
| `local/origin`, `local/tracking` | `push`: your repo's `origin` is the new repo and the current branch tracks it | warning only (`"advisory": true`), with the `git remote` or `git push -u` command. An `origin` that already pointed elsewhere is reported as left alone |

Remote refs are read through the REST API, not `git ls-remote`, so
verification costs no extra SSH connection or hardware-key touch. The banner
is green `Done` when every check is `ok` (advisory warnings aside), yellow
`Done with problems` on a mismatch, and red `Failed` on a fatal check.

With `--json`:

```json
"verification": {
  "ok": false,
  "checks": [
    { "category": "repo", "key": "has_wiki", "status": "mismatch",
      "expected": false, "actual": true, "fatal": false, "advisory": false,
      "remediation": "Run `gh-safe-repo fix alice/demo --yes` once more; ..." }
  ],
  "summary": { "ok": 3, "mismatch": 1, "unknown": 0 }
}
```

All three `summary` keys are always present. `actual` is `null` for
`unknown`. An advisory check counts as a `mismatch` and makes `ok` false but
never changes the exit code. For `code/branches` and `code/tags`, `expected`
is the `{name: sha}` map that was pushed and `actual` is GitHub's SHA for
each name (`null` when missing). The block is absent on `--dry-run` and when
you decline the prompt; when there is nothing to change it is present with
no checks and `"ok": true`.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success, or the user declined at an interactive prompt. For `audit`, the repo is compliant. For the mutating commands, success means everything applied was read back and verified |
| `1` | Operational failure: auth, permissions, API error, a pre-flight scan that blocked the run, a confirmation prompt that hit end of input without `--yes`, a credential pre-flight failure, or a failed code push (the repo exists but is empty; recovery commands are printed). For `create`, also a fatal verification check. For `scan`, also critical findings present |
| `2` | Usage error: bad `owner/repo`, path is not a directory or not a git repo, `--config` with `--no-config`, an unknown flag |
| `3` | `audit` found settings that `fix` would change. Or a mutating command applied the changes but at least one did not stick when read back |
| `130` | Interrupted with Ctrl-C |

An unattended run never exits `0` without doing the work. With no terminal
and no `--yes`, or with `--yes` and critical scan findings, the command
reports why and exits `1`. If stdin is closed (`< /dev/null`, a CI job, an
agent subprocess) it prints `--yes is required for unattended runs` and
exits `1`. An answer piped on stdin counts: `echo y | gh-safe-repo fix
<owner/repo>` applies, and `n`, an empty line or Ctrl-C at the prompt
declines (exit `0`).

## Safe defaults

### Repository

| Setting | GitHub default | Safe default | Notes |
|---|---|---|---|
| Visibility | Public | **Private** | `--public` to override |
| Wiki, projects, issues | Enabled | Not managed | Opt in with `has_wiki = false` etc. in `[repo]` |
| Delete branch on merge | Off | Off | `delete_branch_on_merge = true` for auto-cleanup |
| Merge commit / squash / rebase | On | On | At least one must stay on; GitHub rejects all-off |

### GitHub Actions

| Setting | GitHub default | Safe default |
|---|---|---|
| Allowed actions | All | **Selected**: GitHub-owned and verified creators |
| Default workflow permissions | Read/write | **Read-only** |
| Actions can approve PRs | Yes | **No** |
| Require SHA pinning | No | **Yes** |
| Fork PR approval policy | New GitHub accounts only | **All external contributors** need approval before fork PR workflows run |

### Branch protection

Public repos on any plan, or any repo on a paid plan.

| Rule | Value |
|---|---|
| Require pull request before merge | Yes |
| Required approving reviews | 1 |
| Dismiss stale reviews on push | Yes |
| Require conversation resolution | Yes |
| Allow force pushes | No |
| Allow branch deletion | No |
| Enforce on admins | No, so owner tooling can push |

Protection is applied with the Rulesets API by default (`use_rulesets =
true`): one `gh-safe-repo defaults` ruleset covers every configured branch,
and admin bypass is expressed as a bypass actor. Set `use_rulesets = false`
for the legacy classic per-branch API, kept for one release cycle.

**Migrating from classic protection.** When `fix` finds classic branch
protection it refuses to convert it unless you pass
`--migrate-branch-protection`, because some classic rules have no equivalent
in the ruleset this tool builds and would be dropped:

- `required_status_checks` (required CI checks)
- `restrictions` (push restrictions by user or team; rulesets model this
  differently via bypass actors)
- per-branch differences, since one ruleset applies the same rules to
  `master` and `main`

With the flag, `fix` creates or updates the ruleset and then deletes the
classic protection on each branch so the two do not stack.

### Tag protection

Public repos on any plan, or any repo on a paid plan. A ruleset targeting
`*` (configurable via `protected_tags`) enables **restrict updates** and
**restrict deletions**, so tags cannot be force-pushed or deleted. Repo
admins are on the bypass list, matching `enforce_admins = false`.

### Security

| Feature | Behaviour |
|---|---|
| Dependabot alerts | Enabled |
| Dependabot security updates | Enabled; opens PRs for vulnerable dependencies |
| Secret scanning | Automatic on public repos; enabled on private paid-plan repos |
| Push protection | Enabled; blocks commits containing supported secrets |
| Private vulnerability reporting | Enabled |
| Dependency graph | Automatic on public repos; private repos need the UI (no REST API) |

## Pushing code

`push` and `copy` both create a new repo and populate it, from a local
git repository or from an existing GitHub repo. Both work for private and
public destinations. Run `gh-safe-repo scan PATH` first if you want to
inspect findings without creating anything.

### Git credentials

Pushes use your own git credentials, not the API token: an SSH key in
`ssh-agent` when `gh config get -h github.com git_protocol` is `ssh`, or an
HTTPS credential helper (`gh auth setup-git` sets one up). This is
deliberate: the OAuth token would need the `workflow` scope to push
`.github/workflows/*`. Where neither exists (CI with only `GITHUB_TOKEN`),
the tool falls back to HTTPS with the token in the URL. See `[git_transport]
mode` in [Configuration](#configuration). Token-bearing URLs are never
written to your `.git/config` and are redacted from all output.

SSH settings such as `core.sshCommand` are read from the git config of the
`push` path, or of the **current directory** for `copy`. If you pick a
key per directory (`includeIf` plus `core.sshCommand`), run `copy` from a
directory where that applies, or ssh falls back to your default key and
fails with `Permission denied (publickey)`. The error then names the
directory and the `core.sshCommand` it found; `--debug` prints it up front.

### `push <path> <owner/repo>`

`path` must be an initialized git repository.

1. The [pre-flight scanner](#pre-flight-scanner) runs on the directory.
2. You review findings and confirm, or abort.
3. Your git credentials for `github.com` are checked (an SSH probe when the
   protocol is `ssh`; HTTPS is trusted). This runs right before creation so
   a hardware key's touch is close to the real push. The check waits up to
   45 seconds over SSH and 15 over HTTPS; a timeout says the key was
   probably waiting for a touch.
4. The repo is created; Actions and security settings are applied.
5. Every local branch and tag is pushed in a single `git push` from a bare
   clone, so branches other than the checked-out one are included and a
   hardware key needs one touch. The default branch is pushed first and
   then set via the API. A missed key touch is retried once.
6. Branch and tag protection are applied, now that the branch exists.
7. `origin` and upstream tracking are set in your original repo.
8. [Verification](#verification): pushed refs, default branch, and local
   `origin`/tracking.

The default branch is chosen from the local branches in this order:
`init.defaultBranch` if set, then `main`, then `master`, then the checked-out
branch. The plan shows the branch count and the chosen default.

### `copy <src-owner/repo> <owner/repo>`

1. The source is cloned with full history, so truffleHog can walk every
   commit.
2. The [pre-flight scanner](#pre-flight-scanner) runs on the clone.
3. You review findings and confirm, or abort.
4. Git credentials are checked, as above.
5. The repo is created; Actions and security settings are applied.
6. History is mirrored (`git clone --mirror` + `git push --mirror`) and the
   source's default branch is set.
7. Branch and tag protection are applied.
8. [Verification](#verification): the new repo's branches and tags are
   compared with the source's, SHA for SHA.

## Pre-flight scanner

The scanner runs locally and never sends code anywhere. Use it standalone
(`gh-safe-repo scan [path]`) or let `push`/`copy` run it. The
`[pre_flight_scan]` config applies to both.

### What it detects

| Category | Severity | Examples |
|---|---|---|
| Hardcoded secrets | Critical | AWS keys (`AKIA…`), GitHub tokens (`ghp_…`, `github_pat_…`), private keys, database URLs |
| Banned strings | Critical | Literal strings you configure: usernames, internal hostnames, codenames |
| AI context files | Critical | `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `copilot-instructions.md`, `.cursor/`. Their git history may hold more than the current version |
| Email addresses | Warning | Any `user@domain.tld` in the working tree or git history |
| Large files | Warning | Over the configured threshold (default 100 MB) |
| TODO/FIXME comments | Info | `# TODO`, `# FIXME`, `# HACK`, `# XXX` |

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

- Critical findings default to abort; type `y` to continue.
- Warnings only default to continue; type `n` to abort.
- No findings: the workflow continues silently.

Secrets are redacted. When banned strings or AI context files are found, the
scanner prints a ready-to-run `git filter-repo` command to remove them from
history.

### Scanner engine

The engine is chosen in this order:

1. **truffleHog v3 on `PATH`.** A v2 or unrecognised version warns and falls
   through.
2. **podman or docker.** truffleHog runs in a container
   (`ghcr.io/trufflesecurity/trufflehog:latest`) with the scan path mounted
   read-only at the same absolute path, so output paths match a native run.
   `CONTAINER_RUNTIME` overrides the runtime and `TRUFFLEHOG_IMAGE` pins an
   image. A shell wrapper and `Containerfile` for a pinned local image are in
   [`tools/`](tools/README.md).
3. **Regex fallback.** Used when neither is available or truffleHog fails to
   run. The regex pass always runs alongside truffleHog for emails and
   TODOs, and catches lone key-ID patterns that truffleHog skips because it
   wants both halves of a credential pair.

The planned engine is shown in the scan header; the engine that actually ran
is printed after the scan, because truffleHog can still fail mid-run:

```
Running pre-flight security scan... (truffleHog v3.93.4)
  Scan engine: regex (fell back from truffleHog: truffleHog via podman exited with status 125)
```

Set `trufflehog_mode` to pin an engine. `native` and `docker` are strict: if
truffleHog cannot run, `scan` prints its findings and exits `1`, and
`push`/`copy` exits `1` before the repo is created. Only `auto`
falls back to regex; `off` uses regex by choice.

### Coverage

In a git repo only files listed by `git ls-files` are scanned, which is what
gets pushed. Ignored and untracked files are not. Non-git directories are
walked in full. Build-artifact directories (`node_modules`, `__pycache__`,
`.venv`, `venv`, `dist`, `build`) are skipped unless files inside them are
tracked, in which case they are scanned and a warning notes that more is
committed than expected. A file whose first 8 KB contains a NUL byte is
treated as binary and not content-scanned. When stderr is a terminal, a
one-line progress indicator shows the file being scanned.

### Suppressing false positives

`scan_exclude_paths` skips files or directories from every check. Values are
newline- or comma-separated regex patterns matched against the relative
path, and are also passed to truffleHog via `--exclude-paths`.

```ini
[pre_flight_scan]
scan_exclude_paths = docs/api\.github\.com\.json
    tests/fixtures/
```

`exclude_emails` suppresses email findings for addresses or whole domains.
Case-insensitive. Entries starting with `@` match a domain; others must match
the full address. Applies to the working tree and git history.

```ini
[pre_flight_scan]
exclude_emails = action@github.com, noreply@github.com, @example.com
```

## Configuration

No config file is needed; every value has a safe default. The lookup order
is:

1. `--config PATH`
2. `./gh-safe-repo.ini`
3. `$XDG_CONFIG_HOME/gh-safe-repo/gh-safe-repo.ini` (`~/.config` when unset)

`--no-config` skips the lookup and uses built-in defaults only.

[`gh-safe-repo.ini.example`](gh-safe-repo.ini.example) is the canonical,
fully annotated reference, and a test fails if it drifts from the code. Copy
it to get started:

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/gh-safe-repo"
cp gh-safe-repo.ini.example "${XDG_CONFIG_HOME:-$HOME/.config}/gh-safe-repo/gh-safe-repo.ini"
```

Abridged, with defaults:

```ini
[repo]
private = true
delete_branch_on_merge = false
# Not managed unless set:
# has_wiki = false
# has_projects = false
# has_issues = false
# At least one must stay true:
allow_squash_merge = true
allow_merge_commit = true
allow_rebase_merge = true
# Keep the auto-generated README on `new` (ignored by push/copy)
auto_init = false

[actions]
enabled = true
allowed_actions = selected                # all | local_only | selected
github_owned_allowed = true               # only when selected
verified_allowed = true
# patterns_allowed = myorg/*
default_workflow_permissions = read       # read | write
can_approve_pull_request_reviews = false
sha_pinning_required = true
# all_external_contributors | first_time_contributors | first_time_contributors_new_to_github
fork_pr_approval_policy = all_external_contributors

[branch_protection]
protected_branch = master, main           # branches that don't exist are ignored
require_pull_request = true
required_approving_reviews = 1
dismiss_stale_reviews = true
require_conversation_resolution = true
allow_force_pushes = false
allow_deletions = false
enforce_admins = false
use_rulesets = true                       # false = classic per-branch API

[tag_protection]
protected_tags = *                        # comma-separated globs
prevent_tag_deletion = true
prevent_tag_update = true

[security]
enable_dependabot_alerts = true
enable_dependabot_security_updates = true
enable_private_vulnerability_reporting = true
enable_secret_scanning_push_protection = true

[pre_flight_scan]
scan_for_secrets = true
scan_for_emails = true
scan_for_todos = true
scan_email_history = true
max_file_size_mb = 100
warn_ai_context_files = true
trufflehog_mode = auto                    # auto | native | docker | off
# banned_strings = secret, password
# scan_exclude_paths = tests/fixtures/
# exclude_emails = noreply@github.com, @example.com

[git_transport]
# auto       — your own credentials; token-in-URL only when there is no SSH
#              setup and no credential helper (e.g. CI with GITHUB_TOKEN)
# user_creds — never use the API token for git
# token      — always HTTPS with the API token in the URL
mode = auto
```

## GitHub plan limitations

| Feature | Free + Public | Free + Private | Pro/Team + Private |
|---|:---:|:---:|:---:|
| Branch protection / Rulesets | Yes | No | Yes |
| Tag protection (Rulesets) | Yes | No | Yes |
| Dependabot alerts | Yes | No | Yes |
| Dependabot security updates | Yes | No | Yes |
| Secret scanning | Auto | No | Yes |
| Push protection | Yes | No | Yes |
| Private vulnerability reporting | Yes | No | Yes |
| Dependency graph | Auto | No | Yes |

Plan level and visibility are detected at runtime. Unavailable features
appear as `SKIP` with a reason; nothing fails silently.

## Using with an AI agent

Everything is available non-interactively, and
[`skills/gh-safe-repo/SKILL.md`](skills/gh-safe-repo/SKILL.md) teaches a
coding agent the flags, exit codes and failure modes. It does not live in
`.claude/skills/`, so it never auto-loads in this checkout. Either point a
session at the file, or install it once:

```bash
ln -s "$PWD/skills/gh-safe-repo" ~/.claude/skills/gh-safe-repo   # then /gh-safe-repo
```

The recipe it enforces is plan, show the user, then apply:

```bash
gh-safe-repo push . alice/my-repo --dry-run --json   # zero API calls
gh-safe-repo push . alice/my-repo --yes              # apply
gh-safe-repo audit alice/my-repo --json                 # existing repo: exit 3 on drift
```

## How it works

```
gh-safe-repo new <owner/repo>
      │
      ├─ Parse owner/repo; check the owner matches the authenticated user
      ├─ Load config, apply CLI overrides (--public, …)
      ├─ Authenticate (GITHUB_TOKEN, else `gh auth token`)
      ├─ GET /user → login + plan level (one cached call)
      │
      ├─ Build plan: each plugin diffs desired vs. current state
      │   ├─ RepositoryPlugin        repo creation, basic settings
      │   ├─ ActionsPlugin           allowed actions, workflow permissions, SHA pinning
      │   ├─ BranchProtectionPlugin  Rulesets API (classic if use_rulesets = false)
      │   ├─ SecurityPlugin          Dependabot, secret scanning, push protection, PVR
      │   └─ TagProtectionPlugin     immutable tags via Rulesets API
      │
      ├─ Print plan; confirm
      │
      ├─ Apply (unless --dry-run)
      │   ├─ POST /user/repos, PATCH /repos/{owner}/{repo}
      │   ├─ PUT  …/actions/permissions, …/selected-actions, …/workflow, …/fork-pr-contributor-approval
      │   ├─ PUT  …/vulnerability-alerts, …/automated-security-fixes, …/private-vulnerability-reporting
      │   ├─ PATCH /repos/{owner}/{repo}  (security_and_analysis)
      │   ├─ git push (publish) or git push --mirror (mirror)
      │   └─ POST/PATCH …/rulesets  (branch and tag protection, after the push)
      │
      └─ Verify: read every changed setting back
```

Each settings category is a plugin in `gh_safe_repo/plugins/` that fetches
current state, diffs it against config, returns a plan, and applies only real
changes. The creating commands and `fix` share this code path; the only difference is
whether current state comes from an existing repo or from GitHub's defaults.
See [`gh_safe_repo/README.md`](gh_safe_repo/README.md) for the module map.

All API calls go through `gh api` in a subprocess, so authentication stays in
the `gh` CLI. The token comes from `GITHUB_TOKEN` if set (the only credential
needed in CI), otherwise `gh auth token`. It is passed to child processes as
`GH_TOKEN` and never logged.

## Development

```bash
git clone https://github.com/AriESQ/gh-safe-repo
cd gh-safe-repo
uv sync                                        # creates .venv, installs pytest
uv run pytest tests/ -v
./gh-safe-repo new <owner/repo> --dry-run   # run from source
uv tool install .                              # install from current source
```

There are no runtime dependencies; everything is standard library. Do not
add third-party packages without discussion. `pytest` is the only dev
dependency.

CI runs the test suite on every push to `master` and every pull request, on
Python 3.10 and 3.14, with read-only permissions and no token, so the
authenticated and live tiers skip. It sets `trufflehog_mode = off` so scan
tests use the regex engine. Every action is pinned to a commit SHA, as this
repo's own Actions policy requires.

```
gh-safe-repo/
├── gh-safe-repo              # Thin launcher
├── gh_safe_repo/             # Package (see gh_safe_repo/README.md)
│   ├── cli.py                # Subcommand dispatch
│   ├── commands/             # new (create.py), push, copy, audit, fix, scan
│   └── plugins/              # One plugin per settings category
├── skills/gh-safe-repo/      # Agent-facing SKILL.md
├── tools/                    # trufflehog wrapper, history-scrubbing scripts
├── gh-safe-repo.ini.example  # Annotated example config
└── tests/                    # See tests/README.md
```

### Prior art

- [github/safe-settings](https://github.com/github/safe-settings): org-level
  GitHub App. Source of the plugin pattern (one class per category: fetch,
  diff, apply).
- [repository-settings/app](https://github.com/repository-settings/app):
  per-repo variant of the above; a cleaner reference for the base plugin.
- [nicholasgasior/gh-repo-settings](https://github.com/nicholasgasior/gh-repo-settings):
  Go `gh` extension with a plan/apply workflow. Inspiration for the `gh api`
  wrapper and the dry-run plan output.
