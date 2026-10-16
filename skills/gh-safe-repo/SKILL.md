---
name: gh-safe-repo
description: Drive the gh-safe-repo CLI to create a GitHub repository with hardened defaults, publish a local project or mirror an existing repo into a new hardened one, audit or fix settings on an existing repo (branch protection, rulesets, Dependabot alerts, secret scanning, push protection, Actions permissions, tag protection), or scan a local directory for secrets before publishing it. Use when asked to create/publish a GitHub repo, push a local project to a new repo, harden or audit repo security settings, or check code for leaked credentials before it goes public.
---

# gh-safe-repo

A CLI that applies safe defaults to GitHub repos. Six commands:

| Command | Target | Changes GitHub? | Needs |
|---|---|:-:|---|
| `create <owner/repo>` | a **new**, empty repo | yes | `owner` must equal the authenticated user |
| `publish <path> <owner/repo>` | a **new** repo filled from a local git repo | yes | same, plus git credentials; scans `path` first |
| `mirror <src-owner/repo> <owner/repo>` | a **new** repo copied from an existing one (not a fork) | yes | same, plus git credentials; scans the clone first |
| `audit <owner/repo>` | an **existing** repo, read-only | **no** | **admin** on the repo (org repos OK) |
| `fix <owner/repo>` | an **existing** repo | yes | **admin** on the repo (org repos OK) |
| `scan [path]` | a local directory (default `.`) | **no** | nothing — no GitHub calls at all |

Options are uniform by class. `create`, `publish` and `mirror` take
`--public`, `--dry-run`, `--yes`, `--json`. `fix` takes `--dry-run`, `--yes`,
`--json`, `--migrate-branch-protection`. `audit` and `scan` take only
`--json`. Global, on either side of the command: `--config PATH`,
`--no-config`, `--debug`. There is no `--local`, no `--from`, and `--config`
always needs a value.

Where a command takes two positionals the **source comes first** and the new
repo second (`publish . alice/app`, `mirror upstream/lib alice/lib`).

`audit` and `fix` are settings-only (they never scan for secrets). `publish`
and `mirror` always run a pre-flight secret scan first.

## Operating rules

1. **Always plan first.** For an existing repo run `audit <owner/repo> --json`.
   For a new repo run the creating command with `--dry-run --json`. Both make
   zero API writes (`--dry-run` makes zero API calls at all). Show the user
   what will change, and only then apply.
2. **Always pass `--yes` when applying.** Without a TTY the confirmation
   prompt cannot be answered: with stdin closed, the mutating commands print
   `--yes is required for unattended runs` and exit `1` with nothing done.
   Do not pipe `y` in instead; it works, but `--yes` is the contract.
3. **Never target a repo the user did not name.** Do not guess the owner, do
   not "fix" neighbouring repos.
4. `audit` and `scan` are read-only and safe to run unprompted; the other
   four are not.

```bash
gh-safe-repo publish . alice/my-repo --dry-run --json   # 1. plan (new repo)
# show the user the plan, get agreement
gh-safe-repo publish . alice/my-repo --yes              # 2. apply

gh-safe-repo audit alice/my-repo --json                 # 1. plan (existing repo); exit 3 = drift
gh-safe-repo fix alice/my-repo --yes                    # 2. apply
```

`publish` pushes **every** local branch and tag, not just the checked-out
one. The default branch is `init.defaultBranch`, else `main`, else `master`,
else the current branch; the plan's `code` entry names it
(`Push N branch(es) from PATH (default: X)`). Check it before `--yes`: any
local branch, including unfinished work, will be published.

`mirror` creates a fresh repository. It is **not** a GitHub fork and has no
"forked from" link; say so if the user asked to "fork" something.

Set `NO_COLOR=1` if you want to be sure of clean output; the tool already
drops ANSI codes when stdout is not a terminal.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success — **or** a person declined at the prompt (`n`, empty line, Ctrl-C). Never for an unanswerable prompt: that is `1`. For `audit`: the repo is compliant. For the mutating commands, everything applied was read back and verified (`unknown` checks and `advisory` local-wiring checks warn but still exit `0`) |
| `1` | Operational failure: auth, permissions, API error, a pre-flight scan that blocked the run, a confirmation prompt that hit end of input without `--yes`, or a failed code push on `publish`/`mirror` (the repo **was** created; see Failure modes). For the creating commands, also a failed **fatal** check: the new repo cannot be found, its visibility is wrong, or (`publish`/`mirror`) a pushed branch or tag is missing on GitHub or at a different SHA |
| `2` | Usage error: bad `owner/repo`, path is not a directory, `--config` with `--no-config`, an unknown flag (including the removed `--local`/`--from`) |
| `3` | `audit`: at least one setting differs from the safe defaults; `fix` would change it. Or a mutating command applied the changes but at least one setting did not stick when read back (a non-fatal, non-advisory `mismatch` check, e.g. `repo/default_branch`). For the creating commands the repo exists and is usable. See Failure modes |
| `130` | Interrupted (SIGINT); nothing after the interrupt point ran |

`fix --dry-run` prints the same table as `audit` but always exits `0`, like
every other dry run. Use `audit` when the exit code matters.

In a git repo the built-in scan covers tracked files only (`git ls-files`) —
ignored/untracked files are never pushed and are not scanned. A TTY-only
progress line is drawn on stderr; it never appears with `--json` or when
stderr is piped.

`scan` exits `1` when there is at least one CRITICAL finding, `0` otherwise —
so `scan` returning `1` is a *result*, not a crash.

## Reading `--json`

`audit`, and `create`/`publish`/`mirror`/`fix` with `--json`, emit the plan on
stdout (all prose goes to stderr):

```json
{ "changes": [ {"type": "update", "category": "actions", "key": "default_workflow_permissions",
                "old": "write", "new": "read", "reason": null} ],
  "summary": { "add": 5, "skip": 2 } }
```

`summary` only lists types that occur — use `.get("delete", 0)`. For `audit`,
drift means any key other than `skip` is present; the exit code says the same.

`audit --json` and `--dry-run --json` print that document straight away. A
real mutating run prints it **once, at the end**, after applying and reading
the settings back, with a top-level `verification` block. Parse stdout only
after the process exits.

```json
"verification": {
  "ok": false,
  "checks": [ {"category": "repo", "key": "has_wiki", "status": "mismatch",
               "expected": false, "actual": true, "fatal": false, "advisory": false,
               "remediation": "Run `gh-safe-repo fix alice/demo --yes` once more; ..."} ],
  "summary": { "ok": 3, "mismatch": 1, "unknown": 0 }
}
```

`status` is `ok`, `mismatch` (GitHub holds something else → exit `3`, or
`1` when `fatal` is `true`, unchanged when `advisory` is `true`) or `unknown`
(could not be read back after retries; `actual` is `null`; exit stays `0`,
even for a fatal check). An `advisory` mismatch still makes
`verification.ok` false and counts in `summary.mismatch`. All
three `summary` keys are always present. The block is absent from `audit`,
from `--dry-run`, and when the prompt is declined; `fix` with nothing to
change gives `{"ok": true, "checks": [], ...}`. Trust `verification.ok`, not
the absence of warnings on stderr.

The creating commands add checks no audit can make: `repo/repository` (the
repo exists) and `repo/visibility` (both `fatal`), `file/readme` (the
auto-generated README is gone), `repo/description` and `repo/topics`
(`mirror` copied them), and `branch_protection/protected_branches` /
`tag_protection/protected_tags` (protection covers the branch and tag
patterns).

`publish` and `mirror` add code checks, read through the REST API (no
`git ls-remote`, so no extra hardware-key touch): `code/branches` and
`code/tags` (both `fatal`; `expected` is the `{name: sha}` that was pushed,
or the `mirror` source's refs, and `actual` is GitHub's SHA for each name,
`null` when missing; an annotated tag is compared by its tag object SHA),
and `repo/default_branch` (exit `3`). `publish` also adds `local/origin` and
`local/tracking` (`advisory`): your repo's `remote.origin.url` and
`branch.<b>.remote`/`.merge`, read from local git config.

A creating command that exits `1` **before or during** the apply (repo
already exists, scan blocked, prompt hit end of input, credential pre-flight,
failed code push) prints **no** JSON document: stdout is empty and the
reason is on stderr. One that exits `1` from a fatal check still prints the
full document.

`scan --json` emits `{"findings": [...], "summary": {"critical": N, "warning": N,
"info": N}, "skipped_committed_dirs": [...], "engine": {...}}`. Each finding has
`severity` (lower-case), `category`, `file_path`, `line_number`, `rule`, `match`
(secrets are `[redacted]`), `commit`, `timestamp`.

`engine` is what actually ran: `{"configured": "auto|native|docker|off", "used":
"trufflehog-native|trufflehog-container|regex", "fell_back": bool}`.
`fell_back: true` means truffleHog was attempted but failed, so only the regex
scanner checked for secrets. Tell the user when that happens: regex misses
credential types truffleHog detects. `scan` exiting `1` with
`summary.critical == 0` means an explicit mode fell back (see Failure modes).

## `SKIP` is normal — do not retry it

Most `SKIP` rows mean the feature does not exist on the user's plan/visibility
combination. Explain them; never loop trying to force them.

| Feature | Free + public | Free + private | Paid + private |
|---|:--:|:--:|:--:|
| Branch protection / rulesets | ✅ | ❌ | ✅ |
| Tag protection | ✅ | ❌ | ✅ |
| Dependabot alerts + security updates | ✅ | ❌ | ✅ |
| Secret scanning + push protection | ✅ | ❌ | ✅ |
| Private vulnerability reporting | ✅ | ❌ | ✅ |

A `SKIP` can also simply mean the setting is already correct. `audit` prints
`All settings compliant.` instead of a table when every row would be a `SKIP`.

## Failure modes

| Symptom | Cause and fix |
|---|---|
| `unrecognized arguments: --local ...` or `--from ...` (exit 2) | Those flags were removed. Use `publish <path> <owner/repo>` or `mirror <src-owner/repo> <owner/repo>`. |
| `argument --config: expected one argument` (exit 2) | `--config` needs a path. To ignore config files, pass `--no-config`. |
| `Owner 'X' does not match authenticated user 'Y'` | The creating commands require you own the namespace. Check which account `gh` is authenticated as; do not silently retry with `Y`. |
| `audit`/`fix` reports the repo does not exist (404) | Ambiguous: missing repo, or private and invisible to the *active* account. Confirm the active `gh` account before concluding it is gone. |
| `You do not have admin permissions` | `audit` and `fix` need admin even to *read* settings. Auditing someone else's public repo is not possible — this is by design. |
| `Repository already exists` | Use `audit`/`fix` instead of a creating command. |
| `--yes is required for unattended runs` (exit 1) | You ran a mutating command without `--yes` and stdin was closed, so the confirmation prompt got no answer. Nothing was created or changed. If the user has agreed to the plan, re-run the same command with `--yes`; otherwise show them the plan first. |
| Pre-flight scan blocked the run (exit 1) | Real secrets were found. Show the findings, let the user remove them; do not re-run with different flags to bypass. |
| `trufflehog_mode = native` (or `docker`) `but truffleHog could not run (...)` (exit 1) | The user pinned an engine and it is missing or failed; `publish`/`mirror` stopped before creating anything. Report the reason in the parentheses. Do not edit the config to `auto`/`off` yourself to get past it; that silently weakens the scan. Ask the user to fix the install or choose the fallback. |
| `git ls-remote ... timed out after 45s` (exit 1, nothing created) | Over SSH this is usually a hardware security key waiting for a touch. Ask the user to be ready at the key, then re-run the same command; it is safe because nothing was created. |
| `Permission denied (publickey)` with `Likely cause: SSH settings are read from the git config of <dir>` (exit 1) | The user's key is chosen per directory and did not apply where the tool ran. For `mirror`, re-run from inside the directory where their key setting applies (e.g. their checkout tree); for `publish`, the PATH's own git config is used. Do not change their SSH or git config. |
| Push rejected on `.github/workflows/*` | The API token lacks the `workflow` scope. Set `[git_transport] mode = user_creds` in config to push with the user's own git credentials. |
| `Code push failed: ...` (exit 1) | The repo exists on GitHub, settings applied, but it is **empty**; branch/tag protection were skipped. Do not re-run the creating command (it will fail: repo exists). Fix the cause, then run the recovery commands printed on stderr, ending with `gh-safe-repo fix <owner/repo> --yes`. `Likely cause: your SSH security key did not confirm` means a missed hardware-key touch: ask the user to touch the key and retry the push; it is not a permissions problem. The tool has already retried once (stderr shows `Your SSH security key did not confirm. Touch it when it blinks to retry...`), so this means two touches were missed. |
| `publish`/`mirror` exits `1` with `✗ code/branches` or `✗ code/tags` (`fatal: true`) | The push reported success but GitHub does not have every pushed ref at the pushed SHA, even after ~7s of retries. The repo exists; do not re-run the creating command. Show the user which names differ (`expected` vs `actual`; `null` = missing), then run the printed `remediation` (re-push from the local repo with explicit refspecs, or re-mirror), then `gh-safe-repo fix <owner/repo> --yes` for branch/tag protection. A differing SHA (not `null`) means GitHub holds other commits: ask the user before any force push. |
| `publish`/`mirror` exits `3` with `✗ repo/default_branch` | The code is all there but GitHub's default branch is not the one chosen. Show the user the `gh repo edit <owner/repo> --default-branch <b>` remediation and run it once they agree; `fix` does not manage it. |
| `Code pushed, but local repo not fully wired: ...`, or `✗ local/origin` / `✗ local/tracking` (`advisory: true`, exit `0`) | The push succeeded and GitHub is fine; only the user's local `origin`/upstream is not pointing at the new repo. Usual cause: the directory already had an `origin` pointing elsewhere (left untouched on purpose; the check's remediation says so). Report it to the user; do not rewrite their `origin` yourself. The remediation and the banner give the exact `git remote add`/`set-url origin <url>` and `git push -u origin <b>:<b>` commands. Not a failure: do not retry over it. |
| `audit` exits `3` | Not a failure: the repo has drift. Show the table (or `--json` document) to the user and offer `fix <owner/repo> --yes`. |
| A mutating command exits `3`, `✗` rows / `mismatch` checks | The changes were sent but GitHub does not hold them (ignored, or the apply failed with a warning). Run `gh-safe-repo fix <owner/repo> --yes` **once** more (never re-run a creating command: the repo exists). If it still exits `3`, stop and report the rows to the user — an org policy or plan limit is overriding the setting; do not loop. For `file/readme`, `repo/description` and `repo/topics`, `fix` does not manage them: run the `remediation` command instead, after showing it to the user. |
| A creating command exits `1` with `✗ repo/visibility` (`fatal: true`) | The repo exists but is **public when private was asked for** (or the reverse). Treat it as a possible disclosure: tell the user immediately and show the `gh repo edit ... --visibility ...` remediation; do not push anything else to it first. |
| A creating command exits `1` with `✗ repo/repository` (`fatal: true`) | GitHub accepted the create but the repo cannot be found, even after retries. Check the active `gh` account; do not re-run blindly. |
| `could not be read back`, `?` rows / `unknown` checks | The tool already retried for ~7s; the setting may well be applied. Do **not** retry. Tell the user which settings are unconfirmed so they can check the repo's settings page. |
| Repo created but a later step warned | Creation is not transactional; settings failures are warnings, and verification then reports them as `mismatch` (exit `3`). Re-run `fix <owner/repo> --yes` once to converge. |

## Configuration

Every default is overridable via an INI file (`--config PATH`, or discovered
from XDG / the current directory). Do not invent keys — the full annotated list
lives in `gh-safe-repo.ini.example` at the repo root, and the rendered table is
under "Safe defaults" in `README.md`. `--no-config` ignores config files and
uses built-in defaults only; it cannot be combined with `--config`.
