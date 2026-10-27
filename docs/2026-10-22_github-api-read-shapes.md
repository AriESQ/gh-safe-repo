# GitHub API read shapes: live verification (#94)

**Date observed:** 2026-10-22
**API version:** REST `2022-11-28` (gh CLI default), cross-checked against
the `2022-11-28` dereferenced OpenAPI description in `github/rest-api-description`
**Issue:** [#94](https://github.com/AriESQ/gh-safe-repo/issues/94). Fix: branch `fix/issue-94-api-read-audit`

This records every GitHub API **read** the plugins make, as GitHub actually
answered it on the date above, in the on, off and plan-unavailable states.
The summary table and the decisions it drove are in `docs/LEARNINGS.md`
("API Read Shapes (#94)"). This file holds the evidence. Each shape where
the status code alone would mislead is pinned by a test in
`tests/test_plugins.py::TestApiReadShapes`.

## Method

Three repositories, all owned by one user account:

| Label | Repo | Used for |
|---|---|---|
| **on** | `AriESQ/gh-safe-repo` (public, hardened by this tool) | settings in their enabled state; GET only |
| **free+private** | a private repo on the free plan (name withheld) | plan-unavailable responses; GET only |
| **scratch** | a throwaway public repo (`<owner>/<scratch>`), created for this test only and deleted afterwards | fresh-repo defaults, then each setting toggled off with a write and read back |

All requests used `gh api -i` with the account owner's `gh` OAuth token. The write sequence used on the scratch repo is
reproduced in [Appendix: probe script](#appendix-probe-script).

## Results by plugin

Bodies are trimmed to the fields the plugins read; `url`/`_links` noise
removed.

### `plugins/security.py`

| Read | State | Response |
|---|---|---|
| `GET /repos/{o}/{r}/vulnerability-alerts` | on | `204`, empty body |
| | off (scratch, fresh) | `404 {"message":"Vulnerability alerts are disabled."}` |
| | free+private | `404` (same message) |
| `GET /repos/{o}/{r}/automated-security-fixes` | on | `200 {"enabled":true,"paused":false}` |
| | off (scratch, fresh) | `200 {"enabled":false,"paused":false}` ← **200 when off** |
| | free+private | `200 {"enabled":false,"paused":false}` |
| | paused | not reproducible (GitHub pauses inactive repos on its own schedule); spec marks `paused` required |
| `GET /repos/{o}/{r}/private-vulnerability-reporting` | on | `200 {"enabled":true}` |
| | off (scratch, fresh) | `200 {"enabled":false}` ← **200 when off** |
| | free+private | `404 {"message":"Not Found"}` |
| `GET /repos/{o}/{r}` → `security_and_analysis` | on (public) | `{"secret_scanning":{"status":"enabled"},"secret_scanning_push_protection":{"status":"enabled"},"dependabot_security_updates":{"status":"enabled"},…}` |
| | scratch, fresh public | secret scanning + push protection `enabled`, dependabot updates `disabled` |
| | scratch, after `PATCH` disabling both | `{"secret_scanning":{"status":"disabled"},"secret_scanning_push_protection":{"status":"disabled"},…}` ← **public repos can have it off** |
| | free+private | `"security_and_analysis": null` |

Writes used: `PATCH /repos/{o}/{r}` with `security_and_analysis.{key}.status = "disabled"` → `200`;
`PUT vulnerability-alerts` → `204`; `PUT automated-security-fixes` → `204`, then
read back as `200 {"enabled":true,"paused":false}`.

Dependency graph: no read endpoint exists, so it is still assumed on for public repos.

### `plugins/actions.py`

| Read | State | Response |
|---|---|---|
| `GET …/actions/permissions` | on | `200 {"enabled":true,"allowed_actions":"selected","selected_actions_url":…,"sha_pinning_required":true}` |
| | scratch, fresh | `200 {"enabled":true,"allowed_actions":"all","sha_pinning_required":false}` |
| | Actions disabled (`PUT {"enabled":false}` → `204`) | `200 {"enabled":false,"sha_pinning_required":false}` ← **no `allowed_actions` key** |
| | `local_only` | `200 {"enabled":true,"allowed_actions":"local_only","sha_pinning_required":false}` |
| | free+private | `200`, same shape as on |
| `GET …/actions/permissions/selected-actions` | on | `200 {"github_owned_allowed":true,"patterns_allowed":[],"verified_allowed":false}` |
| | `selected`, untouched | `200 {"github_owned_allowed":true,"patterns_allowed":[],"verified_allowed":false}` |
| | Actions disabled | `409 {"errors":"GitHub Actions is disabled on this repository"}` |
| | `local_only` | `409 {"errors":"Only local actions and workflows are allowed on this repository"}` |
| `GET …/actions/permissions/workflow` | on | `200 {"default_workflow_permissions":"read","can_approve_pull_request_reviews":false}` |
| | Actions disabled | `200`, same shape (still readable and writable) |
| | free+private | `200`, same shape |
| `GET …/actions/permissions/fork-pr-contributor-approval` | on | `200 {"approval_policy":"all_external_contributors"}` |
| | scratch, fresh | `200 {"approval_policy":"first_time_contributors"}` |
| | Actions disabled | `200`, same shape |
| | free+private | `422 {"errors":"Fork PR approval is not allowed for private repositories."}` |

The fresh scratch repo started at `default_workflow_permissions: "read"`,
`can_approve_pull_request_reviews: false` and `first_time_contributors`,
not the documented GitHub defaults (`write` / `true` /
`first_time_contributors_new_to_github`). Those values follow the
account-level defaults, so a "fresh" repo is not a reliable source for
`GITHUB_DEFAULTS`.

### `plugins/branch_protection.py`

| Read | State | Response |
|---|---|---|
| `GET …/rulesets` | on | `200 [{"id":…,"name":"gh-safe-repo tag defaults","target":"tag","source_type":"Repository","enforcement":"active",…}]` |
| | scratch, fresh | `200 []` |
| | free+private | `403 {"message":"Upgrade to GitHub Pro or make this repository public to enable this feature."}` |
| `GET …/rulesets/{id}` | created with `enforcement: "disabled"` | `200 {"enforcement":"disabled","conditions":{…},"rules":[{"type":"deletion"}],…}` ← **rules listed but not enforced** |
| | after `PUT {"enforcement":"active"}` | `200 {"enforcement":"active",…}` |
| `PATCH …/rulesets/{id}` (what the old code sent) | | **`404 Not Found`**: not a supported method |
| `PUT …/rulesets/{id}` | | `200` |
| `PUT …/rulesets/{id}` `{"enforcement":"evaluate"}` | non-Enterprise | `422 {"errors":["Enforcement evaluate option is not supported on this plan. Please upgrade to Enterprise to enable it."]}` |
| `GET …/branches/{b}/protection` | protected (on) | `200` + full body |
| | existing branch, unprotected | `404 {"message":"Branch not protected"}` |
| | branch does not exist | `404 {"message":"Branch not found"}` |
| | free+private | `403 {"message":"Upgrade to GitHub Pro…"}` |
| | protected with every option null/false | `200 {"required_signatures":{"enabled":false},"enforce_admins":{"enabled":false},"required_linear_history":{"enabled":false},"allow_force_pushes":{"enabled":false},"allow_deletions":{"enabled":false},"block_creations":{"enabled":false},"required_conversation_resolution":{"enabled":false},"lock_branch":{"enabled":false},"allow_fork_syncing":{"enabled":false}}`, with **no `required_pull_request_reviews` key** (read as `require_pull_request = false`, which is correct) |

The ruleset list endpoint includes `enforcement` on every item, but the
plugins read it from the detail response, which is authoritative.

### `plugins/tag_protection.py`

Same `rulesets` list/detail endpoints and shapes as above. One extra write
check:

| Write | Response |
|---|---|
| `POST …/rulesets` with a name that already exists (what the old `apply` did on every `fix`) | **`422 {"errors":["Name must be unique"]}`** |

### `plugins/repository.py`

| Read | State | Response |
|---|---|---|
| `GET /repos/{o}/{r}` merge/feature fields | admin, public and private | all of `private`, `allow_squash_merge`, `allow_merge_commit`, `allow_rebase_merge`, `has_wiki`, `has_issues`, `has_projects`, `delete_branch_on_merge` present as booleans |
| `GET …/contents/README.md` | exists | `200 {"name":"README.md","path":"README.md","sha":"…",…}` |
| | missing | `404 {"message":"Not Found"}` |

Booleans only. No setting here can come back as "200 but off", so the
status-plus-field reads in the plugin were already correct.

## Findings → fixes

| # | Finding | Before | After |
|---|---|---|---|
| 1 | Ruleset `enforcement: "disabled"` keeps its rules | a disabled branch or tag ruleset counted as full protection | only `active` counts (`plugins/base.ruleset_enforced`); `fix` re-enables it |
| 2 | `PATCH rulesets/{id}` → `404` | `fix` could never update an existing branch ruleset | `PUT` |
| 3 | Duplicate-name `POST rulesets` → `422` | `fix` on any repo that already had the tag ruleset failed | upsert: find the id, `PUT` it |
| 4 | Actions disabled omits `allowed_actions` | read as `"all"`; `fix` planned `selected` and PUT `enabled: true`, **switching Actions back on** | `allowed_actions` / `sha_pinning_required` are `SKIP` ("GitHub Actions is disabled for this repository") |
| 5 | `automated-security-fixes` 200 + `paused: true` | counted as on | counts as off (user decision, 2026-10-22) |
| 6 | Public repo secret scanning can be `"disabled"` | assumed on | read from `security_and_analysis`; only a missing key assumes on |
| 7 | Null Actions fields (spec marks only `enabled` required) | `None` reached the comparison | `actions._field()` type-checks and falls back to GitHub's default |

Already correct, now covered by tests: `vulnerability-alerts` (status only
is right: 204/404), `private-vulnerability-reporting` (body-read since #92),
the empty-list vs. 403 split on `rulesets`, classic protection 404 vs. 403,
and `security_and_analysis: null` on free+private.

## Not verified live

- **Paused Dependabot updates:** whether `PUT automated-security-fixes`
  un-pauses them. GitHub decides when to pause, so the state could not be
  produced on demand.
- **Paid plan + private:** no Pro account was available. The plugins treat
  paid+private reads like public ones. The spec shapes are the same; only
  availability differs.
- **Organization-owned repos:** not exercised. `rulesets` lists
  org-inherited rulesets too (`includes_parents` defaults to `true`), and
  the plugins match by name and `target`, not by `source_type`.

## Appendix: probe script

Run against the scratch repo with the user's credentials. The output was
captured on 2026-10-22 and summarized above.

```bash
R=repos/<owner>/<scratch>
g()   { echo "== GET $1"; gh api -i "$R/$1" 2>&1 | grep -E '^HTTP|^\{|^\['; }
put() { echo ">> $1 $2 $3"; echo "$3" | gh api -X "$1" "$R${2:+/$2}" --input - -i 2>&1 | grep -E '^HTTP|message'; }

# 1. Actions disabled
put PUT actions/permissions '{"enabled":false}'
g actions/permissions; g actions/permissions/selected-actions
g actions/permissions/workflow; g actions/permissions/fork-pr-contributor-approval
# 2. local_only
put PUT actions/permissions '{"enabled":true,"allowed_actions":"local_only"}'
g actions/permissions; g actions/permissions/selected-actions
# 3. selected, defaults untouched
put PUT actions/permissions '{"enabled":true,"allowed_actions":"selected"}'
g actions/permissions; g actions/permissions/selected-actions
put PUT actions/permissions '{"enabled":true,"allowed_actions":"all"}'
# 4. secret scanning + push protection off on a public repo
put PATCH "" '{"security_and_analysis":{"secret_scanning_push_protection":{"status":"disabled"}}}'
put PATCH "" '{"security_and_analysis":{"secret_scanning":{"status":"disabled"}}}'
gh api $R | jq -c '{security_and_analysis}'
# 5. Dependabot alerts + security updates on
put PUT vulnerability-alerts '{}'; put PUT automated-security-fixes '{}'
g vulnerability-alerts; g automated-security-fixes
# 6. rulesets with enforcement disabled; duplicate name; evaluate; PATCH vs PUT
put POST rulesets '{"name":"gh-safe-repo tag defaults","target":"tag","enforcement":"disabled","conditions":{"ref_name":{"include":["refs/tags/*"],"exclude":[]}},"rules":[{"type":"deletion"}]}'
put POST rulesets '{"name":"gh-safe-repo branch defaults","target":"branch","enforcement":"disabled","conditions":{"ref_name":{"include":["refs/heads/master"],"exclude":[]}},"rules":[{"type":"deletion"}]}'
g rulesets
put POST rulesets '{"name":"gh-safe-repo tag defaults","target":"tag","enforcement":"active","conditions":{"ref_name":{"include":["refs/tags/*"],"exclude":[]}},"rules":[{"type":"deletion"}]}'
put PUT   "rulesets/$TAG_ID"    '{"enforcement":"evaluate"}'
put PATCH "rulesets/$BRANCH_ID" '{"enforcement":"active"}'; g "rulesets/$BRANCH_ID"
put PUT   "rulesets/$BRANCH_ID" '{"enforcement":"active"}'; g "rulesets/$BRANCH_ID"
# 7. classic protection with every option off
put PUT branches/master/protection '{"required_status_checks":null,"enforce_admins":false,"required_pull_request_reviews":null,"restrictions":null}'
g branches/master/protection
# GET-only extras
g branches/nonexistent-branch/protection
g contents/README.md; g contents/NOPE.md
```
