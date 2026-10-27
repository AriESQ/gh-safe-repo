# Actions whitelist enforcement: live verification (#111)

**Date observed:** 2026-10-26 (local; runs dispatched from 2026-10-27T03:48Z)
**Issue:** [#111](https://github.com/AriESQ/gh-safe-repo/issues/111). PR [#117](https://github.com/AriESQ/gh-safe-repo/pull/117)

`gh_safe_repo/actions_usage.py` predicts which `uses:` references a new
Actions policy would block. Those predictions rested on assumptions about
how GitHub matches the whitelist. This records a test that checked them
against what GitHub actually lets run.
LEARNINGS ("Actions in Use…") has the summary.

## Method

- **Repos:** two throwaway public repos owned by one user account (`<owner>`), deleted afterwards:
  - `<owner>/<scratch>` holds the workflows under test.
  - `<owner>/<scratch-lib>` is a second repo with the same owner. It holds a composite action (`action.yml` at the root) and a reusable workflow (`.github/workflows/reuse.yml`).
- **Cases:** nine `workflow_dispatch` workflows, **one file per case**. A disallowed action fails the whole run at startup, so separate files keep the cases independent.
- **Each round:**
  1. Reset to the permissive policy (`all`, no pinning).
  2. Record the tool's prediction with `gh-safe-repo audit --all --config <round.ini> --json`, run from the #111 branch. Its `warnings` list blocked and unknown references.
  3. Apply the round's policy with `PUT …/actions/permissions` and `PUT …/actions/permissions/selected-actions`.
  4. Dispatch all nine workflows and wait for each to finish.
- **How results were judged:** a blocked run ends `conclusion: startup_failure` with no jobs, no check runs and no logs. The REST API exposes no reason, so the reason was read from each run's public web page. Every `startup_failure` below carried GitHub's "is not allowed" message; none failed for any other reason.

The fixture files were pushed with git. Writing `.github/workflows/*`
through the Contents API needs an OAuth token with the `workflow` scope;
without it the API answers `404`.

### Cases

| Case | `uses:` |
|---|---|
| c01 | `<owner>/<scratch-lib>@master` (same owner, other repo, tag) |
| c02 | `astral-sh/setup-uv@v5` |
| c03 | `gradle/actions/setup-gradle@v4` (action in a subdirectory) |
| c04 | `actions/checkout@v4` |
| c05 | `actions/checkout@11d5960a326750d5838078e36cf38b85af677262` (v4, pinned) |
| c06 | `docker://alpine:3.20` |
| c07 | job-level `<owner>/<scratch-lib>/.github/workflows/reuse.yml@master` (same-owner reusable workflow, tag) |
| c08 | `actions/checkout@<sha>` then `./.github/actions/local` |
| c09 | `hashicorp/setup-terraform@v3` |

### Rounds

| Round | `allowed_actions` | `sha_pinning_required` | `github_owned_allowed` | `verified_allowed` | `patterns_allowed` |
|---|---|---|---|---|---|
| R0 | `all` | false | — | — | — |
| R1 | `selected` | false | true | false | `astral-sh/setup-uv`, `gradle/*` |
| R2 | `selected` | **true** | true | false | `astral-sh/setup-uv`, `gradle/*` |
| R3 | `selected` | false | true | false | `!actions/checkout@*` |
| R4 | `local_only` | false | — | — | — |
| R5 | `selected` | false | true | **true** | (none) |

The API accepted every policy write (`204`), including the `!` (blacklist) pattern.

## Results

✅ = ran, ⛔ = blocked by policy. "Old" is what the PR's first version
predicted; "fixed" is after the corrections below. `?` = unknown
(verified-creator status, which no API can check).

| | c01 | c02 | c03 | c04 | c05 | c06 | c07 | c08 | c09 |
|---|---|---|---|---|---|---|---|---|---|
| **R0** GitHub | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| old / fixed | ✅/✅ | ✅/✅ | ✅/✅ | ✅/✅ | ✅/✅ | ✅/✅ | ✅/✅ | ✅/✅ | ✅/✅ |
| **R1** GitHub | ✅ | ⛔ | ✅ | ✅ | ✅ | ⛔ | ✅ | ✅ | ⛔ |
| old / fixed | ✅/✅ | **✅**/⛔ | ✅/✅ | ✅/✅ | ✅/✅ | **?**/⛔ | ✅/✅ | ✅/✅ | ⛔/⛔ |
| **R2** GitHub | ⛔ | ⛔ | ⛔ | ⛔ | ✅ | ⛔ | ✅ | ✅ | ⛔ |
| old / fixed | ⛔/⛔ | ⛔/⛔ | ⛔/⛔ | ⛔/⛔ | ✅/✅ | **?**/⛔ | ✅/✅ | ✅/✅ | ⛔/⛔ |
| **R3** GitHub | ✅ | ⛔ | ⛔ | ⛔ | ⛔ | ⛔ | ✅ | ⛔ | ⛔ |
| old / fixed | ✅/✅ | ⛔/⛔ | ⛔/⛔ | ⛔/⛔ | ⛔/⛔ | **?**/⛔ | ✅/✅ | ⛔/⛔ | ⛔/⛔ |
| **R4** GitHub | ✅ | ⛔ | ⛔ | ⛔ | ⛔ | ⛔ | ✅ | ⛔ | ⛔ |
| old / fixed | ✅/✅ | ⛔/⛔ | ⛔/⛔ | ⛔/⛔ | ⛔/⛔ | **?**/⛔ | ✅/✅ | ⛔/⛔ | ⛔/⛔ |
| **R5** GitHub | ✅ | ✅ | ✅ | ✅ | ✅ | ⛔ | ✅ | ✅ | ✅ |
| old / fixed | ✅/✅ | ?/? | ?/? | ✅/✅ | ✅/✅ | **?**/⛔ | ✅/✅ | ✅/✅ | ?/? |

After the fix, all 54 outcomes agree with GitHub, with `?` only where
verified-creator status decides. `c08` is blocked in R3 and R4 by its
`actions/checkout` step, not by the local action.

### GitHub's messages (verbatim; repo and owner names replaced)

- R1 c02: `The action astral-sh/setup-uv@v5 is not allowed in … because all actions must be from a repository owned by <owner>, created by GitHub, or match one of the patterns: astral-sh/setup-uv, gradle/*.`
- R2 (pinning): the same, then `All actions must also be pinned to a full-length commit SHA.`
- R3: `… must be from a repository owned by <owner> or created by GitHub. All actions must also not match the pattern: actions/checkout@*.`
- R4: `… must be from a repository owned by <owner>.`
- R5 c06: `The action docker://alpine:3.20 is not allowed in … because all actions must be from a repository owned by <owner>, created by GitHub, or verified in the GitHub Marketplace.`

## Findings

| Assumption in the first version | Live result | Change |
|---|---|---|
| A pattern without `@` (`astral-sh/setup-uv`) matches any ref (inferred from the #62 field note that such an entry "worked") | **Wrong.** GitHub blocked `astral-sh/setup-uv@v5` under that exact whitelist. Patterns are matched against the full `name@ref` | `pattern_matches()` always matches against `name@ref`; docs say to write `owner/action@*` |
| `*` crosses `/` | Confirmed: `gradle/*` allowed `gradle/actions/setup-gradle@v4` | none |
| A `!pattern` blacklist entry overrides GitHub-owned | Confirmed: `!actions/checkout@*` blocked checkout by tag **and** by SHA | none |
| Same-owner actions are allowed without a pattern under `selected` and `local_only` | Confirmed for a user-owned account (c01, c07); still subject to SHA pinning (c01 in R2) | none |
| Reusable workflows are exempt from SHA pinning | Confirmed: `reuse.yml@master` ran with pinning on (c07 in R2) | none |
| `docker://` is "unknown" under `selected` / `local_only` | **Blocked** in every non-`all` round, including verified-only | `evaluate()` returns blocked |
| Verified creators can't be evaluated | `astral-sh`, `gradle`, `hashicorp` all ran under verified-only (R5) | still reported as unknown (no API) |

**About the #62 field evidence:** a private repo had `patterns_allowed`
containing `astral-sh/setup-uv` with no `@`, and its setup-uv workflow
did run, so the first version took that as proof that a pattern without
`@` matches. Checked afterwards (2026-10-27, read-only), the field note
was right that the workflow worked, but not because of that entry. The
repo also has `verified_allowed = true`, and `astral-sh` is a verified
creator (it ran in R5). The bare entry matched nothing; verified status
let the action through.

## Not covered

- Organization-owned repos. Here "same owner" was a user account.
- Composite actions that call other actions (the policy's effect on nested `uses:`).
- Whether a `docker://…` pattern in `patterns_allowed` can allow an image.
