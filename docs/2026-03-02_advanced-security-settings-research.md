# GitHub Advanced Security Settings: Research & Plan Availability

**Date**: 2026-03-02
**Scope**: Settings visible in the repository Security → Advanced Security tab (as of 2026)

---

## Context: The April 2025 Restructuring

On **April 1, 2025**, GitHub unbundled GitHub Advanced Security (GHAS) into two separate paid add-ons:

| Product | Price | Includes |
|---------|-------|---------|
| **GitHub Secret Protection** | $19/month per active committer | Secret scanning, push protection, AI-powered detection, custom patterns |
| **GitHub Code Security** | $30/month per active committer | CodeQL code scanning, Copilot Autofix, dependency review, security campaigns |

Prior to this date, these were bundled in a single GHAS license ($49/month). The separation affects how we should think about what to automate — a repo owner might have one product but not the other.

**For personal/free accounts the practical picture is unchanged**: all Advanced Security features remain free for public repositories. Private repository access to these features still requires a paid subscription.

---

## Settings Reference

### 1. Private Vulnerability Reporting

**What it does**: Allows community members (non-collaborators) to privately report security vulnerabilities directly to maintainers, without publicly disclosing the issue. Reports arrive as draft security advisories. GitHub facilitates coordinated disclosure.

| Plan | Public Repos | Private Repos |
|------|:---:|:---:|
| Free | ✅ | ✅ |
| Pro | ✅ | ✅ |
| Team | ✅ | ✅ |
| Enterprise | ✅ | ✅ |

**Available to all plans and both visibilities.** This is one of the few Advanced Security features that does not gate on plan level.

**API:**
```
PUT  /repos/{owner}/{repo}/private-vulnerability-reporting    # enable
DELETE /repos/{owner}/{repo}/private-vulnerability-reporting  # disable
GET  /repos/{owner}/{repo}/private-vulnerability-reporting    # check status
```

**Safe default recommendation**: **Enable.** Zero cost on all plans; gives responsible researchers a path to contact you that does not require a public issue. Important for public repos and still meaningful for private repos (e.g., contractors or auditors who have read access but should not be creating public advisories).

---

### 2. Dependency Graph

**What it does**: Parses supported manifest files (package.json, requirements.txt, Gemfile.lock, etc.) to build a graph of your dependencies and their transitive dependencies. Required for Dependabot alerts to function.

| Plan | Public Repos | Private Repos |
|------|:---:|:---:|
| Free | ✅ (always on) | ✅ (toggleable) |
| Pro | ✅ | ✅ |
| Team | ✅ | ✅ |
| Enterprise | ✅ | ✅ |

Public repositories always have the dependency graph enabled and it cannot be disabled. Private repositories can toggle it.

**API**: The dependency graph itself has no dedicated enable/disable endpoint. It is controlled via the `security_and_analysis` field in the repository PATCH endpoint:
```
PATCH /repos/{owner}/{repo}
Body: {"security_and_analysis": {"dependency_graph": {"status": "enabled"}}}
```

However, this is only meaningful for private repos (it is always on for public).

**Safe default recommendation**: **Enable** (private repos). No plan gating; the only cost is the API call to enable it. Required precondition for Dependabot.

---

### 2a. Automatic Dependency Submission

**What it does**: A sub-feature of the dependency graph. Some build systems (Gradle, Swift, etc.) do not use a static manifest file that GitHub can parse — the dependency tree is only known at build time. Automatic dependency submission runs a GitHub Actions workflow that reports build-time dependencies via the Dependency Submission API so they appear in the graph and are covered by Dependabot alerts.

| Plan | Public Repos | Private Repos |
|------|:---:|:---:|
| Free | ✅ | ✅ |
| All paid | ✅ | ✅ |

Available everywhere Actions is available. The UI shows a "Set up" flow that commits a workflow file — it is not a toggle on the API.

**API**: The Dependency Submission API accepts snapshots of dependencies detected during a build:
```
POST /repos/{owner}/{repo}/dependency-graph/snapshots
```

This is called by the Action itself, not by the tool that sets up the repo.

**Safe default recommendation**: **Out of scope for gh-safe-repo.** This requires adding a `.github/workflows/dependency-submission.yml` file to the repository with ecosystem-specific configuration. It is not a repo-level toggle; it is a workflow the developer opts into. Adding it blindly without knowing the build system would be wrong.

---

### 3. Dependabot Alerts

**What it does**: When a vulnerability is found in the GitHub Advisory Database that affects one of your declared dependencies, GitHub sends an alert. Requires the dependency graph to be enabled.

| Plan | Public Repos | Private Repos |
|------|:---:|:---:|
| Free | ✅ | ✅ |
| Pro | ✅ | ✅ |
| Team | ✅ | ✅ |
| Enterprise | ✅ | ✅ |

**Available to all plans, both visibilities.** As of late 2022, GitHub made Dependabot alerts free for private repositories on all plan levels.

**API:**
```
PUT    /repos/{owner}/{repo}/vulnerability-alerts    # enable
DELETE /repos/{owner}/{repo}/vulnerability-alerts    # disable
GET    /repos/{owner}/{repo}/vulnerability-alerts    # check (204 = enabled, 404 = disabled)
GET    /repos/{owner}/{repo}/dependabot/alerts       # list current alerts
```

**Safe default recommendation**: **Enable.** Free everywhere, high value. Already implemented in `gh_safe_repo/plugins/security.py`.

---

### 3a. Dependabot Rules (Custom Auto-Triage Rules)

**What it does**: Allows configuring rules that automatically dismiss or re-open Dependabot alerts based on criteria like CVSS score, CWE type, scope, or whether a patch is available. The screenshot shows "1 rule enabled".

| Plan | Public Repos | Private Repos |
|------|:---:|:---:|
| Free | ✅ (preset rules only) | ✅ (preset rules only) |
| GitHub Code Security | ✅ | ✅ (custom rules) |

Preset auto-dismiss rules (e.g., dismiss dev-scope alerts with no known exploit) are available free. Custom rules require GitHub Code Security license.

**API**: Rules are managed via the `dependabot_alerts` settings. Not currently automated by gh-safe-repo.

**Safe default recommendation**: **Out of scope for now.** The "1 rule enabled" in the screenshot is likely the default preset. Enabling preset rules requires no API call (they are configured via the repository UI).

---

### 4. Dependabot Security Updates

**What it does**: When a Dependabot alert fires, GitHub automatically opens a pull request to bump the vulnerable dependency to the minimum non-vulnerable version. Requires Dependabot alerts to be enabled.

| Plan | Public Repos | Private Repos |
|------|:---:|:---:|
| Free | ✅ | ✅ |
| All paid | ✅ | ✅ |

**Available to all plans, both visibilities.**

**API:**
```
PUT    /repos/{owner}/{repo}/automated-security-fixes    # enable
DELETE /repos/{owner}/{repo}/automated-security-fixes    # disable
GET    /repos/{owner}/{repo}/automated-security-fixes    # check status
```

**Safe default recommendation**: **Enable.** Already implemented in `gh_safe_repo/plugins/security.py`. Note: the current implementation enables this but it only has effect once Dependabot alerts are also enabled. The ordering in the plugin already handles this correctly.

---

### 5. Grouped Security Updates

**What it does**: Instead of one PR per vulnerable dependency, GitHub groups all available Dependabot security updates for a given package manager/directory into a single PR. Can be overridden via `dependabot.yml` group rules. Became generally available March 2024.

| Plan | Public Repos | Private Repos |
|------|:---:|:---:|
| Free | ✅ | ✅ |
| All paid | ✅ | ✅ |

**Available to all plans, both visibilities.**

**API**: There is no standalone API endpoint for grouped security updates. It is enabled/disabled via the repository settings UI or via `dependabot.yml` configuration. As of early 2026 there is no programmatic REST API toggle equivalent.

**Safe default recommendation**: **Consider enabling, but no API path.** This setting reduces PR noise and is free. However, automating it requires either committing a `dependabot.yml` file or using the UI. Since gh-safe-repo does not manage repository files, this is out of scope unless file-creation support is added.

---

### 6. Dependabot Version Updates

**What it does**: Goes beyond security — Dependabot opens PRs to keep all dependencies on their latest versions, not just to fix known vulnerabilities. Requires a `dependabot.yml` configuration file in the repository specifying ecosystems and update schedules.

| Plan | Public Repos | Private Repos |
|------|:---:|:---:|
| Free | ✅ | ✅ |
| All paid | ✅ | ✅ |

**Available to all plans, both visibilities.**

**API**: Not configurable via API. Requires a `dependabot.yml` file committed to the repository at `.github/dependabot.yml`.

**Safe default recommendation**: **Out of scope for gh-safe-repo.** Requires knowledge of the project's language/ecosystem to write a useful `dependabot.yml`. A generic file would either do nothing or be wrong for the project. This is user-authored configuration, not a toggle.

---

### 7. Code Scanning / CodeQL Analysis

**What it does**: Static analysis that identifies security vulnerabilities and code errors. CodeQL is GitHub's own semantic analysis engine. Can find issues like SQL injection, XSS, path traversal, etc. in supported languages (C/C++, C#, Go, Java/Kotlin, JavaScript/TypeScript, Python, Ruby, Swift).

| Plan | Public Repos | Private Repos |
|------|:---:|:---:|
| Free | ✅ | ❌ |
| Pro | ✅ | ❌ |
| Team | ✅ | ❌ |
| **GitHub Code Security** | ✅ | ✅ |

**Private repos require GitHub Code Security ($30/month per active committer).** Public repos get it free on all plans.

**Setup**: Code scanning is not a toggle — it requires a GitHub Actions workflow. The "Set up" button in the UI commits a workflow file. There are two modes:
- **Default setup**: GitHub auto-detects languages and configures a workflow (no file committed; managed entirely by GitHub)
- **Advanced setup**: User commits a customizable `codeql-analysis.yml` workflow

**API for default setup:**
```
PATCH /repos/{owner}/{repo}/code-scanning/default-setup
Body: {"state": "configured", "languages": [...]}
```

**Safe default recommendation**: **Conditionally implement.** For public repos (all plans), default setup via the API is a meaningful safe default. For private repos, gate behind Code Security plan detection. This is a gap in the current gh-safe-repo implementation.

---

### 7a. Other Code Scanning Tools

**What it does**: Allows adding third-party SAST tools (Snyk, SonarCloud, Semgrep, etc.) via GitHub Actions. These upload SARIF results to GitHub's code scanning API.

**API:**
```
POST /repos/{owner}/{repo}/code-scanning/sarifs    # upload results
GET  /repos/{owner}/{repo}/code-scanning/alerts    # retrieve alerts
```

**Safe default recommendation**: **Out of scope.** Requires knowledge of which third-party tool the user has licensed and configured. Not a general-purpose toggle.

---

### 7b. Copilot Autofix

**What it does**: Uses Copilot AI to suggest code fixes for CodeQL alerts. Presents a diff in the PR or alert UI. Requires CodeQL to be enabled.

| Plan | Public Repos | Private Repos |
|------|:---:|:---:|
| Free (+ Copilot) | ✅ | ❌ |
| GitHub Code Security | ✅ | ✅ |

For public repos, Copilot Autofix is available without a separate Copilot subscription; it is bundled with the free CodeQL access. For private repos, GitHub Code Security license is required.

**API**: Autofix suggestions are generated automatically when a CodeQL alert is created; there is no separate enable/disable API. It activates when CodeQL is configured.

**Safe default recommendation**: **Out of scope.** Depends on CodeQL being configured first. Activates automatically; no API action needed.

---

### 7c. Code Scanning Protection Rules (Check Runs Failure Threshold)

**What it does**: Configures at what severity level code scanning check runs are marked as failed, which can be used in branch rulesets to block merges. The screenshot shows two thresholds:
- **Security alert severity level**: High or higher (blocks on High, Critical)
- **Standard alert severity level**: Only errors

| Plan | Public Repos | Private Repos |
|------|:---:|:---:|
| Free | ✅ | N/A (no code scanning) |
| GitHub Code Security | ✅ | ✅ |

**Safe default recommendation**: **Out of scope for now.** These thresholds only matter if CodeQL is set up. Once CodeQL support is added to gh-safe-repo, sensible defaults (High or higher for security, Only errors for standard) should be applied.

---

### 8. Secret Protection (Secret Scanning — Partner Patterns)

**What it does**: GitHub scans every push and all existing content for tokens, API keys, and credentials matching patterns from partner organizations (AWS, Azure, GitHub, Stripe, Twilio, etc. — 200+ providers). When a secret is found, the provider and user are both notified and the provider can invalidate the credential automatically.

| Plan | Public Repos | Private Repos |
|------|:---:|:---:|
| Free | ✅ (always on, cannot disable) | ❌ |
| Pro | ✅ | ❌ |
| Team | ✅ | ❌ |
| **GitHub Secret Protection** | ✅ | ✅ |

**For public repos**: Secret scanning runs automatically and cannot be disabled via the API. The "Disable" button in the screenshot appears because the repo is public — clicking it would disable it (generally not recommended). For private repos, GitHub Secret Protection ($19/month per active committer) is required.

**API (private repos only, requires Secret Protection license):**
```
PATCH /repos/{owner}/{repo}
Body: {"security_and_analysis": {"secret_scanning": {"status": "enabled"}}}
```

**Already in gh-safe-repo**: `SecurityPlugin` handles this correctly — it emits a SKIP with reason "Automatically enabled for public repositories by GitHub" rather than making an API call for public repos. For private repos on paid plans, it calls the PATCH endpoint. This logic remains correct.

---

### 9. Push Protection

**What it does**: Intercepts `git push` operations before they land on GitHub and blocks the push if any of the committed content matches a known secret pattern. The developer must either remove the secret or explicitly bypass (with an audit trail). Requires secret scanning to be enabled.

| Plan | Public Repos | Private Repos |
|------|:---:|:---:|
| Free | ✅ | ❌ |
| Pro | ✅ | ❌ |
| Team | ✅ | ❌ |
| **GitHub Secret Protection** | ✅ | ✅ |

For public repos, push protection is available free and protects all pushers (including the repo owner) from accidentally committing secrets. For private repos, GitHub Secret Protection is required.

**API (enabling at repository level):**
```
PATCH /repos/{owner}/{repo}
Body: {"security_and_analysis": {"secret_scanning_push_protection": {"status": "enabled"}}}
```

**Safe default recommendation**: **Enable for public repos; enable for private repos on Secret Protection plan.** This is a high-value, zero-false-positive setting for known secrets. This is a gap in the current gh-safe-repo implementation — push protection is not currently applied.

---

## Summary Matrix: What gh-safe-repo Should Do

| Setting | Free Public | Free Private | Paid Private | Current Status | Recommendation |
|---------|:-----------:|:------------:|:------------:|----------------|---------------|
| Private vulnerability reporting | ✅ Enable | ✅ Enable | ✅ Enable | ❌ Not implemented | Add to SecurityPlugin |
| Dependency graph | Always on | ✅ Enable | ✅ Enable | ✅ Implemented | Done |
| Auto dependency submission | Out of scope | Out of scope | Out of scope | N/A | N/A |
| Dependabot alerts | ✅ Enable | ✅ Enable | ✅ Enable | ✅ Implemented | Done |
| Dependabot security updates | ✅ Enable | ✅ Enable | ✅ Enable | ✅ Implemented | Done |
| Grouped security updates | No API | No API | No API | N/A | Future: commit dependabot.yml |
| Dependabot version updates | No API | No API | No API | N/A | Out of scope |
| CodeQL default setup | ✅ Enable | ❌ Skip | ✅ Enable (Code Sec.) | ❌ Not implemented | Add, gated on plan |
| Copilot Autofix | Auto | ❌ Skip | Auto | N/A | Activates with CodeQL |
| Secret scanning | Always on (SKIP) | ❌ Skip | ✅ Enable (Sec. Prot.) | ✅ Implemented | Done |
| Push protection | ✅ Enable | ❌ Skip | ✅ Enable (Sec. Prot.) | ❌ Not implemented | Add to SecurityPlugin |

---

## Implementation Notes for gh-safe-repo

### New settings to add to SecurityPlugin

**1. Private vulnerability reporting**
```
PUT  /repos/{owner}/{repo}/private-vulnerability-reporting
DELETE /repos/{owner}/{repo}/private-vulnerability-reporting
```
No plan gating. Applies to both public and private repos on all plans. Safe to enable unconditionally.

**2. Push protection**
```
PATCH /repos/{owner}/{repo}
Body: {"security_and_analysis": {"secret_scanning_push_protection": {"status": "enabled"}}}
```
- Public repos (all plans): Enable unconditionally.
- Private repos: Gate on `is_paid_plan` (Secret Protection license). Emit SKIP with reason "Requires GitHub Secret Protection for private repositories" when not applicable.
- Note: Requires secret scanning to be enabled first. Apply after secret scanning in the same `apply()` sequence.

**3. CodeQL default setup (new plugin or extension to SecurityPlugin)**
```
PATCH /repos/{owner}/{repo}/code-scanning/default-setup
Body: {"state": "configured"}
```
- Public repos (all plans): Enable.
- Private repos: Gate on Code Security plan detection. The existing `get_plan_name()` returns the plan; an additional check for Code Security entitlement may be needed (the user plan name alone does not indicate GHAS product add-ons).
- If the repo has no Actions minutes or is explicitly Actions-disabled, skip with a note.
- This requires a new `code_scanning` key in `SAFE_DEFAULTS`.

### Plan detection gap

The current `get_plan_name()` returns the GitHub account plan name (`free`, `pro`, `team`, `business`). This is sufficient to distinguish Free from Pro/Team/Enterprise. However, the new GitHub Code Security and Secret Protection add-ons are **separate from the plan name** — a Team plan account might or might not have purchased Code Security. There is currently no REST API endpoint that reliably returns whether a given repository has Code Security or Secret Protection entitlements.

**Practical approach**: Use the same heuristic currently used for Dependabot/branch protection: `is_paid_plan = (plan_name != "free")`. This is slightly optimistic (it assumes any paid plan has the add-ons), but the API will return a 402/403 for unlicensed features, and the plugin already handles non-2xx responses gracefully. Emit a SKIP with a clear message on failure.

### Existing CLAUDE.md learnings still accurate

- "Secret scanning requires no API call for public repos" remains correct. The `SKIP` with reason text is the right behavior; the GitHub UI shows "Disable" but the setting was never `disabled` — it is always-on.
- The `PATCH /repos/{owner}/{repo}` with `security_and_analysis` body is still the correct endpoint for private-repo secret scanning.
- Push protection uses the same endpoint/body structure (`secret_scanning_push_protection` key).

---

## References

- [Introducing GitHub Secret Protection and GitHub Code Security (GitHub Blog, March 2025)](https://github.blog/changelog/2025-03-04-introducing-github-secret-protection-and-github-code-security/)
- [About GitHub Advanced Security — GitHub Docs](https://docs.github.com/en/get-started/learning-about-github/about-github-advanced-github-security)
- [About secret scanning — GitHub Docs](https://docs.github.com/en/code-security/secret-scanning/about-secret-scanning)
- [About push protection — GitHub Docs](https://docs.github.com/en/code-security/secret-scanning/introduction/about-push-protection)
- [About Dependabot alerts — GitHub Docs](https://docs.github.com/en/code-security/dependabot/dependabot-alerts/about-dependabot-alerts)
- [About code scanning — GitHub Docs](https://docs.github.com/en/code-security/code-scanning/introduction-to-code-scanning/about-code-scanning)
- [Configuring private vulnerability reporting — GitHub Docs](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository)
- [REST API: branches/branch-protection — GitHub Docs](https://docs.github.com/en/rest/branches/branch-protection)
- [REST API: code-scanning — GitHub Docs](https://docs.github.com/en/rest/code-scanning/code-scanning)
- [REST API: secret-scanning — GitHub Docs](https://docs.github.com/en/rest/secret-scanning/secret-scanning)
