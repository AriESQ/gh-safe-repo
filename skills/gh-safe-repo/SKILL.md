---
name: gh-safe-repo
description: Drive the gh-safe-repo CLI to create a GitHub repository with hardened defaults, audit or fix settings on an existing repo (branch protection, rulesets, Dependabot alerts, secret scanning, push protection, Actions permissions, tag protection), or scan a local directory for secrets before publishing it. Use when asked to create/publish a GitHub repo, push a local project to a new repo, harden or audit repo security settings, or check code for leaked credentials before it goes public.
---

# gh-safe-repo

A CLI that applies safe defaults to GitHub repos. Three commands:

| Command | Target | Needs |
|---|---|---|
| `create <owner/repo>` | a **new** repo | `owner` must equal the authenticated user |
| `fix <owner/repo>` | an **existing** repo | **admin** on the repo (org repos OK) |
| `scan <path>` | a local directory | nothing — no GitHub calls at all |

`fix` is settings-only (it never scans for secrets). `create --local/--from`
always runs a pre-flight secret scan first.

## Operating rules

1. **Always plan first.** Run with `--dry-run --json`, show the user what will
   change, and only then apply. `--dry-run` makes zero API calls.
2. **Always pass `--yes` when applying.** Without a TTY the confirmation
   prompt cannot be answered.
3. **Never target a repo the user did not name.** Do not guess the owner, do
   not "fix" neighbouring repos.
4. `scan` is read-only and safe to run unprompted; `create` and `fix` are not.

```bash
gh-safe-repo create alice/my-repo --local . --dry-run --json   # 1. plan
# show the user the plan, get agreement
gh-safe-repo create alice/my-repo --local . --yes               # 2. apply
```

`--local` pushes **every** local branch and tag, not just the checked-out
one. The default branch is `init.defaultBranch`, else `main`, else `master`,
else the current branch; the plan's `code` entry names it
(`Push N branch(es) from PATH (default: X)`). Check it before `--yes`: any
local branch, including unfinished work, will be published.

Set `NO_COLOR=1` if you want to be sure of clean output; the tool already
drops ANSI codes when stdout is not a terminal.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success — **or** the user declined at an interactive prompt. For `create` and `fix`, everything applied was read back and verified (`unknown` checks warn but still exit `0`) |
| `1` | Operational failure: auth, permissions, API error, a pre-flight scan that blocked the run, or a failed code push on `--local`/`--from` (the repo **was** created; see Failure modes). For `create`, also a failed **fatal** check: the new repo cannot be found, or its visibility is wrong |
| `2` | Usage error: bad `owner/repo`, path is not a directory, `--local` with `--from` |
| `3` | `create` or `fix` applied the changes but at least one setting did not stick when read back (a non-fatal `mismatch` check). For `create` the repo exists and is usable. See Failure modes |
| `130` | Interrupted (SIGINT); nothing after the interrupt point ran |

In a git repo the built-in scan covers tracked files only (`git ls-files`) —
ignored/untracked files are never pushed and are not scanned. A TTY-only
progress line is drawn on stderr; it never appears with `--json` or when
stderr is piped.

`scan` exits `1` when there is at least one CRITICAL finding, `0` otherwise —
so `scan` returning `1` is a *result*, not a crash.

## Reading `--json`

`create`/`fix` `--json` emit the plan on stdout (all prose goes to stderr):

```json
{ "changes": [ {"type": "update", "category": "actions", "key": "default_workflow_permissions",
                "old": "write", "new": "read", "reason": null} ],
  "summary": { "add": 5, "skip": 2 } }
```

`summary` only lists types that occur — use `.get("delete", 0)`.

`--dry-run --json` prints that document straight away. A real `create --json`
or `fix --json` prints it **once, at the end**, after applying and reading the
settings back, with a top-level `verification` block. (Before #77 `create`
printed the plan before applying; parse stdout only after the process exits.)

```json
"verification": {
  "ok": false,
  "checks": [ {"category": "repo", "key": "has_wiki", "status": "mismatch",
               "expected": false, "actual": true, "fatal": false,
               "remediation": "Run `gh-safe-repo fix alice/demo --yes` once more; ..."} ],
  "summary": { "ok": 3, "mismatch": 1, "unknown": 0 }
}
```

`status` is `ok`, `mismatch` (GitHub holds something else → exit `3`, or
`1` when `fatal` is `true`) or `unknown` (could not be read back after
retries; `actual` is `null`; exit stays `0`, even for a fatal check). All
three `summary` keys are always present. The block is absent on `--dry-run`
and when the prompt is declined; `fix` with nothing to change gives
`{"ok": true, "checks": [], ...}`. Trust `verification.ok`, not the absence
of warnings on stderr.

`create` adds checks no audit can make: `repo/repository` (the repo exists)
and `repo/visibility` (both `fatal`), `file/readme` (the auto-generated
README is gone), `repo/description` and `repo/topics` (`--from` copied them),
and `branch_protection/protected_branches` / `tag_protection/protected_tags`
(protection covers the branch and tag patterns).

A `create` that exits `1` **before or during** the apply (repo already
exists, scan blocked, credential pre-flight, failed code push) prints **no**
JSON document: stdout is empty and the reason is on stderr. A `create` that
exits `1` from a fatal check still prints the full document.

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
| Private vulnerability reporting | ✅ | ✅ | ✅ |

A `SKIP` can also simply mean the setting is already correct.

## Failure modes

| Symptom | Cause and fix |
|---|---|
| `Owner 'X' does not match authenticated user 'Y'` | `create` requires you own the namespace. Check which account `gh` is authenticated as; do not silently retry with `Y`. |
| `fix` reports the repo does not exist (404) | Ambiguous: missing repo, or private and invisible to the *active* account. Confirm the active `gh` account before concluding it is gone. |
| `You do not have admin permissions` | `fix` needs admin even to *read* settings. Auditing someone else's public repo is not possible — this is by design. |
| `Repository already exists` | Use `fix` instead of `create`. |
| Pre-flight scan blocked the run (exit 1) | Real secrets were found. Show the findings, let the user remove them; do not re-run with different flags to bypass. |
| `trufflehog_mode = native` (or `docker`) `but truffleHog could not run (...)` (exit 1) | The user pinned an engine and it is missing or failed; `create` stopped before creating anything. Report the reason in the parentheses. Do not edit the config to `auto`/`off` yourself to get past it; that silently weakens the scan. Ask the user to fix the install or choose the fallback. |
| Push rejected on `.github/workflows/*` | The API token lacks the `workflow` scope. Set `[git_transport] mode = user_creds` in config to push with the user's own git credentials. |
| `Code push failed: ...` (exit 1) | The repo exists on GitHub, settings applied, but it is **empty**; branch/tag protection were skipped. Do not re-run `create` (it will fail: repo exists). Fix the cause, then run the recovery commands printed on stderr, ending with `gh-safe-repo fix <owner/repo> --yes`. `Likely cause: your SSH security key did not confirm` means a missed hardware-key touch: ask the user to touch the key and retry the push; it is not a permissions problem. The tool has already retried once (stderr shows `Your SSH security key did not confirm. Touch it when it blinks to retry...`), so this means two touches were missed. |
| `Code pushed, but local repo not fully wired: ...` | The push succeeded; only the local `origin`/upstream setup was skipped. Usual cause: the directory already had an `origin` pointing elsewhere (left untouched on purpose). Report it to the user; do not rewrite their `origin`. The success banner prints the exact `git push -u origin <b>:<b>` to run once it is resolved. |
| `create` or `fix` exits `3`, `✗` rows / `mismatch` checks | The changes were sent but GitHub does not hold them (ignored, or the apply failed with a warning). Run `gh-safe-repo fix <owner/repo> --yes` **once** more (never re-run `create`: the repo exists). If it still exits `3`, stop and report the rows to the user — an org policy or plan limit is overriding the setting; do not loop. For `file/readme`, `repo/description` and `repo/topics`, `fix` does not manage them: run the `remediation` command instead, after showing it to the user. |
| `create` exits `1` with `✗ repo/visibility` (`fatal: true`) | The repo exists but is **public when private was asked for** (or the reverse). Treat it as a possible disclosure: tell the user immediately and show the `gh repo edit ... --visibility ...` remediation; do not push anything else to it first. |
| `create` exits `1` with `✗ repo/repository` (`fatal: true`) | GitHub accepted the create but the repo cannot be found, even after retries. Check the active `gh` account; do not re-run `create` blindly. |
| `create`/`fix` warns `could not be read back`, `?` rows / `unknown` checks | The tool already retried for ~7s; the setting may well be applied. Do **not** retry. Tell the user which settings are unconfirmed so they can check the repo's settings page. |
| Repo created but a later step warned | Creation is not transactional; settings failures are warnings, and verification then reports them as `mismatch` (exit `3`). Re-run `fix <owner/repo> --yes` once to converge. |

## Configuration

Every default is overridable via an INI file (`--config PATH`, or discovered
from XDG / the current directory). Do not invent keys — the full annotated list
lives in `gh-safe-repo.ini.example` at the repo root, and the rendered table is
under "Safe defaults in full" in `README.md`. `--config` with no value uses
built-in defaults only, and because its value is optional a bare `--config`
must come last.
