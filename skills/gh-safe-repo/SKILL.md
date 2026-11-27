---
name: gh-safe-repo
description: Drive the gh-safe-repo CLI to create a GitHub repository with hardened defaults, push a local project or copy an existing repo into a new hardened one, audit or fix settings on an existing repo (branch protection, rulesets, Dependabot alerts, secret scanning, push protection, Actions permissions, tag protection, immutable releases), or scan a local directory for secrets before publishing it. Use when asked to create/publish a GitHub repo, push a local project to a new repo, harden or audit repo security settings, or check code for leaked credentials before it goes public.
---

# gh-safe-repo

A CLI that applies safe defaults to GitHub repos. Six commands:

| Command | Target | Changes GitHub? | Needs |
|---|---|:-:|---|
| `new <owner/repo>` | a **new**, empty repo | yes | `owner` must equal the authenticated user |
| `push <path> <owner/repo>` | a **new** repo filled from a local git repo | yes | same, plus git credentials; scans `path` first |
| `copy <src-owner/repo> <owner/repo>` | a **new** repo copied from an existing one (not a fork) | yes | same, plus git credentials; scans the clone first |
| `audit <owner/repo>` | an **existing** repo, read-only | **no** | **admin** on the repo (org repos OK) |
| `fix <owner/repo>` | an **existing** repo | yes | **admin** on the repo (org repos OK) |
| `scan [path]` | a local directory (default `.`) | **no** | nothing — no GitHub calls at all |

Options are uniform by class. `new`, `push` and `copy` take
`--public`, `--dry-run`, `--yes`, `--json`. `fix` takes `--all`, `--dry-run`,
`--yes`, `--json`, `--migrate-branch-protection`. `audit` takes `--all` and
`--json`; `scan` takes only `--json`. Global, on either side of the command:
`--config PATH`, `--no-config`, `--debug`. There is no `--local`, no
`--from`, and `--config` always needs a value.

`gh-safe-repo -v` (or `--version`) prints `gh-safe-repo <version>` and exits
`0`; use it to check which version, and so which contract, you are running.
It is top-level only (`gh-safe-repo audit x/y -v` is a usage error, exit 2)
and never means verbose; that is `--debug`.

Where a command takes two positionals the **source comes first** and the new
repo second (`push . alice/app`, `copy upstream/lib alice/lib`).

`audit` and `fix` are settings-only (they never scan for secrets). `push`
and `copy` always run a pre-flight secret scan first.

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
gh-safe-repo push . alice/my-repo --dry-run --json   # 1. plan (new repo)
# show the user the plan, get agreement
gh-safe-repo push . alice/my-repo --yes              # 2. apply

gh-safe-repo audit alice/my-repo --json                 # 1. plan (existing repo); exit 3 = drift
gh-safe-repo fix alice/my-repo --yes                    # 2. apply
```

`push` pushes **every** local branch and tag, not just the checked-out
one. The default branch is `init.defaultBranch`, else `main`, else `master`,
else the current branch; the plan's `code` entry names it
(`Push N branch(es) from PATH (default: X)`). Check it before `--yes`: any
local branch, including unfinished work, will be pushed. `path` may be a
linked worktree or a submodule checkout; a worktree pushes every branch of
its **main** repository (worktrees share refs), so the branch count can be
higher than the worktree suggests. Uncommitted changes are never pushed.

`copy` creates a fresh repository. It is **not** a GitHub fork and has no
"forked from" link; say so if the user asked to "fork" something. It copies
every branch and tag (plan row `Copy all branches and tags from ...`), not
GitHub's read-only `refs/pull/*`, so pull requests do not come along.

Set `NO_COLOR=1` if you want to be sure of clean output; the tool already
drops ANSI codes when stdout is not a terminal.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success — **or** a person declined at the prompt (`n`, empty line, Ctrl-C). Never for an unanswerable prompt: that is `1`. For `audit`: the repo is compliant. For the mutating commands, everything applied was read back and verified (`unknown` checks and `advisory` local-wiring checks warn but still exit `0`) |
| `1` | Operational failure: auth, permissions, API error, a pre-flight scan that blocked the run, a confirmation prompt that hit end of input without `--yes`, a credential pre-flight failure on `push`/`copy` (including an SSH key that belongs to a different account than the token; nothing was created), or a failed code push on `push`/`copy` (the repo **was** created; see Failure modes). For the creating commands, also a failed **fatal** check: the new repo cannot be found, its visibility is wrong, or (`push`/`copy`) a pushed branch or tag is missing on GitHub or at a different SHA |
| `2` | Usage error: bad `owner/repo`, path is not a directory or not a git repo, the `push` path is a git repo with **no commits** (or no local branch), `--config` with `--no-config`, an unknown flag (including the removed `--local`/`--from`). Nothing was created; stdout is empty even with `--json` |
| `3` | `audit`: at least one setting differs from the safe defaults and `fix` would change it (an `add`/`update`/`delete` row; `preserve` rows never count). Or a mutating command applied the changes but at least one setting did not stick when read back (a non-fatal, non-advisory `mismatch` check, e.g. `repo/default_branch`). For the creating commands the repo exists and is usable. See Failure modes |
| `130` | Interrupted (SIGINT); nothing after the interrupt point ran |

`fix --dry-run` prints the same table as `audit` but always exits `0`, like
every other dry run. Use `audit` when the exit code matters.

In a git repo the built-in scan covers tracked files only (`git ls-files`) —
ignored/untracked files are never pushed and are not scanned. A TTY-only
progress line is drawn on stderr; it never appears with `--json` or when
stderr is piped.

`scan` exits `1` when there is at least one CRITICAL finding, `0` otherwise —
so `scan` returning `1` is a *result*, not a crash.

Human-readable output never contains raw control characters from the repo or
from GitHub. A file name, matched line, branch name or error text holding
ANSI/OSC escapes, CR, backspace, C1 controls or invisible Unicode formatting
(bidi overrides, zero-width, line separators) is printed with visible escapes
(`evil\x1b[2K.txt`, `\x0d`, `\u202e`); undecodable file-name bytes show as
`\xNN`. A path that contains such an escape is not the literal file name: do
not paste it into a command. Read the exact name from `scan --json`
(`file_path`), which is never rewritten. If you see these escapes in a path
or a match, tell the user: the repo contains text meant to manipulate a
terminal.

## Reading `--json`

`audit`, and `new`/`push`/`copy`/`fix` with `--json`, emit the plan on
stdout (all prose goes to stderr):

```json
{ "changes": [ {"type": "update", "category": "actions", "key": "default_workflow_permissions",
                "old": "write", "new": "read", "reason": null,
                "tier": "security", "github_default": "write", "weakened": false},
               {"type": "preserve", "category": "repo", "key": "has_wiki",
                "old": false, "new": true, "reason": "changed from default",
                "tier": "preference", "github_default": true, "weakened": false} ],
  "summary": { "update": 1, "preserve": 1, "skip": 2 },
  "warnings": [] }
```

`summary` only lists types that occur — use `.get("delete", 0)`. For `audit`,
drift means `add`, `update` or `delete` is present; `preserve` and `skip` are
not drift, and the exit code says the same. Every change carries `tier`
(`security` / `preference` / `null`), `github_default` (what a fresh repo
holds, `null` when not a per-key setting) and `weakened` (`true` on an
`update` that restores a security control someone relaxed below GitHub's
default; worth pointing out to the user).

`warnings` is always present (often `[]`). It lists what an Actions-policy
change in the plan would break; it never changes what is applied or the
exit code, so **read it and show it to the user before applying**, even
though the run would succeed:

```json
"warnings": [
  {"kind": "whitelist_pattern_removed", "message": "removes whitelisted action astral-sh/setup-uv",
   "pattern": "astral-sh/setup-uv"},
  {"kind": "actions_blocked", "message": "would block 1 action in 1 workflow",
   "branch": "main", "source": "alice/demo", "workflows_scanned": 2,
   "actions": [ {"uses": "actions/checkout@v4", "kind": "action",
                 "workflow": ".github/workflows/ci.yml", "file": ".github/workflows/ci.yml",
                 "line": 12, "reasons": ["not pinned to a full-length commit SHA"],
                 "pin": "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"} ]}
]
```

- `whitelist_pattern_removed`: the new `patterns_allowed` drops `pattern`
  (the whitelist is replaced, not merged).
- `actions_blocked`: `uses:` references on the default branch's workflows
  (`source`/`branch`) that run today and would be blocked after the change.
  `reasons` says why; `pin`, when present, is the reference pinned to the
  SHA its tag resolves to now. Applying anyway breaks those workflows
  (including, possibly, the CI that runs this tool). Offer the user the
  choices: pin the actions (edit the workflows with `pin`), add the action
  to `patterns_allowed` in their config, or proceed knowingly.
- `actions_unknown`: allowed only if the creator is verified (no API can
  tell). Mention them; do not treat as blocked. (`docker://` images are
  `actions_blocked`: GitHub runs them only under `allowed_actions = all`.)
- An whitelist pattern must include an `@` part to match anything:
  `owner/action@*`, not `owner/action`. When suggesting a
  `patterns_allowed` entry, always write the `@*` form.
- `actions_scan_skipped`: the workflows were not checked (`reason`: `copy
  --dry-run` makes no API calls, or the read failed). For `copy`, the real
  run checks them before its prompt.

`--yes` does not stop on warnings: the change is applied with them.

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
(`copy` copied them), and `branch_protection/protected_branches` /
`tag_protection/protected_tags` (protection covers the branch and tag
patterns).

`push` and `copy` add code checks, read through the REST API (no
`git ls-remote`, so no extra hardware-key touch): `code/branches` and
`code/tags` (both `fatal`; `expected` is the `{name: sha}` that was pushed,
or the `copy` source's refs, and `actual` is GitHub's SHA for each name,
`null` when missing; an annotated tag is compared by its tag object SHA),
and `repo/default_branch` (exit `3`). `push` also adds `local/origin` and
`local/tracking` (`advisory`): your repo's `remote.origin.url` and
`branch.<b>.remote`/`.merge`, read from local git config.

A creating command that exits `1` or `2` **before or during** the apply (repo
already exists, scan blocked, prompt hit end of input, credential pre-flight,
failed code push, a `push` path with no commits, an empty `copy` source)
prints **no** JSON document: stdout is empty and the reason is on stderr. One that exits `1` from a fatal check still prints the
full document.

`scan --json` emits `{"findings": [...], "summary": {"critical": N, "warning": N,
"info": N}, "skipped_committed_dirs": [...], "engine": {...}}`. Each finding has
`severity` (lower-case), `category`, `file_path`, `line_number`, `rule`, `match`
(secrets are `[redacted]`), `commit`, `timestamp`. Values are the raw text
(JSON-escaped, so the document is ASCII); only the remediation prose in an AI
context file finding's `match` embeds the path in its escaped display form.
A secret or banned-string `rule` ending in `(obfuscated with <characters>)`,
for example `AWS Access Key ID (obfuscated with U+200B ZERO WIDTH SPACE)`,
means the value is split by characters a viewer does not show (zero-width,
bidi, ANSI escapes, backspace, NUL). Treat it like the same rule without the
suffix (same severity, `match` still `[redacted]`), and tell the user the key
was hidden on purpose or by a broken tool: removing the invisible characters
does not make the key safe. Match on the rule's prefix, not the whole string.

`engine` is what actually ran: `{"configured": "auto|native|docker|off", "used":
"trufflehog-native|trufflehog-container|regex", "fell_back": bool, "error": string|null}`.
`error` is non-null only when a strict mode (`native`/`docker`) could not run
truffleHog: the scan does not count as clean, even with zero findings.
`fell_back: true` means truffleHog was attempted but failed, so only the regex
scanner checked for secrets. Tell the user when that happens: regex misses
credential types truffleHog detects. `scan` exiting `1` with
`summary.critical == 0` means an explicit mode fell back (see Failure modes).
A truffleHog run that logs `error running scan` counts as failed whatever its
exit status, so it shows up here as `fell_back: true`, never as a clean scan.

`scan` on a git repo with no commits warns on stderr (`is a git repository
with no commits: only files staged with git add were scanned`). The exit code
is unchanged, but "no findings" then says little: tell the user.

## `SKIP` is normal — do not retry it

Most `SKIP` rows mean the feature does not exist on the user's plan/visibility
combination. Explain them; never loop trying to force them.

| Feature | Free + public | Free + private | Paid + private |
|---|:--:|:--:|:--:|
| Branch protection / rulesets | ✅ | ❌ | ✅ |
| Tag protection | ✅ | ❌ | ✅ |
| Dependabot alerts + security updates | ✅ | ❌ | ✅ |
| Secret scanning + push protection | ✅ | ❌ | ✅ |
| Private vulnerability reporting | ✅ | ❌ | ❌ (public only) |
| Immutable releases (`releases/immutable`) | ✅ | ✅ (confirmed live 2026-11-26) | ✅ |

`releases/immutable` is never skipped by plan. Its only `SKIP` besides
"already at desired value" is `Enforced by the repository owner's
organization policy`: compliant, and the tool never writes it. If GitHub
refuses the write, the run warns `Immutable releases setting failed: ...` and
verification reports `releases/immutable` as a mismatch (exit `3`). Report
that to the user; do not retry in a loop.

Once immutable releases are on, a **published** release's assets and tag are
locked for good and its tag name can never be reused, even after deleting
the release or the repo. Title and notes stay editable; drafts are not
locked. When you cut a release on such a repo, create it as a draft, attach
every asset, then publish (`gh release create TAG FILES...` does this when
given files). Never delete and recreate a published release to "fix" it:
the tag name is gone. `[releases] immutable = false` stops gh-safe-repo from
turning it on; nothing in the tool turns it off.

A `SKIP` can also simply mean the setting is already correct. `audit` prints
`All settings compliant.` instead of a table when every row would be a `SKIP`.

## `PRESERVE` is a decision — do not loop on it

`audit` and `fix` compare each setting against GitHub's default as well as
the desired value. A setting that differs from the desired value **and** was
changed from GitHub's default by a person is reported as `PRESERVE` and left
alone: preference settings (`[repo]`, `enforce_admins`,
`require_conversation_resolution`, Actions `enabled`) always, security
settings when the current value is *stricter* than the desired one (e.g.
`required_approving_reviews` 3 kept over 1). The row's note says which:
`(changed from default)` or `(stricter than 1)`.

- Re-running `fix` will never change a `PRESERVE` row. Do not retry or
  "fix harder". Report it to the user as a deliberate choice that was kept.
- A security setting that is *weaker* than GitHub's default is not
  preserved: it is an `UPDATE` marked `⚠ weakened from default`
  (`"weakened": true`). Tell the user someone relaxed that control.
- To enforce a preserved setting the user has two options, both their
  call, not yours: declare the section (e.g. `[repo]`) in their config
  file, which enforces every setting in that section, or pass `--all` to
  `audit`/`fix`, which treats every difference as a change. Never add
  `--all` on your own to make a `PRESERVE` row go away.
- A `SKIP` row reading `Not applicable while allowed_actions is 'local_only'
  (preserved)` is a sub-setting whose parent was preserved; it is inert, not
  drift. Do not try to apply it separately.
- `new`, `push` and `copy` never produce `PRESERVE` rows.
- A `SKIP` row reading `GitHub Actions is disabled for this repository`
  (on `allowed_actions` and `sha_pinning_required`) means Actions is off
  entirely. `fix` will not turn it back on. Do not suggest enabling Actions
  just to clear the row.

## Failure modes

| Symptom | Cause and fix |
|---|---|
| `unrecognized arguments: --local ...` or `--from ...` (exit 2) | Those flags were removed. Use `push <path> <owner/repo>` or `copy <src-owner/repo> <owner/repo>`. |
| `push`: `Source path '...' is a git repository with no commits, so there is nothing to push. Nothing was created.` (exit 2, also under `--dry-run`) | The directory was `git init`-ed but never committed (untracked or staged files do not count). Nothing was scanned, no API call was made, no repo exists. Do **not** switch to `new` on your own: `new` creates the GitHub repo but does not wire the local directory. Stderr prints both routes; pick with the user. To keep the directory: `git -C <path> add -A`, `git -C <path> commit -m "Initial commit"` (`--allow-empty` if there are no files), then the same `push` command again. Ask before committing on the user's behalf. Or: `gh-safe-repo new <owner/repo> --yes`, then `gh repo clone <owner/repo>`. |
| `push`: `Source path '...' has commits but no local branch (detached HEAD)` (exit 2) | Same early refusal; nothing was created. `git -C <path> switch -c <branch>`, then re-run `push`. |
| `copy`: `Source repo '...' has no branches (it is empty), so there is nothing to copy. Nothing was created.` (exit 1) | The source repository is empty. Nothing was scanned or created. Use `new <owner/repo>` if an empty repo is what the user wants. `copy --dry-run` makes no API calls and does not detect this. |
| `Warning: No branch was pushed: ... The new repository is empty.` followed by `✗ code/branches` (exit 1) | The source lost its commits after the early check, or git could not answer it. The repo exists; do not re-run the creating command. Follow the printed `remediation`: commit first, push that branch, then `gh-safe-repo fix <owner/repo> --yes`. |
| `argument --config: expected one argument` (exit 2) | `--config` needs a path. To ignore config files, pass `--no-config`. |
| `Owner 'X' does not match authenticated user 'Y'` (exit 1) | The creating commands require you own the namespace. Check which account `gh` is authenticated as; do not silently retry with `Y`. If `X` is another account logged in to `gh`, the error suggests `gh auth switch -u X` (changes the user's active account for every tool: ask first) or the one-run `GH_TOKEN=$(gh auth token --user X) gh-safe-repo ...` (changes nothing; prefer it). A `GITHUB_TOKEN` already set in the environment wins over both. |
| `push`/`copy`: `Your SSH key authenticates to github.com as 'A', but the API token is for 'B'` (exit 1, nothing created) | The SSH key the push would use belongs to a different account than the token. Re-running will fail the same way. Do not change the user's SSH or git config yourself; show them the listed fixes (per-directory `core.sshCommand` via `includeIf`, a one-run `GIT_SSH_COMMAND='ssh -i <key> -o IdentitiesOnly=yes'`, `IdentityFile` under `Host github.com` in `~/.ssh/config`, or HTTPS) and ask which key belongs to `B`. If `A` is the intended owner, the fix is the token instead (see the owner-mismatch row). `... is a deploy key for owner/repo` is the same failure: a repo-scoped key cannot push to a new repo. |
| `Warning: could not tell which GitHub account your SSH key authenticates as (...)` | Not a failure. The account check was skipped (a custom `GIT_SSH` program or an unexpected `ssh -T` answer) and the plain access check ran instead. The run continues; mention to the user that the key's account was not verified. |
| `audit`/`fix` header reads `Auditing <owner>/<repo> as <user>...` | Not an error: the authenticated account `<user>` is not the repo's owner (an org, or a repo it collaborates on). If the user expected a different account, stop and confirm before running `fix`. With `--json` the line is on stderr; the JSON is unchanged. |
| `audit`/`fix` reports the repo does not exist (404) | Ambiguous: missing repo, or private and invisible to the *active* account. Confirm the active `gh` account before concluding it is gone. |
| `You do not have admin permissions` | `audit` and `fix` need admin even to *read* settings. Auditing someone else's public repo is not possible — this is by design. |
| `Repository already exists` | Use `audit`/`fix` instead of a creating command. |
| `--yes is required for unattended runs` (exit 1) | You ran a mutating command without `--yes` and stdin was closed, so the confirmation prompt got no answer. Nothing was created or changed. If the user has agreed to the plan, re-run the same command with `--yes`; otherwise show them the plan first. |
| Pre-flight scan blocked the run (exit 1) | Real secrets were found. Show the findings, let the user remove them; do not re-run with different flags to bypass. |
| `trufflehog_mode = native` (or `docker`) `but truffleHog could not run (...)` (exit 1) | The user pinned an engine and it is missing or failed; `push`/`copy` stopped before creating anything. `scan` still lists the regex findings and prints `this scan is incomplete` instead of `No issues found.`; do not report that as clean. Report the reason in the parentheses. Do not edit the config to `auto`/`off` yourself to get past it; that silently weakens the scan. Ask the user to fix the install or choose the fallback. |
| `ssh -T git@github.com timed out after 45s` or `git ls-remote ... timed out after 45s` (exit 1, nothing created) | Over SSH this is usually a hardware security key waiting for a touch. Ask the user to be ready at the key, then re-run the same command; it is safe because nothing was created. |
| `Permission denied (publickey)` with `Likely cause: SSH settings are read from the git config of <dir>` (exit 1) | The user's key is chosen per directory and did not apply where the tool ran. For `copy`, re-run from inside the directory where their key setting applies (e.g. their checkout tree); for `push`, the PATH's own git config is used. Do not change their SSH or git config. |
| Push rejected on `.github/workflows/*` | The API token lacks the `workflow` scope. Set `[git_transport] mode = user_creds` in config to push with the user's own git credentials. |
| `Code push failed: ...` (exit 1) | The repo exists on GitHub, settings applied, but the push did not complete: it may be empty or hold only some branches and tags; branch/tag protection were skipped. Do not re-run the creating command (it will fail: repo exists). Fix the cause, then run the recovery commands printed on stderr, ending with `gh-safe-repo fix <owner/repo> --yes`. For `copy` they are `git clone --bare <src>` and one `git push <dest> 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'`; never substitute `git clone --mirror` / `git push --mirror`, which GitHub rejects on any repo with pull requests (`deny updating a hidden ref` on `refs/pull/*`). `Likely cause: your SSH security key did not confirm` means a missed hardware-key touch: ask the user to touch the key and retry the push; it is not a permissions problem. The tool has already retried once (stderr shows `Your SSH security key did not confirm. Touch it when it blinks to retry...`), so this means two touches were missed. |
| `push`/`copy` exits `1` with `✗ code/branches` or `✗ code/tags` (`fatal: true`) | The push reported success but GitHub does not have every pushed ref at the pushed SHA, even after ~7s of retries. The repo exists; do not re-run the creating command. Show the user which names differ (`expected` vs `actual`; `null` = missing), then run the printed `remediation` (re-push from the local repo with explicit refspecs, or for `copy` re-run the bare clone and explicit-refspec push), then `gh-safe-repo fix <owner/repo> --yes` for branch/tag protection. A differing SHA (not `null`) means GitHub holds other commits: ask the user before any force push. |
| `push`/`copy` exits `3` with `✗ repo/default_branch` | The code is all there but GitHub's default branch is not the one chosen. Show the user the `gh repo edit <owner/repo> --default-branch <b>` remediation and run it once they agree; `fix` does not manage it. |
| `Code pushed, but local repo not fully wired: ...`, or `✗ local/origin` / `✗ local/tracking` (`advisory: true`, exit `0`) | The push succeeded and GitHub is fine; only the user's local `origin`/upstream is not pointing at the new repo. Usual cause: the directory already had an `origin` pointing elsewhere (left untouched on purpose; the check's remediation says so). Report it to the user; do not rewrite their `origin` yourself. The remediation and the banner give the exact `git remote add`/`set-url origin <url>` and `git push -u origin <b>:<b>` commands. Not a failure: do not retry over it. |
| `audit` exits `3` | Not a failure: the repo has drift. Show the table (or `--json` document) to the user and offer `fix <owner/repo> --yes`. |
| `audit` exits `0` but prints a table of `PRESERVE` rows | Not drift. Those settings differ from the defaults because a person chose them; `fix` would leave them alone too. Report them; do not pass `--all` unless the user asks for full enforcement. |
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
