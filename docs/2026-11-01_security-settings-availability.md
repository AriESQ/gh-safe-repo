# Security settings: availability, API behaviour and live checks (2026-11-01)

A reference for every security setting `plugins/security.py` manages, plus
the plan gates the other plugins rely on. For each setting it records:

1. what GitHub's docs say about where it is available,
2. what the REST description (the spec) says about its endpoints,
3. what live API calls actually returned, with dates.

Where these disagree, the live result wins and the gap is noted. Add rows
as new checks are run; keep the date on every observation.

**Sources**

| Tag | Source |
|---|---|
| **docs** | GitHub docs source, `data/reusables/gated-features/<feature>.md` unless noted. Local mirror of `github/docs` at commit `dae83f7d28` (2026-10-16). |
| **spec** | `github/rest-api-description`, `descriptions/api.github.com/api.github.com.2026-03-10.json`. |
| **live** | Calls made against real repos. Scratch repos are named; one private repo on a second Free account is referred to as "second-account private repo". |
| **#94** | `docs/2026-10-22_github-api-read-shapes.md` (read shapes for every plugin). |
| **drift** | `tools/api_drift/catalog.json` quirk ids. |

**Accounts used for live checks.** Both are user accounts on GitHub Free.
Free status confirmed by `GET /repos/{o}/{r}/rulesets` on a private repo
returning `403 "Upgrade to GitHub Pro or make this repository public to
enable this feature."` (2026-11-01). No Pro, Team or Enterprise account has
been available, so every "paid" cell below is from the docs only.

---

## 1. Availability matrix (docs + live)

✅ available · ❌ not available · ⚙️ available but off by default ·
**L** confirmed live · *d* docs only

| Setting | Public (any plan) | Private, user-owned, Free | Private, user-owned, Pro | Private, org-owned, Team / Enterprise |
|---|---|---|---|---|
| Dependabot alerts | ✅ **L** | ⚙️ **L** (2026-11-01) | ⚙️ *d* | ⚙️ *d* |
| Dependabot security updates | ✅ **L** | ⚙️ **L** (2026-11-01) | ⚙️ *d* | ⚙️ *d* |
| Dependency graph | ✅ on by default *d* | ⚙️ **L**, turned on by the alerts `PUT` (2026-11-01) | ⚙️ *d* | ⚙️ *d* |
| Secret scanning | ✅ on by default, can be turned off **L** (#94) | ❌ **L** (422, 2026-11-01) | ❌ *d* (needs GHEC with EMU) | with GitHub Secret Protection *d* |
| Push protection | ✅ **L** (#94) | ❌ *d*; live write is 200 and ignored (2026-11-01) | ❌ *d* | with GitHub Secret Protection *d* |
| Private vulnerability reporting | ✅ **L** (#94) | ❌ **L** (2026-10-14, 2026-11-01) | ❌ *d* | ❌ *d* |
| Branch protection / rulesets (reference gate) | ✅ | ❌ **L** (403, 2026-11-01) | ✅ *d* | ✅ *d* |
| Code scanning (not managed; reference) | ✅ *d* | ❌ *d* | ❌ *d* | with GitHub Code Security *d* |

**What `plugins/security.py` does today (v0.11.1), compared with the matrix:**

| Setting | Private + Free (`is_paid_plan` False) | Private + paid (`is_paid_plan` True) |
|---|---|---|
| Dependabot alerts | SKIP "Requires a public repo or paid GitHub plan" — **wrong** | plans `PUT` — right |
| Dependabot security updates | SKIP — **wrong** | plans `PUT` — right |
| Dependency graph | SKIP "Requires a public repo or paid…" — **wrong reason** (it comes with alerts) | SKIP "No REST API available" — **wrong** (the alerts `PUT` enables it) |
| Secret scanning | SKIP — right | always plans `PATCH` — **wrong for user-owned Pro**; right only for org + Secret Protection |
| Push protection | SKIP — right | plans `PATCH` — same as secret scanning |
| PVR | SKIP — right | SKIP "Only available for public repositories" — right (PR #113) |

And `is_paid_plan` is False for every standard `gh` login (section 4), so the
paid column is not reached in practice.

---

## 2. Per setting

### 2.1 Dependabot alerts

- **docs:** "Dependabot alerts are available for organization-owned and
  user-owned repositories." (`dependabot-alerts.md`). No plan or visibility
  gate. Configured in Settings, not in `dependabot.yml`
  (`about-the-dependabot-yml-file.md`).
- **spec:** `GET /repos/{o}/{r}/vulnerability-alerts` → 204 (on) / 404 ("Not
  Found if repository is not enabled with vulnerability alerts").
  `PUT` → 204, described as "Enables dependency alerts **and the dependency
  graph**". `DELETE` → 204, "Disables dependency alerts **and the dependency
  graph**".
- **live:**

| Date | Repo | Call | Result |
|---|---|---|---|
| 2026-10-22 | public scratch (#94) | `GET` on / off | 204 / `404 "Vulnerability alerts are disabled."` |
| 2026-10-22 | free+private (#94) | `GET` | 404, same message |
| 2026-11-01 | `AriESQ/gh-safe-repo-probe-2026-11-01` (private, Free, fresh) | `GET` baseline | `404 "Vulnerability alerts are disabled."` |
| 2026-11-01 | same | `PUT` | **204** |
| 2026-11-01 | same | `GET` after | **204** (on) |
| 2026-11-01 | same | `GET dependabot/alerts`, polled | 200 `[]` for ~75 s, then **5 alerts** (manifest pins `requests==2.19.0`) |
| 2026-11-01 | same | `DELETE` | 204; afterwards alerts 404, security updates `{"enabled":false}`, SBOM 404 |
| 2026-11-01 | second-account private repo (Free) | `GET` | 404 |

**Conclusion:** works on private Free. Turning it off also turns off security
updates and the dependency graph (live), matching the spec's `DELETE`
description.

### 2.2 Dependabot security updates

- **docs:** available for "All repositories on GitHub"
  (`dependabot-security-updates.md`). Works without a `dependabot.yml`;
  version updates are what need the file (`about-the-dependabot-yml-file.md`).
- **spec:** `GET /automated-security-fixes` → 200 / 404 ("Not Found if
  Dependabot is not enabled"). `PUT` / `DELETE` → 204.
- **drift:** `automated-security-fixes-off-is-200`. Off answers `200
  {"enabled":false}`, never the documented 404. `paused: true` counts as off
  (#94 decision).
- **live:**

| Date | Repo | Call | Result |
|---|---|---|---|
| 2026-10-22 | public scratch, off / on (#94) | `GET` | `200 {"enabled":false,"paused":false}` / `200 {"enabled":true,"paused":false}` |
| 2026-10-22 | free+private (#94) | `GET` | `200 {"enabled":false,"paused":false}` |
| 2026-11-01 | probe repo (private, Free) | `PUT` (after alerts on) | **204** |
| 2026-11-01 | same | `GET` after | **`200 {"enabled":true,"paused":false}`** |
| 2026-11-01 | same | Actions runs | a dynamic run `pip in /. for requests - Update` started within a minute |
| 2026-11-01 | second-account private repo (Free) | `GET` | `200 {"enabled":false,"paused":false}` |
| 2026-11-01 | `AriESQ/gh-safe-repo-probe2-2026-11-01` (private, Free) | `PUT`, then `DELETE` | 204, 204. Alerts still 204 (on), SBOM still 200, security updates `{"enabled":false}` |

**Conclusion:** works on private Free once alerts are on. The dependency is
one-way: turning off security updates leaves alerts and the graph on, but
turning off alerts turns security updates off (2.1).

### 2.3 Dependency graph

- **docs:** available for "Public repositories (on by default), Private
  repositories, Forks" (`dependency-graph.md`). Can be turned on per repo in
  the UI, or for every repo the user owns "regardless of their visibility"
  (`how-tos/.../enable-dependency-graph.md`). Dependabot job logs are reached
  from the dependency graph (`concepts/.../dependabot-job-logs.md`), so with
  the graph off there is nowhere to see why Dependabot did or did not run.
- **spec:** no per-repo enable endpoint. `PATCH /repos` `security_and_analysis`
  properties are `advanced_security`, `code_security`, `secret_scanning`,
  `secret_scanning_ai_detection`, `secret_scanning_delegated_alert_dismissal`,
  `secret_scanning_delegated_bypass`, `secret_scanning_delegated_bypass_options`,
  `secret_scanning_non_provider_patterns` and `secret_scanning_push_protection`.
  **There is no `dependency_graph` key.** The only documented write is the
  side effect of `PUT` / `DELETE vulnerability-alerts`.
  `GET /dependency-graph/sbom` → 200 / 404 / 403.
- **drift:** `unknown-security-and-analysis-key-ignored`. An undocumented
  `security_and_analysis` key answers 200 and does nothing (observed for
  `dependency_graph_autosubmit_action`, 2026-03-14).
- **live:**

| Date | Repo | Call | Result |
|---|---|---|---|
| 2026-11-01 | second-account private repo (Free) | `GET sbom` | 404 |
| 2026-11-01 | probe repo, fresh | `GET sbom` | (status lost to a script bug) |
| 2026-11-01 | probe repo | `PATCH security_and_analysis.dependency_graph` | **status lost to a script bug**; `security_and_analysis` stayed `null`. A `Graph Update: pip` run started at 02:58:32, between this `PATCH` and the alerts `PUT`, so the trigger is ambiguous |
| 2026-11-01 | probe repo | `GET sbom` right after alerts `PUT` | **200** on first poll |
| 2026-11-01 | probe repo | `GET sbom` after `DELETE vulnerability-alerts` | **404** |
| 2026-11-01 | probe2 repo, fresh, 30 s after the manifest commit | `GET sbom`; Actions runs | 404; no runs |
| 2026-11-01 | probe2 repo | `PATCH security_and_analysis.dependency_graph` (alone) | **`200 OK`, ignored.** `security_and_analysis` stayed `null`, SBOM 404 on all 12 polls over 60 s, no Actions run |
| 2026-11-01 | probe2 repo | `PUT vulnerability-alerts` | 204; SBOM **200**; `Graph Update: pip in /.` run created 03:05:43, seconds after this call (about a minute after the `PATCH`) |

**Conclusion:** on private repos, the graph is turned on and off with
Dependabot alerts, and that is the only per-repo write that works. The
`dependency_graph` key in `PATCH security_and_analysis` is accepted with 200
and silently ignored, like the other undocumented key in
`unknown-security-and-analysis-key-ignored`. In probe v1, the graph run that
looked ambiguous came from the alerts `PUT`. `GET dependency-graph/sbom`
works as a read (200 on, 404 off), which corrects #94's "no read endpoint
exists" note.

### 2.4 Secret scanning

- **docs** (`secret-scanning.md`):
  - Public repositories: "runs automatically for free".
  - Organization-owned private and internal: "Available with GitHub Secret
    Protection enabled on GitHub Team or GitHub Enterprise Cloud".
  - User-owned: "Available on GitHub Enterprise Cloud with Enterprise Managed
    Users", or GHES with Secret Protection.
  - Secret Protection itself is for "Team plan users" and Enterprise
    (`secret-protection.md`). **GitHub Pro is not listed anywhere.**
- **spec:** `PATCH /repos` `security_and_analysis.secret_scanning.status`.
  State is read from `GET /repos` → `security_and_analysis`.
- **live:**

| Date | Repo | Call | Result |
|---|---|---|---|
| 2026-10-22 | public scratch (#94) | `GET /repos` | `{"secret_scanning":{"status":"enabled"}}` on a fresh repo |
| 2026-10-22 | public scratch (#94) | `PATCH` → disabled | 200, reads back `"disabled"`. **Public repos can have it off** |
| 2026-10-22, 2026-11-01 | free+private (#94, probe repo, second-account repo) | `GET /repos` | `"security_and_analysis": null` |
| 2026-11-01 | probe repo | `PATCH` → enabled | status lost to a script bug; `security_and_analysis` stayed `null` |
| 2026-11-01 | probe2 repo (private, Free) | `PATCH` → enabled | **`422 {"message":"Secret scanning is not available for this repository."}`**; stayed `null` |

**Conclusion:** not available on private Free (live) or on Pro user-owned
repos (docs). Unlike the undocumented keys, the refusal is explicit: a 422
with a clear message. The plugin sends `secret_scanning` and push protection
in one `PATCH` (drift `push-protection-needs-secret-scanning`). So on a repo
without Secret Protection, the whole call should fail with this 422. That is
inferred from this result; the combined body was not sent.

### 2.5 Push protection

- **docs:** part of GitHub Secret Protection on private repos, the same gate
  as secret scanning. Free on public repos.
- **spec:** `PATCH /repos` `security_and_analysis.secret_scanning_push_protection.status`.
- **drift:** `push-protection-needs-secret-scanning`. Sent alone, it answers
  200 and is ignored; `secret_scanning` must be in the same body (2026-03-28).
- **live:**

| Date | Repo | Call | Result |
|---|---|---|---|
| 2026-10-22 | public scratch (#94) | `PATCH` → disabled | 200, reads back `"disabled"` |
| 2026-11-01 | probe repo | `PATCH` → enabled (alone) | status lost to a script bug; stayed `null` |
| 2026-11-01 | probe2 repo (private, Free) | `PATCH` → enabled (alone) | **`200 OK`, ignored**; `security_and_analysis` stayed `null` |

**Conclusion:** sent alone, push protection gets a 200 on private Free and
nothing changes, the same as the 2026-03-28 drift quirk on repos where it is
available. Its status code tells you nothing on either. Only a read-back (or
the 422 that `secret_scanning` brings with it) shows the result.

### 2.6 Private vulnerability reporting

- **docs:** "available for public repositories on GitHub.com"
  (`private-vulnerability-reporting.md`). No plan extends it to private repos.
- **spec:** `GET` → 200 / 422. `PUT` / `DELETE` → 204 / 422. 404 is not
  documented.
- **live:**

| Date | Repo | Call | Result |
|---|---|---|---|
| 2026-10-14 | free+private | `GET` | 404 (LEARNINGS) |
| 2026-10-22 | public scratch (#94) | `GET` off / on | `200 {"enabled":false}` / `200 {"enabled":true}`. **200 when off** |
| 2026-11-01 | probe repo (private, Free) | `GET` | `404 {"message":"Not Found"}` |
| 2026-11-01 | probe repo | `PUT` | **`404 {"message":"Not Found"}`**. The spec lists 422 |
| 2026-11-01 | second-account private repo (Free) | `GET` | 404 |

**Conclusion:** public only, as the docs say. On private repos every call
answers **404, not the spec's 422** (drift `pvr-free-private-404` for `GET`,
`pvr-put-private-404` for `PUT`).

### 2.7 Rulesets and branch protection (reference gate)

- **docs:** protected branches are available on public repos on Free, and on
  public and private repos with Pro, Team and Enterprise
  (`protected-branches.md`).
- **live:** on private Free, `GET /repos/{o}/{r}/rulesets` → `403 "Upgrade to
  GitHub Pro or make this repository public to enable this feature."`
  (2026-11-01, probe repo and another AriESQ private repo; 2026-11-01,
  second-account private repo).

Drift quirks `rulesets-free-private-403` and
`branch-protection-free-private-403`: the spec doesn't document this 403.

This 403 is the one clean, machine-readable plan signal seen so far. The
Dependabot endpoints never answer like this on private Free: they answer
"disabled" (404, `{"enabled":false}`, or 403 "Dependabot alerts are disabled
for this repository" from `GET dependabot/alerts`), meaning **off, not gated**.

---

## 3. Response patterns: gated vs off

| Pattern | Meaning | Seen on |
|---|---|---|
| `403 "Upgrade to GitHub Pro or make this repository public…"` | plan-gated | rulesets, branch protection (private Free) |
| `422 "Secret scanning is not available for this repository."` | gated (no Secret Protection) | `PATCH security_and_analysis.secret_scanning` (private Free) |
| `200` on `PATCH`, nothing changes | **accepted and ignored**; the status code says nothing | `security_and_analysis.dependency_graph`; push protection sent alone; `dependency_graph_autosubmit_action` |
| `404 "Vulnerability alerts are disabled."` | off, can be enabled | `vulnerability-alerts` |
| `200 {"enabled":false}` | off, can be enabled | `automated-security-fixes`; PVR on public |
| `403 "Dependabot alerts are disabled for this repository."` | off, can be enabled | `GET dependabot/alerts` |
| `404 sbom` | graph off | `dependency-graph/sbom` |
| `404 {"message":"Not Found"}` | not available here (PVR on private) | `private-vulnerability-reporting` GET and PUT |
| `security_and_analysis: null` | none of those features apply to this repo | `GET /repos` on private Free |

**`gh` hint gotcha.** On the 403 from `GET dependabot/alerts`, `gh` adds
`This API operation needs the "admin:repo_hook" scope`. That is wrong: the
body says alerts are off, and the same token lists alerts fine once they are
on. Don't follow the hint.

---

## 4. Plan detection

- `gh_safe_repo/commands/_common.py` sets `is_paid_plan = plan_name not in
  ("free", "")`. `get_plan_name()` reads `GET /user` → `plan.name` and
  defaults to `"free"`.
- **live (2026-11-01):** `GET /user` returns `"plan": null` for both accounts.
  Their tokens have the scopes `gist`, `read:org`, `repo` (the `gh auth login`
  default). So `is_paid_plan` is False for every standard `gh` login, whatever
  the real plan. PR #113 had noted "the token returns no plan name".
- **spec:** `plan` is optional on the `private-user` schema. Which scope makes
  it appear is not documented there. **Inferred, not checked:** `read:user`
  or `user`.
- `GET /user` describes the *authenticated user's* plan. For `fix` on an
  org-owned repo, that is the wrong account.
- Every gate that reads `is_paid_plan` is affected: `security.py:145`,
  `branch_protection.py:276`, `tag_protection.py:125`.

---

## 5. Corrections to earlier documents

| Document | Says | Correct |
|---|---|---|
| `security.py` docstring | Dependabot alerts on private repos need a paid plan | available on every plan (docs, live) |
| `security.py` docstring and `plan()` | dependency graph: "no writable REST API for private" | `PUT vulnerability-alerts` enables it (spec, live) |
| `docs/2026-03-02_advanced-security-settings-research.md` §1 | PVR available on private repos, all plans | public only (docs, live) |
| same, §2 | graph enabled via `PATCH security_and_analysis.dependency_graph` | no such key in the spec; live it answers 200 and is ignored. Use `PUT vulnerability-alerts` |
| same, summary matrix | dependency graph "✅ Implemented" | the plugin always SKIPs it on private repos |
| same, line 19 | private repos need a paid subscription for these features | true for secret scanning and push protection only, not Dependabot |
| `#94` read-shapes doc | dependency graph has no read endpoint | `GET dependency-graph/sbom` works as one |
| CLAUDE.md, README, `SKILL.md` plan tables | Dependabot alerts and security updates ❌ on Free + Private | ✅ (off by default) |
| same | secret scanning and push protection ✅ on Pro + Private | ❌ for user-owned repos |
| spec | PVR on private answers 422 | answers 404 (live) |

---

## 6. Open checks

- [x] Probe v2 (2026-11-01): the `PATCH dependency_graph` alone (200,
  ignored); secret scanning (422) and push protection (200, ignored) on
  private Free; `DELETE automated-security-fixes` leaves alerts on.
- [x] ~~Whether `read:user` makes `GET /user` return `plan`.~~ Dropped
  (2026-11-01): plan-name gating is to be replaced by repo-level signals,
  not given a second code path.
- [ ] Any paid-account row (Pro user-owned, Team org): none tested.
- [ ] The combined `secret_scanning` + push protection `PATCH` the plugin
  actually sends, on private Free (expected 422).
- [x] Drift catalog (2026-11-01): new quirks `pvr-put-private-404` and
  `user-plan-null-without-user-scope`; `unknown-security-and-analysis-key-ignored`
  and `push-protection-needs-secret-scanning` extended with the private Free
  results. The alerts `PUT`/`DELETE` side effect on the graph is documented
  in the spec, so it needs no quirk.
- [ ] Delete scratch repos `AriESQ/gh-safe-repo-probe-2026-11-01` and
  `AriESQ/gh-safe-repo-probe2-2026-11-01` once the checks are done.
