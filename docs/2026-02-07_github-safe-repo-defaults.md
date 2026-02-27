# GitHub Safe Repo Defaults Automation

**Date**: 2026-02-07

## The Idea

Create automation to quickly spin up GitHub repositories with safe defaults for a personal account (not relying on organization-level rules).

## Key Concerns & Questions

### Security Issues to Address
- **GitHub Actions abuse**: Prevent external contributors from consuming your GitHub Actions minutes through malicious PRs
- **Branch protection**: Enforce PR workflow - no direct pushes to main/master branch
- **What else counts as "safe defaults"?** Need to research this

## Resources to Review

### Tools & Projects
- [github/safe-settings](https://github.com/github/safe-settings) - GitHub's own safe settings app
- [repository-settings/app](https://github.com/repository-settings/app) - Repository settings automation
- [myzkey/gh-repo-settings](https://github.com/github/myzkey/gh-repo-settings) - CLI tool for repo settings
- [Terraform GitHub Provider](https://registry.terraform.io/providers/integrations/github/latest/docs/resources/repository) - IaC approach

### Documentation
- [About Rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets) - Modern branch/tag/push protection (recommended over classic)

---

## Research Findings: Safe Defaults for Personal GitHub Repos

### 1. Authentication & Access Control

#### Two-Factor Authentication (2FA)
- **Critical first step** for account security
- Prefer hardware security keys or passkeys over SMS/TOTP apps
- SMS and TOTP are vulnerable to phishing
- Always have at least 2 second-factor credentials registered (backup)

#### SSH Keys & Personal Access Tokens (PATs)
- Rotate regularly (not just set once and forget)
- Use SSH key passphrases
- Consider hardware security keys for SSH
- Limit PAT scope and set expiration dates
- Never hardcode in repositories

### 2. Branch Protection: Understanding Your Options

**CRITICAL: Most features require paid plans for private repos!**

GitHub offers two ways to protect branches: **Classic Branch Protection Rules** (older) and **Rulesets** (newer). However, for personal accounts with private repos, your options are severely limited unless you pay for GitHub Pro.

#### Free Plan Limitations (Personal Account)

**What you get for FREE on private repos:**
- ❌ **NO branch protection** of any kind
- ❌ **NO rulesets**
- ❌ **NO required PR reviews**
- ❌ **NO status check requirements**
- ✅ You can manually follow good practices, but nothing is enforced

**What you get for FREE on public repos:**
- ✅ **Full branch protection** (classic rules)
- ✅ **Full rulesets**
- ✅ Everything works

**This means**: If you're using private repos on a free personal account, you cannot automate safe defaults for branch protection. You'd need to upgrade to GitHub Pro ($4/month) or make your repos public.

#### Paid Plan Requirements

| Feature | Free (Public) | Free (Private) | Pro (Private) |
|---------|---------------|----------------|---------------|
| Classic Branch Protection | ✅ Yes | ❌ No | ✅ Yes |
| Rulesets | ✅ Yes | ❌ No | ✅ Yes |
| Required PR Reviews | ✅ Yes | ❌ No | ✅ Yes |
| Required Status Checks | ✅ Yes | ❌ No | ✅ Yes |
| Push Rulesets | ❌ No* | ❌ No | ✅ Yes |

*Push rulesets require GitHub Team ($4/user/month)

#### Decision Tree: Which Protection Approach?

```
START: Do you have private repos?
├─ NO (all public repos)
│  └─ Use Rulesets (modern, recommended)
│
└─ YES (have private repos)
   ├─ Are you on GitHub Pro/Team/Enterprise?
   │  ├─ YES
   │  │  └─ Use Rulesets (modern, full features)
   │  │
   │  └─ NO (free plan)
   │     ├─ Can you make repos public?
   │     │  ├─ YES
   │     │  │  └─ Make repos public → Use Rulesets
   │     │  │
   │     │  └─ NO (must stay private)
   │     │     └─ ⚠️  NO AUTOMATION POSSIBLE
   │     │        Options:
   │     │        1. Upgrade to Pro ($4/month)
   │     │        2. Use manual processes
   │     │        3. Move to public repos
```

#### Classic Branch Protection Rules (Legacy, but works on Pro)

**Available on:**
- ✅ Free plan for **public repos**
- ✅ Pro/Team/Enterprise for **private repos**
- ❌ Free plan for **private repos**

**Essential Settings for main/master branch:**
- ✅ **Require pull request before merging** - Forces PR workflow, prevents direct pushes
- ✅ **Require status checks to pass** - Automated tests must pass before merge
- ✅ **Dismiss stale pull request approvals when new commits are pushed** - Re-review after changes
- ✅ **Require conversation resolution** - All PR comments must be resolved
- ✅ **Require signed commits** (optional but recommended) - GPG verification
- ✅ **Disable force pushes** - Prevent rewriting history (enabled by default)
- ✅ **Prevent branch deletion** - Protect main branch from accidental deletion (enabled by default)
- ⚠️ **Include administrators** - Apply rules to admins too (optional but safer)

**Limitations of Classic Protection:**
- Only one rule can apply to a branch at a time
- Rules can conflict and override each other
- Must delete rules to disable them (no status toggle)
- Less visible to non-admins
- Harder to manage with multiple protection needs

**API Endpoint:**
- `PUT /repos/{owner}/{repo}/branches/{branch}/protection`
- [Documentation](https://docs.github.com/en/rest/branches/branch-protection)

#### Rulesets (Modern Approach - Better but requires same paid plans)

**Available on:**
- ✅ Free plan for **public repos**
- ✅ Pro/Team/Enterprise for **private repos**
- ❌ Free plan for **private repos**

**What are Rulesets?**
- A named list of rules that applies to branches, tags, or pushes
- Can have up to 75 rulesets per repository
- More flexible and powerful than classic protection
- **Documentation**: [About Rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)

**Why Rulesets are Better than Classic Protection (when available):**
1. **Multiple rulesets can apply simultaneously** - Rules are aggregated, most restrictive wins
2. **Statuses** - Can be Active or Disabled without deleting
3. **Better visibility** - Anyone with read access can view active rulesets
4. **No conflicts** - Rules layer instead of override
5. **More control** - Metadata restrictions (commit messages, author emails)
6. **Better for automation** - Cleaner API, better for IaC

**Three Types of Rulesets:**

**#001 Branch Rulesets**
- Control how people interact with specific branches
- Use fnmatch patterns to target branches (e.g., `releases/**/*`)
- Can enforce: signed commits, PR reviews, status checks, etc.

**#002 Tag Rulesets**
- Control who can create, delete, or rename tags
- Prevent accidental tag deletion
- Protect release tags

**#003 Push Rulesets** ⚠️ *Requires GitHub Team or higher*
- Block pushes based on file characteristics
- Apply to entire repository and fork network
- Can restrict:
  - File paths (e.g., block changes to `config/production/**`)
  - File extensions (e.g., block `.exe`, `.dll`)
  - File sizes (e.g., block files >100MB)
  - File path lengths (e.g., max 255 characters)

**Ruleset Enforcement Statuses:**
- **Active**: Ruleset is enforced
- **Disabled**: Ruleset exists but not enforced (easy to re-enable later)

**Bypass Permissions:**
- Can allow certain users/teams to bypass rulesets
- Examples: Admins, specific GitHub Apps, deployment bots
- More granular than classic protection

**API Endpoints:**
- `POST /repos/{owner}/{repo}/rulesets` - Create ruleset
- `GET /repos/{owner}/{repo}/rulesets` - List all rulesets
- `PUT /repos/{owner}/{repo}/rulesets/{ruleset_id}` - Update ruleset
- `DELETE /repos/{owner}/{repo}/rulesets/{ruleset_id}` - Delete ruleset

#### Rulesets vs Classic Protection: Rule Layering

When both rulesets AND classic protection rules target the same branch:
- All rules are aggregated together
- The most restrictive version of each rule applies
- Example scenario:
  - Ruleset requires 3 PR reviews + signed commits
  - Classic rule requires 2 PR reviews + linear history
  - Result: 3 PR reviews + signed commits + linear history (most restrictive)

#### Recommendations for Personal Repos

**If you're on FREE plan:**
- ✅ **Public repos**: Use Rulesets (modern, full featured)
- ❌ **Private repos**: No automation possible - either upgrade to Pro or make repos public
- 💡 **Workaround**: Start projects public, make private later if needed (you lose protection when you do)

**If you're on GitHub Pro ($4/month):**
- ✅ Use Rulesets for both public and private repos
- ✅ You get all the modern features
- ✅ Automation makes sense

**If you're on FREE and staying FREE:**
- Focus automation on:
  - Security features (Dependabot, secret scanning for public repos)
  - Repository settings (features, visibility, etc.)
  - Creating SECURITY.md files
  - Setting up GitHub Actions configurations
- Accept that branch protection can't be automated for private repos
- Consider: Is the code really private? Many personal projects could be public

**Reality check for this blog idea:**
- Original goal: "Automate safe defaults for personal repos"
- Reality: Can't automate branch protection on free private repos
- Pivot options:
  1. Target only public repos (viable for OSS projects)
  2. Assume GitHub Pro (viable if you pay)
  3. Focus on non-protection settings (security, Actions, repo config)
  4. Document the limitations and let users decide

#### The Hard Truth

For most personal developers:
- You have private repos
- You don't want to pay $4/month for Pro
- **Therefore**: Branch protection automation is not available to you

This significantly limits what "safe defaults" you can actually automate. The automation would be useful for:
- Security settings (Dependabot, secret scanning on public repos)
- Repository configuration (disabling wiki, enabling issues, etc.)
- GitHub Actions settings (though protection is limited)
- Creating documentation files (SECURITY.md, etc.)

But the core security feature - preventing direct pushes to main - cannot be automated on private repos without paying.

### 3. GitHub Actions Security (THE BIG ONE!)

This is the most complex and dangerous area for personal repos.

#### The Fork Pull Request Problem
**Critical vulnerability**: Anyone can fork your public repo, add malicious workflow code, and open a PR. If your workflows run on `pull_request` trigger, their code executes automatically.

**Three approaches to mitigation:**

**Option 1: Require approval for fork PRs (RECOMMENDED)**
- Go to Settings → Actions → General
- Under "Fork pull request workflows from outside collaborators"
- Select: "Require approval for all outside collaborators"
- This makes ALL external PRs wait for manual approval before workflows run

**Option 2: Avoid dangerous triggers**
- NEVER use `pull_request_target` with code checkout from the PR
- `pull_request_target` runs with write permissions and secrets - extremely dangerous
- Only use `pull_request` trigger, which has read-only access and no secrets for forks

**Option 3: Use environment protection rules**
- Create protected environments for sensitive operations
- Require manual approval before accessing environment secrets
- More complex but granular control

#### GitHub Actions Secrets
- Secrets are NOT passed to workflows triggered from forks (by design)
- Anyone with write access can read repository secrets
- Use environment-level secrets for better control
- Consider using OpenID Connect instead of long-lived secrets for cloud access

#### Self-Hosted Runners
- **NEVER use self-hosted runners with public repositories**
- Even with private repos, be extremely cautious
- Runners are non-ephemeral by default (state persists between jobs)
- Anyone with read access who can fork and PR can compromise your infrastructure

#### Workflow Best Practices
- Set GITHUB_TOKEN to read-only by default
- Pin action versions to commit SHAs (not tags)
- Never reference untrusted inputs in inline scripts (script injection vulnerability)
- Keep actions up to date with Dependabot
- Monitor workflow runs for anomalies

### 4. Dependency Management

#### Dependabot
- **Enable Dependabot alerts** - Notifies about vulnerable dependencies
- **Enable Dependabot security updates** - Automatic PRs to fix vulnerabilities
- **Enable Dependabot version updates** - Keep dependencies current
- Works for public repos; private repos need GitHub Advanced Security

#### Dependency Graph
- Enable in Settings → Security & analysis → Dependency graph
- Required for Dependabot to work
- Shows all dependencies and their relationships

#### Dependency Review
- Visualize dependency changes in PRs before merging
- Helps catch new vulnerabilities before they enter codebase
- Available for all public repos; private repos need GitHub Pro+

### 5. Secret Scanning

#### Built-in Secret Scanning
- GitHub automatically scans public repos for known secret patterns
- Alerts if API keys, tokens, credentials detected
- For private repos, requires GitHub Advanced Security
- Use tools like `git-secrets` to prevent secrets from being committed

#### Best Practices
- Never commit secrets, even to private repos
- Use GitHub Secrets for CI/CD workflows
- Rewrite history if secrets are committed (use `git filter-branch`)
- Rotate any exposed credentials immediately

### 6. Code Scanning & Security Features

#### Code Scanning
- Static analysis to find security vulnerabilities
- GitHub's CodeQL available for public repos
- Can integrate third-party tools (Snyk, GitGuardian, etc.)
- Set up in Security tab or via GitHub Actions

#### Security Policy (SECURITY.md)
- Document how to report vulnerabilities
- Specify supported versions
- Describe disclosure policy
- Shows in Security tab and builds trust

### 7. Repository Settings

#### Visibility & Access
- Disable repository visibility changes by regular members
- Disable forking if you want to maintain control over source code
- Regularly audit access permissions
- Remove access for inactive contributors

#### Repository Features to Consider Disabling
- **Disable "Allow merge commits"** if you prefer squash or rebase
- **Disable "Allow rebase merging"** if you want linear history
- **Require linear history** - Prevents merge commits
- **Disable Wiki** if not needed (reduces attack surface)
- **Disable Projects** if not used
- **Disable Packages** if not used

### 8. Audit Logging

#### For Personal Accounts
- Limited compared to organizations
- Review Security log regularly (Settings → Security log)
- Monitor for: unusual logins, token creation, permission changes
- Set up email notifications for important events

### 9. Third-Party Integrations

#### OAuth Apps & GitHub Apps
- Regularly audit installed apps (Settings → Applications)
- Only install trusted applications
- Follow principle of least privilege
- Remove unused integrations
- Review permissions carefully before granting

---

## Safe Defaults Summary Checklist

### Account-Level
- [ ] Enable 2FA with hardware key/passkey
- [ ] Set up backup 2FA method
- [ ] Use PATs instead of passwords
- [ ] Rotate SSH keys and PATs regularly

### Repository-Level: Security Tab
- [ ] Enable Dependency graph
- [ ] Enable Dependabot alerts
- [ ] Enable Dependabot security updates
- [ ] Enable Secret scanning (if available)
- [ ] Enable Code scanning (CodeQL or similar)
- [ ] Add SECURITY.md file

### Repository-Level: Settings
- [ ] Set up branch protection on main/master ⚠️ **Requires Pro for private repos**
  - **IF on FREE plan**: Only works for public repos
  - **IF on Pro/Team/Enterprise**: Works for all repos
  - **Modern approach**: Use Rulesets (if available)
  - **Legacy approach**: Use Classic Branch Protection (if available)
  - Require pull requests
  - Require status checks
  - Dismiss stale reviews
  - Include administrators (optional)
  - Disable force push
  - Disable deletion
- [ ] Configure GitHub Actions settings
  - Require approval for fork PRs
  - Set GITHUB_TOKEN to read-only default
  - Never use self-hosted runners (public repos)
- [ ] Disable unused features (wiki, projects, etc.)
- [ ] Restrict who can change repository visibility

### Workflow-Level (if using Actions)
- [ ] Pin actions to commit SHAs
- [ ] Use OIDC instead of long-lived secrets
- [ ] Avoid `pull_request_target` trigger
- [ ] Never checkout fork code with write permissions
- [ ] Use environment protection for sensitive operations
- [ ] Keep actions updated with Dependabot

---

## Deep Dive: GitHub App Solution for Personal Accounts

### What is a GitHub App?

A GitHub App is an official integration type that can authenticate and interact with GitHub APIs on behalf of itself (not a user). Think of it as a "service account" but better - it's a first-class GitHub citizen with its own identity, permissions, and authentication mechanism.

**Key Concept**: Unlike a Personal Access Token (PAT) that represents YOU, a GitHub App represents an AUTOMATION or SERVICE.

### Why Use a GitHub App Instead of PAT?

**Comparison Table:**

| Feature | Personal Access Token | GitHub App |
|---------|----------------------|------------|
| **Ownership** | Tied to your user account | Independent entity |
| **Lifecycle** | Breaks if you leave/get removed | Persists independently |
| **Permissions** | All-or-nothing scopes | Fine-grained, repo-specific |
| **Token Expiration** | 7-90 days (or never) | 1 hour (auto-renewable) |
| **Security** | If leaked = full account access | If leaked = limited blast radius |
| **Attribution** | Shows as you in logs | Shows as the app |
| **Repository Access** | All repos you can access | Only repos where it's installed |
| **Setup Complexity** | Easy (2 minutes) | Moderate (15-30 minutes) |
| **Best For** | Personal scripts, one-off tasks | Long-running automation, teams |

### How GitHub Apps Work: The Authentication Flow

**#001 Create the GitHub App**
- You register the app in your GitHub settings
- Define what permissions it needs (read/write repos, manage settings, etc.)
- GitHub gives you:
  - **App ID**: A number that identifies your app
  - **Private Key**: A PEM file used to sign authentication requests

**#002 Install the GitHub App**
- You "install" the app on your personal account or organization
- During installation, you choose which repos it can access
- GitHub gives you:
  - **Installation ID**: A number identifying this specific installation

**#003 Generate Installation Access Token**
- Your automation uses the App ID + Private Key to create a JWT (JSON Web Token)
- Exchange the JWT for an **Installation Access Token**
- This token is valid for 1 hour and has the permissions you configured
- Use this token to make API calls

**#004 Token Auto-Renewal**
- When the 1-hour token expires, generate a new one
- This happens programmatically - no human intervention needed
- Libraries like Octokit handle this automatically

### Can You Use GitHub Apps with a Personal Account?

**YES!** This is a common misconception. GitHub Apps work with:
- ✅ Personal accounts (your individual account)
- ✅ Organization accounts
- ✅ Multiple accounts simultaneously

**For Personal Account Use Case:**
1. Create app under your personal account settings
2. Install it on your personal account
3. Grant access to specific repos (or all repos)
4. Use it in your automation scripts

**Important**: You can make the app **private** (only you can install it) or **public** (others can install it). For personal automation, always choose private.

### Setting Up a GitHub App for Personal Repo Automation

**Step-by-Step Process:**

**#001 Create the GitHub App**
```
1. Go to: https://github.com/settings/apps
2. Click "New GitHub App"
3. Fill in:
   - Name: "My Repo Automation" (or whatever)
   - Homepage URL: Your GitHub profile or repo URL
   - Webhook: Uncheck "Active" (not needed for scripts)
   - Permissions: Select what you need (see below)
   - Where can this app be installed: "Only on this account"
4. Click "Create GitHub App"
```

**#002 Permissions Needed for Safe Defaults Automation**
- **Repository permissions**:
  - Administration: Read & write (for branch protection, repo settings)
  - Contents: Read & write (for creating SECURITY.md)
  - Metadata: Read (always required)
  - Security events: Read & write (for enabling security features)
  - Actions: Read & write (for configuring Actions settings)
  - Dependabot alerts: Read & write
  - Workflows: Read & write (if modifying .github/workflows)

**#003 Generate Private Key**
```
1. Scroll to "Private keys" section
2. Click "Generate a private key"
3. A .pem file downloads - SAVE THIS SECURELY
4. Store it somewhere safe (not in git!)
```

**#004 Install the App**
```
1. Go to: https://github.com/settings/installations
   OR click "Install App" in the app settings
2. Select your personal account
3. Choose:
   - "All repositories" (easiest for automation)
   - OR "Only select repositories" (more secure)
4. Click "Install"
5. Note the Installation ID in the URL: 
   github.com/settings/installations/XXXXXXXX
```

### Using the GitHub App in Scripts

**Simple Example (Bash + gh CLI):**

The `gh` CLI doesn't directly support GitHub App authentication, so you need to generate the token first.

**Option A: Use a helper tool**
```bash
# Install GitHub App token generator
npm install -g github-app-installation-token

# Generate token
export GH_TOKEN=$(github-app-installation-token \
  --appId YOUR_APP_ID \
  --installationId YOUR_INSTALLATION_ID \
  --privateKey "$(cat path/to/private-key.pem)")

# Now use gh CLI as normal
gh api repos/OWNER/REPO/branches/main/protection
```

**Option B: Manual token generation (more complex)**
```bash
#!/bin/bash
# This is pseudocode - actual implementation requires JWT generation

# 1. Generate JWT using App ID + Private Key
# 2. Exchange JWT for Installation Token
# 3. Use Installation Token with GitHub API

# Libraries exist for this in Python, Node.js, Ruby, etc.
```

**Python Example (using PyGithub):**

```python
from github import GithubIntegration
import os

# Load credentials
APP_ID = "123456"
PRIVATE_KEY = open('/path/to/private-key.pem', 'r').read()
INSTALLATION_ID = "78910"

# Authenticate as GitHub App
git_integration = GithubIntegration(APP_ID, PRIVATE_KEY)

# Get installation access token
installation = git_integration.get_installation(INSTALLATION_ID)
access_token = installation.get_access_token()

# Use the token
from github import Github
g = Github(access_token.token)

# Now apply safe defaults
repo = g.get_repo("username/repo-name")

# Enable branch protection
repo.get_branch("main").edit_protection(
    required_approving_review_count=1,
    dismiss_stale_reviews=True,
    require_code_owner_reviews=False,
    enforce_admins=False
)

# Enable vulnerability alerts
repo.enable_vulnerability_alert()

print(f"✅ Applied safe defaults to {repo.name}")
```

**Node.js Example (using Octokit):**

```javascript
const { createAppAuth } = require("@octokit/auth-app");
const { Octokit } = require("@octokit/rest");
const fs = require('fs');

// Load credentials
const appId = 123456;
const privateKey = fs.readFileSync('./private-key.pem', 'utf-8');
const installationId = 78910;

// Create authenticated Octokit instance
const octokit = new Octokit({
  authStrategy: createAppAuth,
  auth: {
    appId,
    privateKey,
    installationId
  }
});

// Apply safe defaults
async function applyDefaults(owner, repo) {
  // Enable branch protection
  await octokit.repos.updateBranchProtection({
    owner,
    repo,
    branch: 'main',
    required_status_checks: null,
    enforce_admins: true,
    required_pull_request_reviews: {
      required_approving_review_count: 1,
      dismiss_stale_reviews: true
    },
    restrictions: null
  });
  
  // Enable vulnerability alerts
  await octokit.repos.enableVulnerabilityAlerts({
    owner,
    repo
  });
  
  console.log(`✅ Applied safe defaults to ${owner}/${repo}`);
}

applyDefaults('username', 'repo-name');
```

### Advantages of GitHub App for Personal Account

**#001 Security**
- Short-lived tokens (1 hour) reduce risk if leaked
- Fine-grained permissions - app only gets what it needs
- Can't accidentally give full account access
- Separate from your personal credentials

**#002 Durability**
- Doesn't break if you change password
- Doesn't expire unexpectedly (tokens auto-renew)
- Not tied to employment status
- Survives account security changes

**#003 Auditability**
- Actions clearly attributed to the app, not you
- Easy to see what the automation did
- Can revoke app access instantly
- Separate from human activity in logs

**#004 Flexibility**
- Same app can be installed on multiple accounts
- Can restrict to specific repos
- Easy to update permissions without regenerating credentials
- Can be used by multiple scripts/machines

### Disadvantages of GitHub App for Personal Account

**#001 Initial Complexity**
- More setup steps than PAT (15-30 min vs 2 min)
- Need to understand JWT/token exchange
- Requires managing private key file securely
- More moving parts that can break

**#002 Code Complexity**
- Can't just use `gh` CLI out of the box
- Need libraries or token generation logic
- More error handling required
- Debugging is harder

**#003 Overkill for Simple Use Cases**
- If running one script once, PAT is easier
- If only you will ever use it, PAT is simpler
- If you're comfortable with manual rotation, PAT works
- Personal projects may not need this level of security

### When to Use GitHub App vs PAT

**Use GitHub App when:**
- ✅ Building automation for multiple repos
- ✅ Want to enforce safe defaults across repos
- ✅ Plan to run scripts long-term (months/years)
- ✅ Want better security (short-lived tokens)
- ✅ Working in a team or might share the automation
- ✅ Want clear attribution separate from your account
- ✅ Need fine-grained, repo-specific permissions

**Use PAT when:**
- ✅ One-off script or task
- ✅ Personal project, only you use it
- ✅ Comfortable manually rotating tokens
- ✅ Quick prototype or testing
- ✅ Using `gh` CLI interactively
- ✅ Don't want the setup overhead

### Recommended Approach for This Project

**For personal repo automation (this blog idea):**

**Phase 1: Start with PAT**
- Build the automation script using PAT
- Test it, make it work
- Iterate on the settings and logic
- Prove the concept

**Phase 2: Convert to GitHub App (optional)**
- Once stable, create GitHub App
- Refactor script to use App authentication
- Benefits: Better security, long-term durability
- Cost: One-time setup effort

**Why this approach:**
- PAT is faster to prototype with
- GitHub App is better for production
- Can decide later if App is worth the effort
- Not locked in - easy to switch

### Alternative: Hybrid Approach

**Script that supports both:**
```python
def get_github_client():
    if os.getenv('USE_GITHUB_APP') == 'true':
        # Use GitHub App authentication
        return github_app_auth()
    else:
        # Use PAT
        return Github(os.getenv('GITHUB_TOKEN'))
```

This gives flexibility to use whichever auth method fits the situation.

---

## Implementation Strategy Options

### Option 1: GitHub CLI + Script
Pros: Simple, works for personal accounts, no special permissions
Cons: Need to write/maintain script

### Option 2: Terraform
Pros: Declarative, version controlled, reproducible
Cons: Learning curve, overkill for personal use?

### Option 3: GitHub Apps (github/safe-settings)
Pros: Official GitHub solution, comprehensive
Cons: Designed for organizations, may be overkill

### Option 4: Template Repository
Pros: Simple, built-in GitHub feature
Cons: Only copies files/structure, not settings

**Recommendation**: Start with GitHub CLI + script for personal repos. Can create a "template" script that sets all the safe defaults via GitHub API.

---

---

## GitHub CLI Scripts & Tools Found

### Existing Tools

**#001 [twelvelabs/gh-repo-config](https://github.com/twelvelabs/gh-repo-config)** - GitHub CLI Extension
- Declarative configuration via JSON files
- Manages: repo settings, topics, branch protection
- Workflow: `gh repo-config init` → edit JSON → `gh repo-config apply`
- Stores config in `.github/config/` directory
- Pros: Declarative, version controlled
- Cons: Requires pre-existing repo, must be run per-repo

**#002 [katiem0/gh-branch-rules](https://github.com/katiem0/gh-branch-rules)** - GitHub CLI Extension
- List and update branch protection rules
- Can work across multiple repos in an org
- CSV export/import functionality
- Good for auditing existing rules
- Pros: Bulk operations, reporting
- Cons: Organization-focused, not for initial setup

**#003 [cgpu/add-branch-protection-rules](https://github.com/cgpu/add-branch-protection-rules)** - Bash Script
- Uses GraphQL API via `gh api graphql`
- Creates branch protection rules programmatically
- Accepts parameters for settings
- Example: `./createBranchProtectionRule.sh github.com $ORG $REPO main requiresApprovingReviews=true ...`
- Pros: Simple, parameterized
- Cons: Only handles branch protection

### Key GitHub CLI Patterns

**Pattern 1: REST API via `gh api`**
```bash
# Get branch protection
gh api repos/:owner/:repo/branches/main/protection

# Set branch protection
gh api --method PUT repos/:owner/:repo/branches/main/protection \
  --input config.json
```

**Pattern 2: GraphQL API via `gh api graphql`**
```bash
# Query branch protection rules
gh api graphql -f query='
  query($owner: String!, $repo: String!) {
    repository(owner: $owner, name: $repo) {
      branchProtectionRules(first: 100) {
        nodes { pattern requiresApprovingReviews }
      }
    }
  }' -F owner=$OWNER -F repo=$REPO
```

**Pattern 3: Built-in `gh repo` commands**
```bash
# Create repo with some settings
gh repo create my-repo --private --description "My repo"

# Edit repo settings (limited options)
gh repo edit --enable-issues --enable-wiki=false
```

### API Endpoints for Safe Defaults

**Repository Settings**
- `PUT /repos/{owner}/{repo}` - Update repo (description, visibility, features)
- `gh api repos/:owner/:repo --method PATCH -f has_issues=true`

**Branch Rulesets (Modern - RECOMMENDED)**
- `POST /repos/{owner}/{repo}/rulesets` - Create ruleset
- `GET /repos/{owner}/{repo}/rulesets` - List all rulesets
- `GET /repos/{owner}/{repo}/rulesets/{ruleset_id}` - Get specific ruleset
- `PUT /repos/{owner}/{repo}/rulesets/{ruleset_id}` - Update ruleset
- `DELETE /repos/{owner}/{repo}/rulesets/{ruleset_id}` - Delete ruleset
- Better for IaC and automation
- No conflicts - rules layer together
- Can be Active or Disabled
- [Rulesets Documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)

**Branch Protection (Classic - Legacy)**
- `PUT /repos/{owner}/{repo}/branches/{branch}/protection`
- Requires JSON payload with all settings
- Older approach, still supported but rulesets preferred
- [Full documentation](https://docs.github.com/en/rest/branches/branch-protection)

**Security Settings**
- Enable Dependabot: `PUT /repos/{owner}/{repo}/vulnerability-alerts`
- Enable secret scanning: Repository-level settings API
- Note: Many security features are org-level for private repos

**Actions Settings**
- `PUT /repos/{owner}/{repo}/actions/permissions`
- Configure fork PR approval requirements
- Set default GITHUB_TOKEN permissions

### Script Approaches Discovered

**Approach 1: Single comprehensive script**
- One script that does everything
- Accepts repo name as parameter
- Applies all safe defaults in sequence
- Good for: New repo creation workflow

**Approach 2: Modular scripts**
- Separate scripts for: branch protection, security, repo settings
- Can apply individually
- Good for: Updating existing repos, flexibility

**Approach 3: GitHub Actions workflow**
- Self-healing: Automatically enforces settings
- Triggers on push to main or on schedule
- Ensures settings don't drift
- Good for: Continuous enforcement

**Approach 4: Template repository + setup script**
- Template provides files (.github/workflows, SECURITY.md)
- Setup script applies settings via API
- Good for: Consistent repo structure + settings

### Authentication for Scripts

**Method 1: GitHub CLI authentication**
```bash
gh auth login  # Interactive
# or
export GH_TOKEN="ghp_..."  # Environment variable
```

**Method 2: Personal Access Token (PAT)**
```bash
# Classic PAT needs: repo, workflow scopes
# Fine-grained PAT needs: repo administration, actions, security events
gh api --method PUT ... -H "Authorization: token $PAT"
```

**Method 3: GitHub App (advanced)**
- Create GitHub App with required permissions
- Generate installation token
- Use for automation without PAT
- More secure for long-running automation

### Limitations Found

**Personal Account Limitations**
- ❌ Can't enforce settings across multiple repos automatically
- ❌ No organization-level security configurations
- ❌ Branch protection on private repos requires Pro plan
- ❌ Advanced security features require paid plans

**API Limitations**
- Some settings not available via API (Actions approval settings)
- Repository secrets can't be read via API (only set)
- Branch rulesets API is newer, less documented than classic protection

**Script Challenges**
- Must handle repo that doesn't exist yet vs. existing repo
- Settings validation difficult (API often returns 200 even on partial failure)
- Some settings depend on others (can't require status checks without checks)
- JSON payloads complex and version-specific

---

## Project Scope Reality Check

Given the research findings, here's the honest assessment of what's automatable:

### What You CAN Automate (Free Plan)

**For Public Repos:**
- ✅ Branch protection (classic or rulesets)
- ✅ Required PR reviews
- ✅ Status check requirements
- ✅ Dependabot alerts
- ✅ Secret scanning
- ✅ Code scanning (CodeQL)
- ✅ Repository settings (wiki, issues, etc.)
- ✅ GitHub Actions configurations
- ✅ Creating SECURITY.md

**For Private Repos:**
- ✅ Repository settings (wiki, issues, projects, etc.)
- ✅ GitHub Actions basic configuration
- ✅ Creating SECURITY.md and other files
- ❌ Branch protection (requires Pro)
- ❌ Dependabot (requires Advanced Security)
- ❌ Secret scanning (requires Advanced Security)
- ❌ Code scanning (requires Advanced Security)

### What This Means for the Original Idea

**Original goal**: "Quickly create GitHub repos with safe defaults for personal account"

**Revised realistic goals:**

**Option A: Target Public Repos**
- Build automation for open source projects
- All security features available
- Full branch protection
- Most useful for developers who share their code

**Option B: Target Paid Accounts**
- Assume user has GitHub Pro ($4/month)
- Build full-featured automation
- Document that it requires Pro
- Best for professional developers

**Option C: Focus on Universal Settings**
- Skip branch protection entirely
- Automate what works on free private repos:
  - Repository configuration (features, visibility)
  - File creation (SECURITY.md, README templates)
  - Basic Actions setup
  - Documentation and structure
- Less impactful but universally applicable

**Option D: Hybrid Approach**
- Detect repo visibility and plan level
- Apply settings based on what's available
- Inform user of limitations
- Offer upgrade suggestions

### Recommended Implementation Strategy

**Build in phases:**

**Phase 1: Universal Settings (works everywhere)**
- Repository feature configuration
- File creation (SECURITY.md, etc.)
- Basic Actions setup
- Works for all users, all repo types

**Phase 2: Public Repo Features**
- Add branch protection
- Add security scanning setup
- Detects public repos and applies accordingly

**Phase 3: Paid Plan Features**
- Detects Pro/Team/Enterprise
- Enables all features for private repos
- Documents upgrade benefits

This way:
- Free users get value immediately (Phase 1)
- Public repo users get full protection (Phase 2)
- Paid users get everything (Phase 3)
- Clear documentation of limitations

---

## Next Steps
1. ~~Research what constitutes "safe defaults" for personal GitHub repos~~ ✅
2. ~~Search for existing GitHub CLI scripts/tools~~ ✅
3. Design script architecture (modular vs. monolithic)
4. Build proof of concept with GitHub CLI
5. Test with new repo creation
6. Create reusable automation tool
7. Document the process

## Notes
- Focus is on personal account (no org features)
- Want this to be quick/automated - not manual clicking through settings
- GitHub Actions security is surprisingly complex for public repos
- **CRITICAL LIMITATION**: Branch protection NOT available on free plan for private repos
- Many "safe defaults" require paid plans for private repos
- Automation is most useful for:
  - Public repos (all features available for free)
  - Paid accounts (Pro at $4/month unlocks everything)
  - Non-protection settings (repo config, security features for public repos)
- **Realistic scope for free personal account**: Focus on public repos or non-protection automation
