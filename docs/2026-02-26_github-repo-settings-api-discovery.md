# GitHub Repository Settings — API Discovery Reference

*Date: 2026-02-26*

## Summary

All GitHub repository settings that can be configured via the API are discoverable through a combination of the official OpenAPI spec and the REST API documentation. There is no need to scrape the UI — the machine-readable spec is the canonical, exhaustive source.

---

## Authoritative Sources (ranked)

| # | Source | URL | Best for |
|---|--------|-----|----------|
| 1 | **`github/rest-api-description`** (OpenAPI spec) | https://github.com/github/rest-api-description | Exhaustive machine-readable reference; grep for all `/repos/{owner}/{repo}` paths |
| 2 | **`docs.github.com/en/rest/repos/repos`** | https://docs.github.com/en/rest/repos/repos | Human-readable reference for `PATCH /repos` fields |
| 3 | **`docs.github.com/en/rest/branches/branch-protection`** | https://docs.github.com/en/rest/branches/branch-protection | Classic branch protection |
| 4 | **`docs.github.com/en/rest/repos/rules`** | https://docs.github.com/en/rest/repos/rules | Rulesets (modern branch/tag protection) |
| 5 | **`github.blog/changelog`** | https://github.blog/changelog | New settings that land in the UI before the API docs catch up |

### Using the OpenAPI spec

GitHub publishes their full REST API as an OpenAPI 3.0 spec. The single most useful file is:

```
descriptions/api.github.com/api.github.com.json
```

This bundled file covers every documented endpoint and schema. To enumerate all writable repository settings:

```bash
# Clone the spec repo
git clone --depth=1 https://github.com/github/rest-api-description

# Find all paths under /repos/{owner}/{repo}
cat descriptions/api.github.com/api.github.com.json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); [print(p) for p in d['paths'] if p.startswith('/repos/')]"
```

The spec ships in two variants:
- **`descriptions/`** — OpenAPI 3.0, stable
- **`descriptions-next/`** — OpenAPI 3.1, may have breaking changes

Product variants exist for `api.github.com` (Free/Pro/Team), `ghec` (Enterprise Cloud), and each GHES version.

---

## Main Endpoints for Repository Settings

### `PATCH /repos/{owner}/{repo}` — General settings

The broadest single endpoint. Requires `admin` permission.

| Category | Fields |
|----------|--------|
| Identity | `name`, `description`, `homepage` |
| Visibility | `private`, `visibility` |
| Features | `has_issues`, `has_projects`, `has_wiki`, `allow_forking`, `is_template` |
| Merge behavior | `allow_squash_merge`, `allow_merge_commit`, `allow_rebase_merge`, `allow_auto_merge`, `delete_branch_on_merge`, `allow_update_branch` |
| Merge messages | `squash_merge_commit_title/message`, `merge_commit_title/message` |
| Security | `security_and_analysis` (Advanced Security, secret scanning, AI detection, delegated dismissal/bypass) |
| Misc | `web_commit_signoff_required`, `archived`, `default_branch` |

### `PUT /repos/{owner}/{repo}/branches/{branch}/protection` — Classic branch protection

Fields: `required_status_checks`, `enforce_admins`, `required_pull_request_reviews` (including `dismiss_stale_reviews`, `require_code_owner_reviews`, `required_approving_review_count`, `require_last_push_approval`, `bypass_pull_request_allowances`), `restrictions` (users/teams/apps), `required_linear_history`, `allow_force_pushes`, `allow_deletions`, `block_creations`, `required_conversation_resolution`, `lock_branch`, `allow_fork_syncing`.

### `POST /repos/{owner}/{repo}/rulesets` — Modern rulesets

Rule types available:

| Category | Rule types |
|----------|------------|
| Restriction | `creation`, `update`, `deletion`, `non_fast_forward` |
| Code quality | `required_linear_history`, `required_signatures`, `pull_request` |
| Pattern matching | `commit_message_pattern`, `commit_author_email_pattern`, `committer_email_pattern`, `branch_name_pattern`, `tag_name_pattern` |
| File restrictions | `file_path_restriction`, `file_extension_restriction`, `max_file_path_length`, `max_file_size` |
| Status/workflow | `required_status_checks`, `workflows`, `code_scanning`, `merge_queue`, `required_deployments` |
| Review | `copilot_code_review` |

Rulesets also support `bypass_actors` (who can bypass rules) and `conditions` (which branches/tags they target).

### Discrete feature endpoints

| Endpoint | Purpose |
|----------|---------|
| `PUT /repos/{owner}/{repo}/vulnerability-alerts` | Enable Dependabot alerts |
| `PUT /repos/{owner}/{repo}/automated-security-fixes` | Enable Dependabot auto-fix |
| `PUT /repos/{owner}/{repo}/actions/permissions` | GitHub Actions permissions |
| `PUT /repos/{owner}/{repo}/topics` | Repository topics/tags |
| `PUT /repos/{owner}/{repo}/private-vulnerability-reporting` | Private vulnerability reporting |
| `PUT /repos/{owner}/{repo}/interaction-limits` | Temporary interaction limits (restrict new users, etc.) |

---

## Known Gaps: UI vs. API

**New UI settings with no confirmed API coverage (as of Feb 2026):** GitHub added "disable pull requests entirely" and "restrict pull requests to collaborators" to the Settings > General UI panel ([changelog, 2026-02-13](https://github.blog/changelog/2026-02-13-new-repository-settings-for-configuring-pull-request-access/)). These are not yet documented in `PATCH /repos`.

**Permission model asymmetry:** Several `PATCH /repos` fields require `admin` permission even for settings that maintainers can change in the UI. GraphQL can *read* most settings but lacks mutations to modify them — only rulesets are mutable via GraphQL.

**Code security configurations (org-level):** The older per-feature security API (enabling individual `security_and_analysis` flags) was de-emphasised in mid-2024 in favor of Code Security Configurations (`/orgs/{org}/code-security/configurations`). The new system is primarily org-level. Individual flags on `PATCH /repos` still work but are considered legacy for org-managed repos.

**Signal for new gaps:** The `github.blog/changelog` is the fastest indicator when a new UI setting lands without API coverage — watch for changelog entries that describe UI changes with no mention of an API endpoint.

---

## Relevance to `gh-safe-repo`

The current implementation covers the settings available via `PATCH /repos`, classic branch protection, rulesets, Dependabot, and Actions permissions. The OpenAPI spec file can be used to:

1. Audit completeness — diff the spec's `/repos/{owner}/{repo}` paths against what we currently apply
2. Detect newly added fields after GitHub spec releases
3. Validate request bodies match the documented schema before making API calls
