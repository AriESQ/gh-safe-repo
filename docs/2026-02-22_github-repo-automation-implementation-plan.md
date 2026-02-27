# GitHub Repository Automation - Implementation Plan

**Date**: 2026-02-22  
**Status**: Planning Phase  
**Related**: [Research Document](2026-02-07_github-safe-repo-defaults.md)

## Executive Summary

Build automation to quickly create GitHub repositories with safe defaults for a personal account, supporting two workflows:
1. **Private repo creation** (90% of use cases) - for initial development
2. **Public repo creation from private** (10% of use cases) - when ready to share/accept contributions

## Use Case & Workflow

### Current Pain Points
- Manually clicking through GitHub settings for each new repo
- Forgetting security configurations
- Inconsistent settings across repos
- Risk when making repos public (security gap, exposed secrets)

### Target User
- Personal GitHub account (not organization)
- Primarily uses private repos for initial development
- Occasionally makes repos public for contributions
- Currently uses SSH credentials for git operations
- May adopt GitHub CLI if needed

### Typical Workflow

**Stage 1: Private Development (90% of repos)**
```bash
$ create-repo my-new-project
Creating private repository 'my-new-project'...
✓ Repository created
✓ Applied safe defaults
✓ Created SECURITY.md
✓ Configured GitHub Actions
→ Repository ready at: https://github.com/username/my-new-project
```

**Stage 2: Go Public (10% of repos)**
```bash
$ create-public-repo my-public-project --from my-new-project
🔍 Running pre-flight security scan...
  ✓ No hardcoded secrets found
  ✓ No sensitive usernames/emails
  ✓ No files >100MB
  ⚠ Warning: 3 TODO comments found (review recommended)
  
Continue? (y/n): y

Creating public repository 'my-public-project'...
✓ Public repository created
✓ Applied public safety defaults (branch protection, fork PR approval)
✓ Copied code from private repo
✓ Original private repo preserved
→ Public repository ready at: https://github.com/username/my-public-project
```

## Requirements

### Functional Requirements

**FR-1: Private Repo Creation**
- Create new private repository
- Apply safe defaults (see Configuration section)
- Work with free GitHub plan
- No branch protection (not available on free private repos)

**FR-2: Public Repo Creation**
- Create new public repository (separate from private)
- Apply all available safety features (branch protection, etc.)
- Copy code from specified private repo
- Keep private repo intact

**FR-3: Pre-flight Security Scan**
- Run LOCALLY (not in GitHub Actions to avoid leaking scan rules)
- Scan for:
  - Hardcoded secrets (API keys, tokens, passwords)
  - Usernames and email addresses
  - File paths that might expose system info
  - Large files (>100MB recommended, configurable)
  - TODO/FIXME comments that might contain sensitive info
- Interactive: show findings, allow user to abort

**FR-4: Configuration Management**
- `.ini` config file for all defaults
- Human-readable format
- Easy to understand what's being configured
- Override defaults via command-line flags

**FR-5: Authentication**
- Work with existing SSH credentials
- Support GitHub CLI authentication if available
- Consider GitHub App for better security (TBD)

### Non-Functional Requirements

**NFR-1: Safety First**
- No window where public repo exists without protection
- Pre-flight checks before exposing code
- Fail safely (abort rather than misconfigure)

**NFR-2: Transparency**
- Show exactly what's being configured
- Confirm before destructive operations
- Log all API calls (optional debug mode)

**NFR-3: Simplicity**
- Minimal dependencies
- Work on macOS, Linux, Windows
- Single command to run
- No complex setup

## Configuration

### Settings to Automate

Based on [research findings](2026-02-07_github-safe-repo-defaults.md), here's what we can configure:

#### Universal Settings (Private & Public, Free & Paid)

**Repository Features:**
- Description
- Homepage URL
- Topics/tags
- Issues (enable/disable)
- Wiki (disable by default)
- Projects (disable by default)
- Discussions (disable by default)
- Preserve history on deletion
- Allow auto-merge
- Delete head branches after merge

**GitHub Actions:**
- Default GITHUB_TOKEN permissions (read-only recommended)
- Allow GitHub Actions (yes/no)
- Allow workflows from forks (with approval)

**Files to Create:**
- SECURITY.md (template)
- .gitignore (language-specific)
- README.md (basic template, optional)

#### Public Repo Only Settings (Free)

**Branch Protection/Rulesets:**
- Require pull request before merging
- Require status checks (if CI configured)
- Dismiss stale reviews on new commits
- Require conversation resolution
- Disable force pushes
- Prevent branch deletion
- Include administrators in restrictions

**Fork PR Security:**
- Require approval for fork pull requests from outside collaborators
- Setting: "Require approval for all outside collaborators"

**Security Features:**
- Enable Dependabot alerts
- Enable secret scanning (automatic for public repos)
- Enable code scanning (optional, requires setup)

#### Paid Plan Only Settings (Pro/Team/Enterprise)

**Private Repo Branch Protection:**
- Same as public repo settings
- Only available with GitHub Pro ($4/month)

**Advanced Security:**
- Dependabot for private repos
- Secret scanning for private repos
- Code scanning for private repos

### Sample Configuration File

```ini
# GitHub Repository Automation Configuration
# Location: ~/.github-repo-automation/config.ini

[repository]
# Default visibility (private or public)
default_visibility = private

# Repository features
enable_issues = true
enable_wiki = false
enable_projects = false
enable_discussions = false
delete_branch_on_merge = true
allow_auto_merge = false

[actions]
# GitHub Actions configuration
default_token_permissions = read
allow_fork_workflows = true
require_fork_approval = all_outside_collaborators

[branch_protection]
# Only applied to public repos (not available for free private repos)
require_pull_request = true
required_approving_reviews = 1
dismiss_stale_reviews = true
require_conversation_resolution = true
enforce_admins = false
allow_force_pushes = false
allow_deletions = false

[security]
# Security scanning (public repos only on free plan)
enable_dependabot_alerts = true
enable_secret_scanning = true
enable_code_scanning = false  # Requires manual setup

[pre_flight_scan]
# Run before making repo public
scan_for_secrets = true
scan_for_emails = true
scan_for_usernames = true
max_file_size_mb = 100
scan_todos = true
# Patterns to scan for (beyond defaults)
custom_patterns = 

[files]
# Files to create in new repos
create_security_md = true
create_gitignore = true
gitignore_template = python  # language name or path to custom
create_readme = false

[api]
# API configuration
timeout_seconds = 30
retry_attempts = 3
debug_mode = false
```

## Implementation Options

### Option 1: Bash Script + GitHub CLI

**Approach:**
- Bash script that wraps `gh` CLI commands
- Use `gh api` for settings not covered by `gh repo` commands
- Leverage existing `gh auth` for credentials

**Pros:**
- Simple to write and understand
- Works anywhere bash + gh CLI works
- No compilation needed
- Easy to debug

**Cons:**
- Bash portability issues (macOS vs Linux vs Windows)
- JSON manipulation is clunky in bash
- Error handling is verbose
- Not great for complex logic (pre-flight scan)

**Tech Stack:**
- Bash 4.0+
- GitHub CLI (`gh`)
- `jq` for JSON parsing
- Standard Unix tools (grep, awk, sed)

**Example:**
```bash
#!/bin/bash
# gh-safe-repo

source ~/.github-repo-automation/config.ini

REPO_NAME=$1
VISIBILITY=${2:-private}

# Create repo
gh repo create "$REPO_NAME" --"$VISIBILITY" --description "..."

# Configure settings via API
gh api --method PATCH repos/:owner/"$REPO_NAME" \
  -f has_wiki=false \
  -f has_projects=false

# If public, set up branch protection
if [ "$VISIBILITY" = "public" ]; then
  gh api --method PUT repos/:owner/"$REPO_NAME"/branches/main/protection \
    --input branch-protection.json
fi
```

### Option 2: Python Script + PyGithub

**Approach:**
- Python script using PyGithub library
- Handles authentication, API calls, JSON parsing
- Can include sophisticated pre-flight scanning

**Pros:**
- Cross-platform (Windows, macOS, Linux)
- Excellent libraries (PyGithub, GitPython)
- Easy JSON/config handling
- Great for complex logic (regex, file scanning)
- Good error handling

**Cons:**
- Requires Python 3.8+
- Dependencies to install
- Slightly heavier than bash

**Tech Stack:**
- Python 3.8+
- PyGithub (GitHub API)
- GitPython (local git operations)
- configparser (INI files)
- click or argparse (CLI)

**Example:**
```python
#!/usr/bin/env python3
from github import Github
import configparser

def create_private_repo(name, config):
    g = Github(get_token())
    user = g.get_user()
    
    repo = user.create_repo(
        name=name,
        private=True,
        has_issues=config.getboolean('repository', 'enable_issues'),
        has_wiki=config.getboolean('repository', 'enable_wiki'),
        # ... more settings
    )
    
    # Configure Actions
    repo.edit(
        default_branch_permission='read',
        # ... more settings
    )
    
    return repo
```

### Option 3: GitHub CLI Extension (Go)

**Approach:**
- Build as official `gh` extension
- Written in Go (GitHub CLI's language)
- Install via `gh extension install`

**Pros:**
- Native integration with `gh`
- Fast, compiled binary
- Cross-platform (gh handles it)
- Feels like native `gh` command
- Can use `gh` authentication seamlessly

**Cons:**
- Requires learning Go
- More complex build/distribution
- Heavier development process

**Tech Stack:**
- Go 1.19+
- GitHub CLI API
- `go-github` library
- `viper` for config

**Example:**
```go
// gh-safe-repo
package main

import (
    "github.com/cli/go-gh"
    "github.com/cli/go-gh/pkg/api"
)

func main() {
    client, _ := gh.RESTClient(nil)
    
    // Create repo
    client.Post("user/repos", map[string]interface{}{
        "name": repoName,
        "private": true,
        "has_wiki": false,
    })
}
```

### Option 4: GitHub App

**Approach:**
- Create private GitHub App
- Install on personal account
- Web UI or CLI to trigger operations
- App handles authentication

**Pros:**
- Best security (short-lived tokens)
- Fine-grained permissions
- Not tied to personal credentials
- Can revoke access easily

**Cons:**
- Significant setup overhead
- Requires managing private key
- More complex authentication flow
- Overkill for personal use?

**When to use:**
- If you want maximum security
- If you plan to share tool with others
- If you want long-term maintenance

### Option 5: Node.js + Octokit

**Approach:**
- Node.js script using Octokit library
- npm package for easy installation
- Can be interactive (inquirer.js)

**Pros:**
- JavaScript ecosystem
- Excellent GitHub libraries
- Easy async/await for API calls
- Good for interactive prompts

**Cons:**
- Requires Node.js
- npm dependencies
- JavaScript (if you prefer Python/Go)

**Tech Stack:**
- Node.js 16+
- Octokit (@octokit/rest)
- Inquirer.js (interactive prompts)
- ini parser

## Recommended Approach

### Primary Recommendation: Python Script

**Why Python:**
1. ✅ Cross-platform compatibility
2. ✅ Excellent libraries (PyGithub is mature and well-documented)
3. ✅ Easy to implement complex logic (pre-flight scanning with regex)
4. ✅ Good configuration parsing (configparser for INI)
5. ✅ Most developers have Python installed
6. ✅ Easy to read and maintain
7. ✅ Can package as standalone executable later (PyInstaller)
8. ✅ **Works with all authentication methods** (PAT, gh CLI, GitHub App)

**Alternative: Bash + gh CLI**
- ✅ If you prefer bash and already use gh CLI
- ✅ Simpler for basic operations
- ⚠️ Harder for complex logic (pre-flight scanning)
- ⚠️ Limited to PAT or gh CLI auth (GitHub App is complex in bash)
- ⚠️ JSON manipulation is clunky

**Not Tied to Implementation:**
The authentication method doesn't force your language choice:
- Python works with: PAT ✅, gh CLI ✅, GitHub App ✅
- Bash works with: PAT ✅, gh CLI ✅, GitHub App ⚠️ (hard)
- Go works with: PAT ✅, gh CLI ✅, GitHub App ✅
- Node.js works with: PAT ✅, gh CLI ✅, GitHub App ✅

**Recommendation: Start with Python + PAT/gh CLI, you can always add GitHub App support later.**

**Structure:**
```
github-repo-automation/
├── gh-safe-repo              # Main executable script
├── config.ini.example        # Example configuration
├── lib/
│   ├── __init__.py
│   ├── repo_creator.py       # Repo creation logic
│   ├── security_scanner.py   # Pre-flight scanning
│   ├── config_manager.py     # Config parsing
│   └── templates/            # File templates (SECURITY.md, etc.)
├── requirements.txt          # Python dependencies
├── README.md
└── tests/
    └── test_*.py             # Unit tests
```

**Installation:**
```bash
# Clone repo
git clone https://github.com/username/github-repo-automation
cd github-repo-automation

# Install dependencies
pip install -r requirements.txt

# Copy config template
cp config.ini.example ~/.github-repo-automation/config.ini

# Edit config
vim ~/.github-repo-automation/config.ini

# Make executable
chmod +x gh-safe-repo

# Optional: symlink to PATH
ln -s $(pwd)/gh-safe-repo /usr/local/bin/gh-safe-repo
```

**Usage:**
```bash
# Create private repo
gh-safe-repo my-project

# Create public repo from private
gh-safe-repo my-public-project --from my-private-project --public

# Override config
gh-safe-repo my-project --no-wiki --enable-discussions

# Debug mode
gh-safe-repo my-project --debug
```

### Authentication Strategy

All authentication methods can access the same GitHub APIs - the difference is in setup complexity, security, and token lifecycle. Based on [research findings](2026-02-07_github-safe-repo-defaults.md#authentication-for-scripts), here are our options:

#### Option 1: Personal Access Token (PAT) - RECOMMENDED FOR MVP

**What it is:**
- Token that represents YOU and your account permissions
- Can be Classic PAT (all-or-nothing scopes) or Fine-grained PAT (repo-specific)
- Created at https://github.com/settings/tokens

**Setup complexity:** ⭐ Easy (2 minutes)

**Required scopes:**
- **Classic PAT**: 
  - `repo` (full control of repositories)
  - `workflow` (update GitHub Actions workflows)
- **Fine-grained PAT** (recommended):
  - Repository administration: Read & write
  - Contents: Read & write
  - Actions: Read & write
  - Security events: Read & write
  - Workflows: Read & write

**Pros:**
- ✅ Simplest to set up and use
- ✅ Works immediately with any tool (gh CLI, Python, bash, etc.)
- ✅ No complex token generation logic needed
- ✅ Can set expiration or make it permanent
- ✅ Perfect for personal use

**Cons:**
- ⚠️ If leaked, gives full access to your account (within scopes)
- ⚠️ Tied to your account (breaks if account issues)
- ⚠️ Needs manual rotation if expiring
- ⚠️ Same token for all repos (can't be repo-specific with Classic)

**Usage in script:**
```bash
# Store in environment variable
export GITHUB_TOKEN="ghp_xxxxxxxxxxxx"

# Or in config file (not recommended, less secure)
# Script reads from ~/.github-repo-automation/credentials
```

**When to use:**
- ✅ MVP and initial development
- ✅ Personal use only
- ✅ You're comfortable managing token rotation
- ✅ Quick prototyping

#### Option 2: GitHub CLI (gh) Authentication - RECOMMENDED FOR USERS

**What it is:**
- Leverage existing `gh` CLI authentication
- Script runs `gh auth token` to get current token
- User manages auth through `gh auth login`

**Setup complexity:** ⭐⭐ Medium (requires installing gh CLI)

**How it works:**
```bash
# User sets up gh CLI once
$ gh auth login
# Chooses: browser login or paste token
# gh stores credentials securely

# Script uses it
TOKEN=$(gh auth token)
# Now use TOKEN with PyGithub or API calls
```

**Pros:**
- ✅ User-friendly (handles auth flow for you)
- ✅ Secure token storage (gh CLI manages it)
- ✅ No need to manually copy/paste tokens
- ✅ Works with SSO if configured
- ✅ Familiar to developers already using gh

**Cons:**
- ⚠️ Requires gh CLI installed
- ⚠️ Adds dependency
- ⚠️ Still uses PAT under the hood (same security model)

**Usage in script:**
```python
import subprocess

def get_github_token():
    """Get token from gh CLI if available, fallback to env var"""
    try:
        result = subprocess.run(
            ['gh', 'auth', 'token'],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Fallback to environment variable
        return os.getenv('GITHUB_TOKEN')
```

**When to use:**
- ✅ Users already have gh CLI installed
- ✅ Want easier auth management
- ✅ Building tool for others to use
- ✅ Production release

#### Option 3: GitHub App - MOST SECURE (Future Enhancement)

**What it is:**
- Separate entity from your user account
- Uses App ID + Private Key → generates short-lived tokens (1 hour)
- Fine-grained, repo-specific permissions

**Setup complexity:** ⭐⭐⭐⭐ Complex (15-30 minutes initial setup)

**How it works:**
1. Create GitHub App at https://github.com/settings/apps
2. Generate private key (.pem file)
3. Install app on your account
4. Script generates JWT from App ID + Private Key
5. Exchange JWT for Installation Access Token
6. Use token (expires in 1 hour, auto-renewable)

**Pros:**
- ✅ Most secure (short-lived tokens)
- ✅ Fine-grained permissions (per-repo control)
- ✅ Independent from user account
- ✅ Best for long-running automation
- ✅ Clear attribution (shows as app, not you)

**Cons:**
- ⚠️ Complex initial setup
- ⚠️ Need to manage private key file
- ⚠️ More code for token generation
- ⚠️ Overkill for simple personal use
- ⚠️ Requires understanding JWT

**Usage in script:**
```python
from github import GithubIntegration

# Load app credentials
APP_ID = "123456"
PRIVATE_KEY = open('path/to/private-key.pem', 'r').read()
INSTALLATION_ID = "7891011"

# Authenticate as GitHub App
git_integration = GithubIntegration(APP_ID, PRIVATE_KEY)
installation = git_integration.get_installation(INSTALLATION_ID)
access_token = installation.get_access_token()

# Use token (auto-renews after 1 hour)
from github import Github
g = Github(access_token.token)
```

**When to use:**
- ✅ Maximum security needed
- ✅ Sharing tool with others
- ✅ Long-running automation
- ✅ Want independent service identity
- ❌ NOT for MVP (too complex)

#### Option 4: SSH Keys - NOT APPLICABLE

**Why not:**
- ❌ SSH only works for git operations (clone, push, pull)
- ❌ Cannot access GitHub REST/GraphQL APIs
- ❌ Cannot modify repository settings
- ❌ Only for authenticating git protocol

Your existing SSH setup is fine for git operations, but we need API access for settings automation.

### What Each Method Can Do

**All authentication methods can access the same APIs:**

| Feature | PAT | gh CLI | GitHub App | SSH |
|---------|-----|--------|------------|-----|
| Create repos | ✅ | ✅ | ✅ | ❌ |
| Branch protection | ✅ | ✅ | ✅ | ❌ |
| Security settings | ✅ | ✅ | ✅ | ❌ |
| Actions config | ✅ | ✅ | ✅ | ❌ |
| Git operations | ✅* | ✅* | ✅* | ✅ |

*Can use token as password for HTTPS git operations

**The limitation is NOT the auth method - it's your GitHub plan:**
- Free plan + private repo = no branch protection (regardless of auth)
- Pro plan + private repo = full branch protection (any auth method works)
- Free plan + public repo = full branch protection (any auth method works)

### Recommended Implementation Path

**Phase 1 (MVP): Support Both PAT and gh CLI**

```python
def get_github_token():
    """
    Try multiple auth methods in order of preference:
    1. gh CLI (if available)
    2. GITHUB_TOKEN environment variable
    3. Config file (warn if insecure)
    """
    # Try gh CLI first
    try:
        import subprocess
        result = subprocess.run(
            ['gh', 'auth', 'token'],
            capture_output=True,
            text=True,
            check=True
        )
        print("✓ Using gh CLI authentication")
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    
    # Try environment variable
    token = os.getenv('GITHUB_TOKEN')
    if token:
        print("✓ Using GITHUB_TOKEN environment variable")
        return token
    
    # Try config file (warn about security)
    config_token = config.get('auth', 'token', fallback=None)
    if config_token:
        print("⚠️  Using token from config file (not recommended)")
        print("   Consider using: export GITHUB_TOKEN=xxx or gh auth login")
        return config_token
    
    # No auth found
    print("❌ No authentication found!")
    print("   Options:")
    print("   1. Run: gh auth login")
    print("   2. Set: export GITHUB_TOKEN=your_token")
    print("   3. Create token at: https://github.com/settings/tokens")
    sys.exit(1)

# Use it
token = get_github_token()
g = Github(token)
```

**Phase 2 (Future): Add GitHub App Support**

Add optional GitHub App support for users who want it:

```python
def get_github_client():
    """Get authenticated GitHub client"""
    
    # Check if GitHub App credentials exist
    if os.path.exists('~/.github-repo-automation/app.pem'):
        print("✓ Using GitHub App authentication")
        return github_app_auth()
    
    # Otherwise use token-based auth
    token = get_github_token()
    return Github(token)
```

### Documentation Requirements

**For users, document:**
1. **Easiest**: Install gh CLI and run `gh auth login`
2. **Alternative**: Create PAT and export as environment variable
3. **Advanced**: Set up GitHub App (link to guide)

**Required PAT scopes:**
- Clearly list what permissions are needed
- Explain why each is needed
- Link to GitHub's scope documentation

**Security best practices:**
- Never commit tokens to git
- Use environment variables
- Set token expiration
- Rotate regularly
- Use fine-grained PATs when possible

## Implementation Phases

### Phase 1: MVP - Private Repo Creation (Week 1)

**Goal:** Create working script that sets up private repos

**Features:**
- Create private repository
- Apply basic settings (wiki off, issues on, etc.)
- Create SECURITY.md from template
- Read from config.ini
- PAT authentication

**Deliverables:**
- `gh-safe-repo` script (Python)
- `config.ini.example`
- Basic README with setup instructions
- Tested on macOS and Linux

**Success Criteria:**
- Can create private repo with one command
- Settings match config file
- SECURITY.md created

### Phase 2: Public Repo Creation (Week 2)

**Goal:** Add public repo creation with safety features

**Features:**
- Create public repository (new, not converting existing)
- Apply branch protection/rulesets
- Copy code from existing private repo
- Configure fork PR approval

**Deliverables:**
- `--from` and `--public` flags
- Branch protection configuration
- Code copying logic

**Success Criteria:**
- Can create public repo from private
- Branch protection active before code copied
- Private repo unchanged

### Phase 3: Pre-flight Security Scan (Week 3)

**Goal:** Add security scanning before going public

**Features:**
- Scan for hardcoded secrets (regex patterns)
- Scan for emails and usernames
- Check file sizes
- Interactive review of findings
- Abort if critical issues found

**Deliverables:**
- `security_scanner.py` module
- Pattern library for common secrets
- Interactive prompt system

**Success Criteria:**
- Detects common secret patterns
- User can review and abort
- No false positives for common cases

### Phase 4: Polish & Distribution (Week 4)

**Goal:** Make tool production-ready

**Features:**
- Comprehensive error handling
- Better logging and debug mode
- Unit tests
- Documentation
- Package for distribution (pip installable?)

**Deliverables:**
- Complete test suite
- Full documentation
- Packaging setup
- GitHub repo for tool itself

**Success Criteria:**
- All error cases handled gracefully
- Tests pass
- Documentation complete
- Others can install and use

## Technical Decisions Needed

### Decision 1: Rulesets vs Classic Branch Protection

**Context:** GitHub offers two APIs for branch protection

**Options:**
- A) Use Rulesets API (modern, recommended by GitHub)
- B) Use Classic Branch Protection API (older, well-documented)
- C) Support both, with config option

**Recommendation:** Start with Classic (B), add Rulesets later (C)

**Rationale:**
- Classic API is simpler, better documented
- More examples available
- Rulesets require more complex JSON payloads
- Can add Rulesets support in Phase 4

### Decision 2: Configuration Format

**Options:**
- A) INI file (human-readable, simple)
- B) YAML (more powerful, hierarchical)
- C) TOML (modern, Python-friendly)
- D) JSON (machine-readable, verbose)

**Recommendation:** INI (A)

**Rationale:**
- Explicitly requested in discussion
- Easy for non-technical users
- Good Python support (configparser)
- Simple syntax, fewer gotchas

### Decision 3: Pre-flight Scan Depth

**Options:**
- A) Basic: Regex for common patterns only
- B) Medium: Use tools like `truffleHog`, `git-secrets`
- C) Advanced: Custom ML-based detection

**Recommendation:** Medium (B) with fallback to Basic (A)

**Rationale:**
- `truffleHog` catches more secrets than regex
- Still runs locally (requirement)
- If `truffleHog` not installed, fall back to regex
- Can enhance patterns over time

### Decision 4: Handling Existing Repos

**Scope:** Should script work on existing repos?

**Options:**
- A) New repos only (simpler, safer)
- B) Support updating existing repos
- C) Read-only analysis of existing repos

**Recommendation:** Phase 1: New only (A), Phase 4: Read-only analysis (C)

**Rationale:**
- Updating existing repos is complex (what if settings conflict?)
- New repos only is safer
- Analysis mode (show what's wrong) is useful without risk
- Can add update feature later if needed

## Risk Analysis

### Risk 1: API Rate Limiting

**Impact:** Medium  
**Likelihood:** Low (personal use, not bulk operations)

**Mitigation:**
- Implement exponential backoff
- Show rate limit status in debug mode
- Batch operations where possible

### Risk 2: Exposed Secrets in Pre-flight Scan

**Impact:** High  
**Likelihood:** Low (running locally, not in GHA)

**Mitigation:**
- Never commit scan patterns to public repo
- Run scan locally only
- User-specific patterns in local config
- Clear documentation about not sharing patterns

### Risk 3: GitHub API Changes

**Impact:** Medium  
**Likelihood:** Medium (APIs do change)

**Mitigation:**
- Pin API versions where possible
- Monitor GitHub changelog
- Version script, document compatible API versions
- Automated tests against GitHub API

### Risk 4: Authentication Token Exposure

**Impact:** High  
**Likelihood:** Medium (user error)

**Mitigation:**
- Never log tokens
- Use environment variables
- Document token security best practices
- Support token scopes (least privilege)
- Consider GitHub App in future

### Risk 5: Incompatible GitHub Plans

**Impact:** Low  
**Likelihood:** High (free plan very limited)

**Mitigation:**
- Detect plan level via API
- Show clear error messages about features
- Gracefully degrade (skip features not available)
- Document plan requirements clearly

## Success Metrics

### MVP Success (Phase 1)
- ✅ Can create private repo in <30 seconds
- ✅ All config options work as expected
- ✅ Zero manual GitHub UI clicks needed
- ✅ Works on macOS, Linux, Windows

### Full Success (Phase 4)
- ✅ Used for 100% of new repos (personal adoption)
- ✅ Pre-flight scan catches secrets (at least 1 real catch)
- ✅ Zero repos made public without protection
- ✅ Other developers can use it (optional)

## Open Questions

1. **Should we support organization repos eventually?**
   - Probably not priority, but API is similar
   - Could add with `--org` flag later

2. **Should script handle git operations (clone, push)?**
   - Or just GitHub settings, user handles git separately?
   - Leaning toward: just GitHub settings for now

3. **Interactive vs fully automated?**
   - Show preview of settings before apply?
   - Or just apply based on config?
   - Compromise: Show summary, require `--yes` to skip confirmation

4. **How to handle template repos?**
   - GitHub has built-in template repos
   - Do we integrate with that, or replace it?
   - Leaning toward: ignore for now, different use case

5. **Should we support .github/workflows creation?**
   - Create basic CI workflows?
   - Or just configure Actions settings, user adds workflows?
   - Leaning toward: just settings, workflows are content

## Next Steps

1. **Review this plan** - Validate approach, decisions, scope
2. **Set up development environment** - Python, PyGithub, test account
3. **Create GitHub repo for tool** - Meta: use the tool to create itself!
4. **Start Phase 1 implementation** - Private repo creation
5. **Test with real repos** - Use for actual projects
6. **Iterate based on usage** - Refine based on real experience

## Appendix: API Endpoints Reference

Quick reference for APIs we'll be using:

**Repository Creation:**
```
POST /user/repos
POST /orgs/{org}/repos
```

**Repository Settings:**
```
PATCH /repos/{owner}/{repo}
```

**Branch Protection (Classic):**
```
PUT /repos/{owner}/{repo}/branches/{branch}/protection
GET /repos/{owner}/{repo}/branches/{branch}/protection
```

**Branch Rulesets (Modern):**
```
POST /repos/{owner}/{repo}/rulesets
GET /repos/{owner}/{repo}/rulesets
PUT /repos/{owner}/{repo}/rulesets/{ruleset_id}
```

**Security Settings:**
```
PUT /repos/{owner}/{repo}/vulnerability-alerts
PUT /repos/{owner}/{repo}/automated-security-fixes
```

**Actions Settings:**
```
PUT /repos/{owner}/{repo}/actions/permissions
```

**Topics:**
```
PUT /repos/{owner}/{repo}/topics
```

**References:**
- [GitHub REST API Docs](https://docs.github.com/en/rest)
- [PyGithub Documentation](https://pygithub.readthedocs.io/)
- [Branch Protection API](https://docs.github.com/en/rest/branches/branch-protection)
- [Rulesets API](https://docs.github.com/en/rest/repos/rules)
