# Manual Testing Guide — gh-safe-repo

Live tests only (no `--dry-run`). Run these in order — later tests depend on repos created by earlier ones.

**Prerequisites:**
- `uv tool install .` completed (or `./gh-safe-repo` from repo root)
- `gh auth login` completed and `gh auth token` returns a token
- Replace `YOUR_USERNAME` with your actual GitHub username throughout
- Note your GitHub plan (free vs. paid) — some tests behave differently

---

## 0. Sanity Checks

### 0.1 Auth check

```bash
gh auth token
```

**Expected:** A token string (ghp_... or github_pat_...). If nothing: run `gh auth login` first.

### 0.2 Tool is on PATH

```bash
gh-safe-repo --help
```

**Expected output (approximately):**

```
usage: gh-safe-repo [-h] [--from SOURCE] [--local PATH] [--audit] [--public]
                    [--dry-run] [--debug] [--config PATH] [--scan PATH]
                    [--json]
                    [repo]

positional arguments:
  repo           Repository name to create or audit

optional arguments:
  -h, --help     show this help message and exit
  --from SOURCE  Mirror an existing private repo into the new public repo
  --local PATH   Push a local directory or git repo to the new GitHub repo
  --audit        Audit an existing repo and apply missing safe defaults
  --public       Create a public repository
  --dry-run      Show what would be done without making any changes
  --debug        Show all API calls and responses
  --config PATH  Path to config file (default: ~/.config/gh-safe-repo/config.ini)
  --scan PATH    Scan a local path for secrets and sensitive data (no GitHub
                 interaction)
  --json         Output the plan as JSON instead of a human-readable table
```

---

## 1. Create — Private Repo (Basic)

### 1.1 Create a new private repo

```bash
gh-safe-repo gsr-test-private-01
```

**Expected — interactive prompt:**

```
Plan for gsr-test-private-01:

  Category          Setting                          Change   Old       New
  ────────────────────────────────────────────────────────────────────────────
  REPO              create_repo                      ADD                private
  REPO              has_wiki                         UPDATE   true      false
  REPO              has_projects                     UPDATE   true      false
  REPO              delete_branch_on_merge           UPDATE   false     true
  REPO              allow_merge_commit               UPDATE   true      false
  ACTIONS           sha_pinning_required             UPDATE   false     true
  ACTIONS           default_workflow_permissions     UPDATE   write     read
  ACTIONS           can_approve_pull_request_reviews UPDATE   true      false
  BRANCH_PROTECTION protected_branches               ADD                main
  SECURITY          dependabot_alerts                SKIP               ...
```

> **Note on SECURITY rows:** On a free plan, private repos show SKIP for `dependabot_alerts` and `secret_scanning` with a reason like "Requires paid GitHub plan for private repositories". On a paid plan, both show ADD.
>
> **Note on BRANCH_PROTECTION:** On a free plan, private repos show SKIP for branch protection. On a paid plan, ADD rows appear.

```
Apply 5 changes? [y/N]:
```

Type `y`.

**Expected — success output:**

```
╔══════════════════════════════════════════════════════╗
║  ✓ gsr-test-private-01 created                       ║
║    https://github.com/YOUR_USERNAME/gsr-test-private-01 ║
╚══════════════════════════════════════════════════════╝
```

**Verify on GitHub:**
- Go to `https://github.com/YOUR_USERNAME/gsr-test-private-01`
- Settings → General: confirm Wiki disabled, Projects disabled, Squash merge ON, Merge commits OFF
- Settings → Branches (paid plan): confirm branch protection rule exists on `main`
- Settings → Actions → General: confirm "Read repository contents and packages" selected, and "Require SHA pinning" is checked

### 1.2 Attempt to create the same repo again

```bash
gh-safe-repo gsr-test-private-01
```

**Expected:**

```
Error: repository 'YOUR_USERNAME/gsr-test-private-01' already exists.
```

Process exits with non-zero status.

---

## 2. Create — Public Repo (Basic)

### 2.1 Create a new public repo directly (no --from)

```bash
gh-safe-repo gsr-test-public-01 --public
```

**Expected plan:** Similar to 1.1 but:
- REPO `create_repo` shows `public` instead of `private`
- SECURITY `secret_scanning` shows SKIP with reason "Automatically enabled for public repositories by GitHub"
- SECURITY `dependabot_alerts` shows ADD (free plan supports public repos)
- BRANCH_PROTECTION shows ADD rows (free plan supports public repos)

Type `y` at the prompt.

**Expected success output:**

```
╔═══════════════════════════════════════════════════════╗
║  ✓ gsr-test-public-01 created                         ║
║    https://github.com/YOUR_USERNAME/gsr-test-public-01 ║
╚═══════════════════════════════════════════════════════╝
```

**Verify on GitHub:**
- Repo is public
- Branch protection is enabled on `main`
- Dependabot alerts enabled (Security tab shows "Dependabot alerts: Enabled")

---

## 3. --from --public Workflow (Mirror Private → Public)

This creates a source private repo, puts a test file in it, then mirrors it to a new public repo.

### 3.1 Create a source private repo with content

```bash
gh-safe-repo gsr-test-source-01
```

Type `y`. Then push a test file:

```bash
cd /tmp
git clone https://github.com/YOUR_USERNAME/gsr-test-source-01
cd gsr-test-source-01
echo "# Test" > README.md
git add README.md
git commit -m "Initial commit"
git push
cd /tmp && rm -rf gsr-test-source-01
```

### 3.2 Mirror private to new public repo

```bash
gh-safe-repo gsr-test-public-from-01 --from gsr-test-source-01 --public
```

**Expected — scan output first (since source has content):**

```
Scanning gsr-test-source-01 for sensitive content...
  No findings.
```

Then the plan table, then prompt. Type `y`.

**Expected — success output:**

```
╔═══════════════════════════════════════════════════════════════╗
║  ✓ gsr-test-public-from-01 created                            ║
║    https://github.com/YOUR_USERNAME/gsr-test-public-from-01   ║
╚═══════════════════════════════════════════════════════════════╝
```

**Verify on GitHub:**
- `gsr-test-public-from-01` is public
- README.md is present (code was mirrored)
- Branch protection enabled on `main`

### 3.3 --from without --public is rejected

```bash
gh-safe-repo gsr-test-bad --from gsr-test-source-01
```

**Expected:**

```
error: argument --from: --from requires --public
```

Process exits immediately (no API calls made).

### 3.4 --from with non-existent source

```bash
gh-safe-repo gsr-test-bad --from gsr-test-does-not-exist-xyz --public
```

**Expected:**

```
Error: source repository 'YOUR_USERNAME/gsr-test-does-not-exist-xyz' not found.
```

---

## 4. --local Workflow (Push Local Directory/Repo)

### 4.1 Push a plain local directory (no .git)

```bash
mkdir /tmp/gsr-local-dir-test
echo "# Hello" > /tmp/gsr-local-dir-test/README.md
echo "secret = not_a_real_secret" > /tmp/gsr-local-dir-test/config.txt
gh-safe-repo gsr-test-local-dir-01 --local /tmp/gsr-local-dir-test
```

**Expected — scan output:**

```
Scanning /tmp/gsr-local-dir-test for sensitive content...
```

The `config.txt` file may or may not trigger a WARNING depending on the regex patterns (the word "secret" may match a WARNING pattern). If findings are shown:

```
  [WARNING] SECRET  config.txt:1  generic_secret  secret = ***REDACTED***

1 finding(s). Continue anyway? [y/N]:
```

Type `y` to continue (or `n` to abort — test both).

After plan prompt, type `y`.

**Expected success:**

```
╔══════════════════════════════════════════════════════════╗
║  ✓ gsr-test-local-dir-01 created                         ║
║    https://github.com/YOUR_USERNAME/gsr-test-local-dir-01 ║
╚══════════════════════════════════════════════════════════╝
```

**Verify:** README.md and config.txt are present in the new GitHub repo.

### 4.2 Push a local git repo (with history)

```bash
mkdir /tmp/gsr-local-git-test
cd /tmp/gsr-local-git-test
git init
echo "# Git Repo" > README.md
git add README.md
git commit -m "Initial commit"
echo "v2" > README.md
git add README.md
git commit -m "Second commit"
cd -
gh-safe-repo gsr-test-local-git-01 --local /tmp/gsr-local-git-test
```

Type `y` at the plan prompt.

**Expected success:**

```
╔══════════════════════════════════════════════════════════╗
║  ✓ gsr-test-local-git-01 created                         ║
║    https://github.com/YOUR_USERNAME/gsr-test-local-git-01 ║
╚══════════════════════════════════════════════════════════╝
```

**Verify:** README.md on GitHub shows "v2". Git log on GitHub shows 2 commits.

### 4.3 --local with non-existent path

```bash
gh-safe-repo gsr-test-bad --local /tmp/path-does-not-exist-xyz
```

**Expected:**

```
error: --local path '/tmp/path-does-not-exist-xyz' does not exist or is not a directory
```

Exits with status 2 (no API calls).

### 4.4 --local and --from are mutually exclusive

```bash
gh-safe-repo gsr-test-bad --local /tmp --from gsr-test-source-01 --public
```

**Expected:**

```
error: argument --local: --local and --from are mutually exclusive
```

### 4.5 --local and --audit are mutually exclusive

```bash
gh-safe-repo gsr-test-private-01 --audit --local /tmp
```

**Expected:**

```
error: argument --local: --local and --audit are mutually exclusive
```

---

## 5. --audit Workflow (Apply Safe Defaults to Existing Repo)

### 5.1 Audit a fully-configured repo (all settings already correct)

Audit one of the repos created in section 1 — it was already configured with safe defaults.

```bash
gh-safe-repo gsr-test-private-01 --audit
```

**Expected — scan output:**

```
Scanning gsr-test-private-01 for sensitive content...
  No findings.
```

**Expected — plan table:** All rows show SKIP with reason "already set" or "no change needed". No ADD or UPDATE rows (unless your GitHub plan differs from what was applied).

**Expected prompt:**

```
No changes needed.
```

(No `Apply N changes?` prompt — or the tool exits cleanly without prompting when there's nothing to do.)

### 5.2 Audit a repo with settings drift

Create a repo manually via `gh` CLI (without safe defaults), then audit it:

```bash
gh repo create gsr-test-audit-target-01 --private --confirm
gh-safe-repo gsr-test-audit-target-01 --audit
```

**Expected — plan table shows UPDATE rows** for all settings that differ from safe defaults:
- `has_wiki`: true → false
- `has_projects`: true → false
- `delete_branch_on_merge`: false → true
- `allow_merge_commit`: true → false
- `sha_pinning_required`: false → true
- `default_workflow_permissions`: write → read
- `can_approve_pull_request_reviews`: true → false
- (Plus branch protection ADD if paid plan)

```
Apply N changes? [y/N]:
```

Type `y`.

**Expected success:**

```
╔═══════════════════════════════════════════════════════════╗
║  ✓ gsr-test-audit-target-01 audited — N changes applied   ║
╚═══════════════════════════════════════════════════════════╝
```

**Verify on GitHub:** Settings match safe defaults.

### 5.3 Audit a non-existent repo

```bash
gh-safe-repo gsr-test-does-not-exist-xyz --audit
```

**Expected:**

```
Error: repository 'YOUR_USERNAME/gsr-test-does-not-exist-xyz' not found.
```

### 5.4 --audit and --from are mutually exclusive

```bash
gh-safe-repo gsr-test-private-01 --audit --from gsr-test-source-01 --public
```

**Expected:**

```
error: argument --audit: --audit and --from are mutually exclusive
```

---

## 6. --scan Workflow (Standalone Security Scan)

### 6.1 Scan a clean directory

```bash
mkdir /tmp/gsr-clean-scan
echo "# Hello World" > /tmp/gsr-clean-scan/README.md
gh-safe-repo --scan /tmp/gsr-clean-scan
```

**Expected:**

```
Scanning /tmp/gsr-clean-scan...
  No findings.
```

Exit code 0.

### 6.2 Scan a directory with a fake secret

```bash
mkdir /tmp/gsr-secret-scan
echo 'AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE' > /tmp/gsr-secret-scan/creds.env
gh-safe-repo --scan /tmp/gsr-secret-scan
```

**Expected:**

```
Scanning /tmp/gsr-secret-scan...

  [CRITICAL] SECRET  creds.env:1  aws_access_key  AWS_ACCESS_KEY_ID=***REDACTED***

1 finding(s).
```

Exit code non-zero (findings present).

### 6.3 Scan a directory with an email address

```bash
mkdir /tmp/gsr-email-scan
echo 'Contact: user@example.com' > /tmp/gsr-email-scan/README.md
gh-safe-repo --scan /tmp/gsr-email-scan
```

**Expected:**

```
Scanning /tmp/gsr-email-scan...

  [WARNING] EMAIL  README.md:1  email_address  user@example.com

1 finding(s).
```

### 6.4 Scan a directory with a large file

```bash
mkdir /tmp/gsr-large-scan
dd if=/dev/urandom bs=1M count=150 of=/tmp/gsr-large-scan/bigfile.bin 2>/dev/null
gh-safe-repo --scan /tmp/gsr-large-scan
```

**Expected:**

```
Scanning /tmp/gsr-large-scan...

  [WARNING] LARGE_FILE  bigfile.bin  large_file  150.0 MB (limit: 100 MB)

1 finding(s).
```

### 6.5 Scan a directory with a TODO comment

```bash
mkdir /tmp/gsr-todo-scan
echo '// TODO: remove hardcoded password before shipping' > /tmp/gsr-todo-scan/app.js
gh-safe-repo --scan /tmp/gsr-todo-scan
```

**Expected:**

```
Scanning /tmp/gsr-todo-scan...

  [INFO] TODO  app.js:1  todo_comment  TODO: remove hardcoded password before shipping

1 finding(s).
```

### 6.6 Scan a directory with a CLAUDE.md file

```bash
mkdir /tmp/gsr-ai-scan
echo '# Instructions' > /tmp/gsr-ai-scan/CLAUDE.md
gh-safe-repo --scan /tmp/gsr-ai-scan
```

**Expected:**

```
Scanning /tmp/gsr-ai-scan...

  [CRITICAL] AI_CONTEXT_FILE  CLAUDE.md  ai_context_file  AI context file detected.
             Remediation: git filter-repo --path CLAUDE.md --invert-paths

1 finding(s).
```

### 6.7 Scan with no path argument

```bash
gh-safe-repo --scan
```

**Expected:**

```
error: argument --scan: expected one argument
```

### 6.8 Scan a git repo with AI context file deleted from history

```bash
mkdir /tmp/gsr-history-scan
cd /tmp/gsr-history-scan
git init
echo '# AI Instructions' > CLAUDE.md
git add CLAUDE.md && git commit -m "add claude"
git rm CLAUDE.md && git commit -m "remove claude"
cd -
gh-safe-repo --scan /tmp/gsr-history-scan
```

**Expected:**

```
Scanning /tmp/gsr-history-scan...

  [CRITICAL] AI_CONTEXT_FILE  CLAUDE.md  ai_context_file_history  AI context file found in git history but removed from working tree.
             Remediation: git filter-repo --path CLAUDE.md --invert-paths

1 finding(s).
```

---

## 7. --json Flag

### 7.1 JSON output for a new repo plan

```bash
gh-safe-repo gsr-test-json-01 --dry-run --json
```

> Note: `--dry-run` is used here so the JSON test doesn't create a real repo. For a fully live test, omit `--dry-run` — but the JSON output is written to stdout regardless.

**Expected stdout:** A valid JSON object. Human-readable info goes to stderr.

```json
{
  "repo": "gsr-test-json-01",
  "changes": [
    {
      "type": "add",
      "category": "repo",
      "key": "create_repo",
      "old": null,
      "new": "private",
      "reason": null
    },
    ...
  ],
  "summary": {
    "add": 1,
    "update": 7,
    "skip": 3
  }
}
```

Key checks:
- `changes` is an array of objects with `type`, `category`, `key`, `old`, `new`, `reason` fields
- `summary` only contains keys for change types that are actually present (no `delete` key if there are no deletes)
- `type` values are lowercase: `"add"`, `"update"`, `"skip"`, `"delete"`
- stdout is valid JSON (`gh-safe-repo ... --json | python3 -m json.tool` should succeed)

### 7.2 JSON is machine-readable (pipe test)

```bash
gh-safe-repo gsr-test-json-01 --dry-run --json 2>/dev/null | python3 -m json.tool
```

**Expected:** Pretty-printed JSON with no errors. All info/warn output is suppressed (it went to stderr).

### 7.3 JSON for audit mode

```bash
gh-safe-repo gsr-test-private-01 --audit --dry-run --json 2>/dev/null | python3 -m json.tool
```

**Expected:** Valid JSON. All rows will be SKIP (repo already has safe defaults). `summary` will contain only `{"skip": N}`.

---

## 8. --debug Flag

### 8.1 Debug output shows API calls

```bash
gh-safe-repo gsr-test-debug-01 --dry-run --debug
```

**Expected:** All normal output PLUS lines like:

```
[DEBUG] GET /user -> 200
[DEBUG] GET /repos/YOUR_USERNAME/gsr-test-debug-01 -> 404
```

No tokens or credentials appear in debug output (sanitized URLs).

---

## 9. Config File Customisation

### 9.1 Custom config with different settings

```bash
mkdir -p /tmp/gsr-config-test
cat > /tmp/gsr-config-test/config.ini << 'EOF'
[repo]
has_issues = false
has_wiki = true

[branch_protection]
required_approving_reviews = 2
EOF
gh-safe-repo gsr-test-config-01 --config /tmp/gsr-config-test/config.ini --dry-run
```

**Expected plan differences vs. defaults:**
- `has_issues` shows UPDATE true → false (or ADD false in create mode)
- `has_wiki` shows SKIP (desired=true matches GitHub default=true, no change needed)
- `required_approving_reviews` shows 2 instead of 1 in branch protection

### 9.2 Config file that doesn't exist

```bash
gh-safe-repo gsr-test-config-01 --config /tmp/path-that-does-not-exist/config.ini --dry-run
```

**Expected:** Tool runs normally using built-in safe defaults (missing config file is not an error — it's treated as "use defaults").

---

## 10. Plan-Level Gating

These tests verify the tool correctly detects GitHub plan level and gates features.

> **Note:** `sha_pinning_required` is **not** plan-gated or visibility-gated. It appears as an UPDATE in every plan (create, audit, public, private, free, paid).

### 10.1 Free plan — private repo skips branch protection and security

(Only relevant if you are on a free GitHub plan.)

```bash
gh-safe-repo gsr-test-free-plan-private --dry-run
```

**Expected plan:** BRANCH_PROTECTION and SECURITY rows show SKIP with reason text like:
- "Requires paid GitHub plan for private repositories"

### 10.2 Free plan — public repo gets branch protection and Dependabot

```bash
gh-safe-repo gsr-test-free-plan-public --public --dry-run
```

**Expected plan:**
- BRANCH_PROTECTION shows ADD rows (no SKIP)
- SECURITY `dependabot_alerts` shows ADD
- SECURITY `secret_scanning` shows SKIP with "Automatically enabled for public repositories by GitHub"

---

## 11. Pre-flight Scan — Interactive Abort

### 11.1 Abort when findings are present

```bash
mkdir /tmp/gsr-abort-test
echo 'GITHUB_TOKEN=ghp_fakefakefakefakefakefakefakefake01' > /tmp/gsr-abort-test/leak.txt
gh-safe-repo gsr-test-abort-01 --local /tmp/gsr-abort-test
```

**Expected — scan output:**

```
Scanning /tmp/gsr-abort-test for sensitive content...

  [CRITICAL] SECRET  leak.txt:1  github_token  GITHUB_TOKEN=***REDACTED***

1 finding(s). Continue anyway? [y/N]:
```

Type `n`.

**Expected:**

```
Aborted.
```

No repo is created. Verify with:

```bash
gh repo view YOUR_USERNAME/gsr-test-abort-01
```

**Expected:** `Could not resolve to a Repository` (doesn't exist).

### 11.2 Continue past warnings

Repeat the above but type `y`. Repo should be created with the file pushed to it.

---

## 12. Rulesets API (use_rulesets = true)

### 12.1 Create a repo using Rulesets instead of classic branch protection

```bash
cat > /tmp/gsr-ruleset-config.ini << 'EOF'
[branch_protection]
use_rulesets = true
EOF
gh-safe-repo gsr-test-rulesets-01 --public --config /tmp/gsr-ruleset-config.ini
```

Type `y` at the prompt.

**Verify on GitHub:**
- Settings → Rules → Rulesets (not Branches) shows a ruleset named something like "default-protection"
- The ruleset includes: non-fast-forward (force push) restriction, deletion restriction, pull request requirement

---

## 13. Cleanup

Delete all test repos after testing:

```bash
for repo in \
  gsr-test-private-01 \
  gsr-test-public-01 \
  gsr-test-source-01 \
  gsr-test-public-from-01 \
  gsr-test-local-dir-01 \
  gsr-test-local-git-01 \
  gsr-test-audit-target-01 \
  gsr-test-debug-01 \
  gsr-test-config-01 \
  gsr-test-free-plan-private \
  gsr-test-free-plan-public \
  gsr-test-rulesets-01; do
  gh repo delete YOUR_USERNAME/$repo --yes 2>/dev/null && echo "Deleted $repo" || echo "Skipped $repo (not found)"
done
```

Clean up local temp directories:

```bash
rm -rf /tmp/gsr-*
```

---

## 14. Tool Scripts (`tools/`)

The shell scripts in `tools/` have their own manual test suites and must be
tested separately against throwaway repos in `/tmp`. These scripts have **not
yet been manually tested** after recent rewrites.

| Script | Test doc | Status |
|---|---|---|
| `git-filter-file.sh` | `tools/git-filter-file-TESTING.md` | Untested after `--yes` flag addition |
| `scrub-ai-context.sh` | `tools/scrub-ai-context-TESTING.md` | Untested — rewritten as wrapper around git-filter-file |

Run each test doc's full suite before considering these scripts production-ready.

---

## Test Matrix Summary

| Test | Create | From | Local | Audit | Scan | JSON | Free plan | Paid plan |
|------|:------:|:----:|:-----:|:-----:|:----:|:----:|:---------:|:---------:|
| 1.1 Basic private create | ✓ | | | | | | ✓ | ✓ |
| 1.2 Duplicate repo error | ✓ | | | | | | ✓ | ✓ |
| 2.1 Basic public create | ✓ | | | | | | ✓ | ✓ |
| 3.2 Mirror private→public | ✓ | ✓ | | | ✓ | | ✓ | ✓ |
| 3.3 --from without --public | | ✓ | | | | | ✓ | ✓ |
| 3.4 --from bad source | ✓ | ✓ | | | | | ✓ | ✓ |
| 4.1 --local plain dir | ✓ | | ✓ | | ✓ | | ✓ | ✓ |
| 4.2 --local git repo | ✓ | | ✓ | | ✓ | | ✓ | ✓ |
| 4.3 --local bad path | | | ✓ | | | | ✓ | ✓ |
| 4.4 --local + --from error | | ✓ | ✓ | | | | ✓ | ✓ |
| 4.5 --local + --audit error | | | ✓ | ✓ | | | ✓ | ✓ |
| 5.1 Audit fully-configured | | | | ✓ | ✓ | | ✓ | ✓ |
| 5.2 Audit with drift | | | | ✓ | ✓ | | ✓ | ✓ |
| 5.3 Audit non-existent repo | | | | ✓ | | | ✓ | ✓ |
| 6.2 Scan fake secret | | | | | ✓ | | ✓ | ✓ |
| 6.3 Scan email | | | | | ✓ | | ✓ | ✓ |
| 6.4 Scan large file | | | | | ✓ | | ✓ | ✓ |
| 6.5 Scan TODO comment | | | | | ✓ | | ✓ | ✓ |
| 6.6 Scan CLAUDE.md | | | | | ✓ | | ✓ | ✓ |
| 6.8 Scan deleted history | | | | | ✓ | | ✓ | ✓ |
| 7.1 --json output | ✓ | | | | | ✓ | ✓ | ✓ |
| 7.2 --json pipeable | ✓ | | | | | ✓ | ✓ | ✓ |
| 7.3 --json audit | | | | ✓ | | ✓ | ✓ | ✓ |
| 9.1 Custom config | ✓ | | | | | | ✓ | ✓ |
| 11.1 Abort on findings | ✓ | | ✓ | | ✓ | | ✓ | ✓ |
| 12.1 Rulesets API | ✓ | | | | | | | ✓ |
