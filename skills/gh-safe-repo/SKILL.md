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

Set `NO_COLOR=1` if you want to be sure of clean output; the tool already
drops ANSI codes when stdout is not a terminal.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success — **or** the user declined at an interactive prompt |
| `1` | Operational failure: auth, permissions, API error, or a pre-flight scan that blocked the run |
| `2` | Usage error: bad `owner/repo`, path is not a directory, `--local` with `--from` |

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

`scan --json` emits `{"findings": [...], "summary": {"critical": N, "warning": N,
"info": N}, "skipped_committed_dirs": [...]}`. Each finding has `severity`
(lower-case), `category`, `file_path`, `line_number`, `rule`, `match` (secrets
are `[redacted]`), `commit`, `timestamp`.

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
| Push rejected on `.github/workflows/*` | The API token lacks the `workflow` scope. Set `[git_transport] mode = user_creds` in config to push with the user's own git credentials. |
| Repo created but a later step warned | Creation is not transactional; settings failures are warnings. Re-run `fix <owner/repo> --yes` to converge. |

## Configuration

Every default is overridable via an INI file (`--config PATH`, or discovered
from XDG / the current directory). Do not invent keys — the full annotated list
lives in `gh-safe-repo.ini.example` at the repo root, and the rendered table is
under "Safe defaults in full" in `README.md`. `--config` with no value uses
built-in defaults only, and because its value is optional a bare `--config`
must come last.
