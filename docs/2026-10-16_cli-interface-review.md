# CLI interface review

A review of the `gh-safe-repo` command-line surface against the twenty
personas in `2026-03-28-user-stories.md`, with a recommended redesign.
Implemented on the `docs/cli-interface-review` branch the same day; section 7 records how.

## 1. The current surface

Three verbs, each with its own option set:

```
gh-safe-repo [--config [PATH]] [--debug] create <owner/repo> [--public] [--local PATH | --from OWNER/REPO] [--dry-run] [--yes] [--json]
gh-safe-repo [--config [PATH]] [--debug] fix    <owner/repo> [--dry-run] [--yes] [--json] [--migrate-branch-protection]
gh-safe-repo [--config [PATH]] [--debug] scan   <path> [--json]
```

Laid out as a matrix, which is how a user has to hold it in their head:

| Option | `create` | `fix` | `scan` |
|---|:-:|:-:|:-:|
| positional | `owner/repo` (must be the authenticated user) | `owner/repo` (any owner, needs admin) | `path` (required) |
| `--public` | ✓ | | |
| `--local PATH` | ✓ (mode switch) | | |
| `--from OWNER/REPO` | ✓ (mode switch, excludes `--local`) | | |
| `--dry-run` | ✓ | ✓ (the only way to audit) | |
| `--yes`, `-y` | ✓ | ✓ | |
| `--json` | plan document | plan document | findings document |
| `--migrate-branch-protection` | | ✓ | |
| `--config [PATH]`, `--debug` | global | global | global |

The verbs are organised by repository lifecycle (make one, repair one, look at
local code) while the options encode orthogonal dimensions: where code comes
from, visibility, whether to mutate, how to print. Because those dimensions do
not line up with the verbs, every cell in the table has to be memorised
separately. That is the "matrix" problem.

## 2. What the personas reveal

1. **`create` is three commands wearing one name.** An empty repo, a local
   project pushed to a new repo, and a copy of an existing repo are three
   different workflows with different pre-flight behaviour (the latter two
   scan for secrets first). `--local` and `--from` are mutually exclusive mode
   switches disguised as options, and they take different value types (a
   filesystem path versus `owner/repo`). The exclusion is enforced inside
   `run()`, not in the parser, so `--help` cannot show it. Story #7 (Lee)
   reads `--from` as a fork and loses the "forked from" link. Story #9 (Rin)
   has to discover that `--local` exists before they can publish a project.

2. **There is no read-only verb for an existing repo.** Stories #4 (weekly
   hygiene), #13 (what did it change?), #14 (CI) and #2 (batch alignment)
   all want "tell me the drift" as a first-class action. Today that is
   `fix --dry-run`, which reads as "pretend to fix", always exits `0`, and
   therefore cannot be used as a CI gate the way `scan` can (it exits `1` on
   findings). Story #4 also wants a one-line "all compliant" result, which
   the dry-run table does not give.

3. **`--json` means a different document per verb.** `create` and `fix` emit
   a plan (and a different shape again on a real run, with a `verification`
   block); `scan` emits findings. This is acceptable, but each verb's help
   should state which document it prints. Story #14 depends on it.

4. **`--config` with an optional value is a footgun.** `gh-safe-repo --config
   scan ./src` parses `scan` as the config path. The README has to carry a
   warning about it. Story #5 (Morgan, two config files) is exactly the user
   who hits this.

5. **`scan` requires a path.** Story #8 (Casey) runs a bare `gh-safe-repo
   scan` inside the project and gets a usage error.

6. **The planned `fix --all`** from the user-stories design (the three-way
   merge and "Control model options") adds another cell to the matrix.
   Whatever shape the surface takes must absorb that flag without creating a
   new special case.

None of these are correctness bugs. They are discoverability and
teachability problems, and they leak into `skills/gh-safe-repo/SKILL.md`,
which currently has to explain the matrix to coding agents row by row.

## 3. Design principles

- **One verb, one intent.** A verb never has a mode switch. If an option
  changes what the command fundamentally does, it should be a verb.
- **Uniform option sets by verb class.** Every mutating verb takes exactly
  `--dry-run`, `--yes`, `--json`. Every creating verb additionally takes
  `--public`. Read-only verbs take only `--json`. The only per-verb option is
  `fix --migrate-branch-protection` (and later `--all`, which lands on both
  `audit` and `fix`).
- **Source before destination** where a verb takes two positionals, matching
  `cp`, `scp` and `git push`. Mis-ordering is caught: a path is not
  `owner/repo`, and the destination owner must match the authenticated user.
- **Read-only verbs exit non-zero on findings**, as `scan` already does, so
  they work as CI gates.
- **Global options take a required value.** No optional-value flags.

## 4. Recommended surface

```
gh-safe-repo [--config PATH | --no-config] [--debug] <command> ...

Create a repo                      (all take --public --dry-run --yes --json)
  create   <owner/repo>                       empty repo with safe defaults
  publish  <path> <owner/repo>                push a local git repo into a new repo (scans first)
  mirror   <src-owner/repo> <owner/repo>      copy an existing repo into a new one (scans first; not a fork)

Existing repo
  audit    <owner/repo> [--json]              read-only drift report; exit 3 if anything differs
  fix      <owner/repo> [--dry-run] [--yes] [--json] [--migrate-branch-protection]

Local code
  scan     [path] [--json]                    secrets scan; path defaults to "."
```

`fix --dry-run` stays. The rule "every mutating verb takes `--dry-run`" is
simpler to teach than an exception, and the output is identical to `audit`.
The difference is intent and exit code: `audit` is the discoverable name and
exits `3` on drift; `fix --dry-run` exits `0` like every other dry run.

The exit code `3` for `audit` is chosen deliberately over `1`. For `create`
and `fix`, `3` already means "GitHub holds something other than the desired
state". `audit` reporting drift is the same statement, made before applying
rather than after. Keeping `1` for operational failures lets a CI job
distinguish "drift found" from "the tool could not run" (story #14).

### Mocked help output

```
$ gh-safe-repo --help
usage: gh-safe-repo [--config PATH | --no-config] [--debug] <command> [options]

Manage safe defaults for GitHub repositories.

create a repo (all take --public, --dry-run, --yes, --json):
  create   <owner/repo>                  Create an empty repo with safe defaults
  publish  <path> <owner/repo>           Push a local git repo into a new repo
  mirror   <src-owner/repo> <owner/repo> Copy an existing repo into a new one (not a fork)

existing repo:
  audit    <owner/repo>                  Report settings drift without changing anything
  fix      <owner/repo>                  Apply safe defaults to an existing repo

local code:
  scan     [path]                        Scan a directory for secrets (no GitHub calls)

options:
  --config PATH   Config file to use (default: discovered from XDG / cwd)
  --no-config     Ignore config files; built-in defaults only
  --debug         Show every API call made

examples:
  gh-safe-repo create alice/app --public
  gh-safe-repo publish . alice/app
  gh-safe-repo mirror upstream/lib alice/lib --public
  gh-safe-repo audit alice/app --json
  gh-safe-repo fix alice/app --yes
  gh-safe-repo scan
```

```
$ gh-safe-repo mirror --help
usage: gh-safe-repo mirror [--public] [--dry-run] [--yes] [--json] SRC-OWNER/REPO OWNER/REPO

Copy an existing repository into a new one. The source is cloned with full
history, scanned for secrets, and mirrored into the new repo. The result is a
new repository, not a GitHub fork: it has no "forked from" link.

positional arguments:
  SRC-OWNER/REPO   Repository to copy (any public repo, or one you can read)
  OWNER/REPO       New repository; OWNER must be the authenticated user

options:
  --public         Create a public repository instead of private
  --dry-run        Print the plan and make no API calls
  --yes, -y        Skip the confirmation prompt (required when stdin is not a terminal)
  --json           Emit the plan as JSON on stdout
```

### Persona check

| Story | Before | After |
|---|---|---|
| #2 batch alignment | loop over `fix --yes` | unchanged; `audit` loop for a report-only pass |
| #4 weekly hygiene | `fix --dry-run`, always exit 0, full table | `audit`, exit 3 on drift, "All settings compliant." when clean |
| #5 two config files | `--config` with optional value | `--config PATH` always takes a value; `--no-config` is explicit |
| #7 seed from upstream | `create X --from Y` looks like a fork | `mirror Y X`; help text says "not a fork" |
| #8 scan without args | usage error | `scan` defaults to `.` |
| #9 push local project | discover `--local` | `publish . rin/new-project` |
| #13 what did fix change | reconstruct from scrollback | `audit --json` before, `fix --json` after; same document shape |
| #14 CI | `fix --dry-run` cannot gate | `audit` gates; `fix --yes` applies |
| future `--all` | `fix --all` | `audit --all` and `fix --all`, same rule on both |

The remaining stories (#1, #3, #6, #10, #11, #12, #15 to #20) are not about
the CLI shape. They are about defaults, the three-way merge, plan-limit
messaging, and archived-repo detection, and are unaffected by this change.

## 5. Alternatives considered

### Creation verbs

- **Keep `create` and rename the flags** to `--source PATH` and
  `--mirror OWNER/REPO`, aligning with `gh repo create --source`. Least
  churn. Rejected because the mode switch remains: `--help` still cannot
  show that the two are exclusive, and the fact that they change pre-flight
  behaviour (a secret scan runs) stays hidden behind an option.
- **A single `--from <PATH | OWNER/REPO>`** with auto-detection by "is this an
  existing directory". Rejected: ambiguous when a directory named
  `owner/repo` exists, which is a common layout for people who clone into
  `~/src/owner/repo`.
- **Noun-verb nesting** (`repo create`, `repo fix`, `code scan`) in the style
  of `gh`. Over-engineering for six verbs.

### Name of the read-only verb

- **`audit`** (recommended). Matches the existing `Auditing owner/repo...`
  line and the README section "Audit and fix a repo you already have".
- **`check`**. Shorter, and pairs with `fix` the way lint/format tools do.
- **`status`**. Reads like `git status` / `terraform plan`: "show me the
  drift". Risks being read as "is the repo up" rather than "is it compliant".

### Names of the creating verbs

- `publish` versus `push` versus `import`. `push` collides with the git verb
  and under-states what happens (a repo is created, settings applied).
  `import` is GitHub's own word for the UI feature but suggests a one-time
  migration. `publish` says "make this project exist on GitHub", which is
  the user's intent in story #9, even when the result is private.
- `mirror` versus `copy` versus `clone`. `clone` is taken by git and implies
  a local result. `copy` is fine but says nothing about history. `mirror`
  is what the implementation does (`git clone --mirror` then push), and the
  word already appears in the README.

## 6. Backward compatibility

None. The old surface is removed outright, with no aliases and no
deprecation period:

- `create --local PATH` and `create --from OWNER/REPO` are deleted. The only
  way to push code is `publish` or `mirror`. Passing the old flags is an
  argparse usage error (exit `2`).
- Bare `--config` (no value) is deleted; `--no-config` replaces it.
- `fix --dry-run` stays because it follows the uniform rule, not for
  compatibility.
- `CHANGELOG.md` records the removals under **Removed**. `SKILL.md` is
  rewritten rather than patched so no old example survives; a stale skill
  actively teaches agents wrong flags.

## 7. Implementation sketch

All command modules already follow the `NAME` / `HELP` /
`add_arguments(parser)` / `run(args)` convention and are registered by
iteration in `gh_safe_repo/cli.py` (`build_parser`). New verbs slot into
that loop.

### `gh_safe_repo/commands/_common.py`

- `add_common_args`: `--config` becomes `metavar="PATH"` with a required
  value; add `--no-config` (`store_true`). Resolve both into the existing
  `args.config == ""` convention in one helper (`resolve_config_arg(args)`)
  so `build_context` and `scan.run` do not change. Reject both together
  (usage error, exit `2`).
- New `add_mutation_args(parser)` adding `--dry-run` and `--yes`/`-y` with
  shared help strings, and `add_create_args(parser)` adding `--public` on
  top. `create.py` and `fix.py` switch to these so the help text is
  identical everywhere.

### `gh_safe_repo/commands/create.py`

- `run(args)` already branches on `args.local_path` / `args.from_repo`. Keep
  it as the single engine; `publish` and `mirror` set those two attributes
  and delegate.
- `add_arguments`: remove `--local` and `--from` entirely, and the
  mutual-exclusion check at the top of `run` (impossible now).
- Error strings that say `--local:` / `--from:` become verb-neutral
  ("source path ...").
- Remove `test_local_and_from_are_mutually_exclusive` in `tests/test_cli.py`
  and the `--local` with `--from` row from the exit-code tables.

### New `gh_safe_repo/commands/publish.py` and `mirror.py`

Thin modules: `NAME`, `HELP`, `add_arguments` (two positionals, source first,
plus create args and common args), and `run(args)` that sets
`args.local_path` or `args.from_repo` from the positionals and delegates to
`create.run(args)`. `mirror`'s help says explicitly: "The result is a new
repository, not a GitHub fork."

### New `gh_safe_repo/commands/audit.py`

- `add_arguments`: `repo` positional plus `--json` (and later `--all`). No
  `--dry-run`, no `--yes`.
- `run(args)`: set `args.dry_run = True`, `args.yes = False`,
  `args.migrate_branch_protection = False`, then call
  `fix.run(args, audit=True)`.
- `fix.run` gains `audit=False`. In the dry-run branch: print
  `All settings compliant.` when there are no actionable changes, else the
  existing table. When `audit` is true, exit `3` if anything is actionable.
  The "Dry run — no changes made." line is printed only for a real
  `--dry-run`.
- The 404 error hint and the verification remediation text already name
  `fix`; leave them.

### `gh_safe_repo/commands/scan.py`

`path` becomes `nargs="?"`, `default="."`.

### `gh_safe_repo/cli.py`

Register `(create, publish, mirror, audit, fix, scan)` in that order. Rewrite
the `description` usage block and the `epilog` examples to the new surface,
one example per verb plus `--no-config`. Group the verbs in `--help` as in
the mock-up above (argparse supports this with one subparsers group per
heading, or a custom formatter).

### Docs, same commit (per "Keeping docs in sync" in `CLAUDE.md`)

- `README.md`: Quick start, Commands (one subsection per verb), CLI
  reference tables (global / creating verbs / `audit` / `fix` / `scan`),
  Exit codes (`audit` exits `3`), and the `--config` prose.
- `skills/gh-safe-repo/SKILL.md`: the three-row command table becomes six
  rows; operating rule 1 becomes "run `audit --json` on an existing repo, or
  `create|publish|mirror ... --dry-run --json` for a new one"; the exit-code
  table gets the `audit` row; every `--local` / `--from` example is replaced.
- `CHANGELOG.md` `[Unreleased]`: Added (`publish`, `mirror`, `audit`,
  `--no-config`, `scan` default path); Removed (`create --local`,
  `create --from`, bare `--config`).
- `gh_safe_repo/README.md`: module map gains the three new modules.
- `docs/MANUAL_TESTING.md`, `tests/README.md`, `tools/README.md`: update
  command examples.

### Tests (`tests/test_cli.py`, mocked subprocess per `tests/README.md`)

- Parser: each new verb parses; `publish` / `mirror` positionals land in
  `local_path` / `from_repo`; `create --local` and `create --from` are usage
  errors (exit `2`); `--config` without a value is a usage error;
  `--no-config` yields `args.config == ""`; `--config X --no-config` exits
  `2`; `scan` with no path uses the current directory.
- Behaviour: `audit` exits `3` with actionable changes and `0` when clean
  (reuse the `fix` mocks); `audit` never prompts and never calls
  `plugin.apply`; `fix --dry-run` still exits `0`.
- `tests/e2e/test_input_validation.py`: add the `publish` / `mirror`
  mis-order cases (a path where `owner/repo` is expected exits `2`).

### Verification after implementing

```bash
uv run pytest tests/ -v
uv run gh-safe-repo --help                        # six verbs, grouped
uv run gh-safe-repo publish --help                # positionals, then the uniform options
uv run gh-safe-repo scan                          # runs on cwd
uv run gh-safe-repo audit AriESQ/<repo>           # exit 3 or 0, no prompt
uv run gh-safe-repo audit AriESQ/<repo> --json | jq .summary
uv run gh-safe-repo create AriESQ/x --local .     # exit 2: unrecognized argument
uv run gh-safe-repo publish . AriESQ/x --dry-run  # the plan
```

Then grep the tree for `--local`, `--from` and `fix --dry-run` to confirm no
stale example remains outside `CHANGELOG.md` and the dated docs.

## 8. Interaction with the three-way merge proposal

`2026-03-28-user-stories.md` proposes that `fix` resolve conflicts between
the tool's desired state and deliberate human changes using setting tiers
(security versus preference) and a `PRESERVE` plan action, with `--all` as
the escape hatch that enforces the full template.

This redesign is orthogonal and makes that proposal cleaner:

- `--all` lands on both `audit` and `fix` with the same meaning, so the
  report and the apply always agree. Under the uniform-options rule it is
  the one additional flag shared by the two existing-repo verbs.
- `audit` is the natural home for the `PRESERVE` rows: a weekly `audit`
  shows what was changed, what was preserved and why, which is exactly what
  story #13 asks for, without a mutating command having to be run to see it.
- `create`, `publish` and `mirror` never see the merge logic (there is no
  "theirs" for a repo that does not exist yet), which is also what the
  proposal states for `create`.
- The `audit` exit code `3` on drift should count `UPDATE` rows only.
  `PRESERVE` rows are the tool choosing not to act and are not drift.

Nothing in the merge proposal needs to change to accommodate this
redesign; the two can be implemented in either order.
