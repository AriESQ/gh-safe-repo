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
usage: gh-safe-repo [-h] {create,fix,scan} ...

Create GitHub repositories with safe defaults applied.

positional arguments:
  {create,fix,scan}
    create           Create a new repo with safe defaults
    fix              Audit an existing repo and apply safe defaults
    scan             Scan a local directory for secrets (no GitHub interaction)

options:
  -h, --help         show this help message and exit
```

### 0.3 Subcommand help

```bash
gh-safe-repo create --help
gh-safe-repo fix --help
gh-safe-repo scan --help
```

**Expected:** Each subcommand shows its own arguments (e.g. `create` shows `--public`, `--local`, `--from`, `--yes`; `fix` shows `--yes`; `scan` has no `--dry-run`).

### 0.4 No subcommand shows help

```bash
gh-safe-repo
```

**Expected:** Prints the top-level help with examples and exits with status 2.

### 0.5 Bare repo name is rejected

```bash
gh-safe-repo create my-repo
```

**Expected:**

```
Error: Use owner/repo format (e.g. myuser/my-repo)
```

Exits with status 2.

### 0.6 Wrong owner is rejected

```bash
gh-safe-repo create wronguser/my-repo --dry-run
```

**Expected:**

```
Error: Owner 'wronguser' does not match authenticated user 'YOUR_USERNAME'
```

---

## 1. Create — Private Repo (Basic)

### 1.1 Create a new private repo

```bash
gh-safe-repo create YOUR_USERNAME/gsr-test-private-01
```

**Expected — plan table then prompt:**

```
Plan for YOUR_USERNAME/gsr-test-private-01:

  Type    Category          Setting                          Value / Note
  ────────────────────────────────────────────────────────────────────────────
  ADD     REPO              create_repo                      private
  ADD     REPO              has_wiki                         false
  ADD     REPO              has_projects                     false
  ADD     ACTIONS           allowed_actions                  selected
  ADD     ACTIONS           verified_allowed                 true
  ADD     ACTIONS           sha_pinning_required             true
  ADD     ACTIONS           default_workflow_permissions     read
  ADD     ACTIONS           can_approve_pull_request_reviews false
  SKIP    BRANCH_PROTECTION ...                              ...
  SKIP    SECURITY          ...                              ...
```

> **Note on SECURITY rows:** On a free plan, private repos show SKIP for `dependabot_alerts` and `secret_scanning` with a reason like "Requires paid GitHub plan for private repositories". On a paid plan, both show ADD.
>
> **Note on BRANCH_PROTECTION:** On a free plan, private repos show SKIP for branch protection. On a paid plan, ADD rows appear.

Type `y` at the prompt.

**Expected — success banner:**

```
╭─ Done ────────────────────────────────────────────╮
│  Repository created successfully!                  │
│  https://github.com/YOUR_USERNAME/gsr-test-private-01 │
│                                                    │
│  HTTPS: git remote add origin https://...          │
│  SSH:   git remote add origin git@github.com:...   │
╰───────────────────────────────────────────────────╯
```

**Verify on GitHub:**
- Go to `https://github.com/YOUR_USERNAME/gsr-test-private-01`
- Settings → General: confirm Wiki disabled, Projects disabled, Squash merge ON, Merge commits OFF
- Settings → Branches (paid plan): confirm branch protection rule exists on `main`
- Settings → Actions → General: confirm "Allow select actions and reusable workflows" is selected with "Allow actions created by GitHub" and "Allow actions by Marketplace verified creators" checked, "Read repository contents and packages" selected, and "Require SHA pinning" is checked

### 1.2 Attempt to create the same repo again

```bash
gh-safe-repo create YOUR_USERNAME/gsr-test-private-01
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
gh-safe-repo create YOUR_USERNAME/gsr-test-public-01 --public
```

**Expected plan:** Similar to 1.1 but:
- REPO `create_repo` shows `public` instead of `private`
- SECURITY `secret_scanning` shows SKIP with reason "Automatically enabled for public repositories by GitHub"
- SECURITY `dependabot_alerts` shows ADD (free plan supports public repos)
- BRANCH_PROTECTION shows ADD rows (free plan supports public repos)

Type `y` at the prompt.

**Verify on GitHub:**
- Repo is public
- Branch protection is enabled on `main`
- Dependabot alerts enabled (Security tab shows "Dependabot alerts: Enabled")

---

## 3. create --from Workflow (Mirror Repo)

This creates a source repo, puts a test file in it, then mirrors it to a new repo.

### 3.1 Create a source private repo with content

```bash
gh-safe-repo create YOUR_USERNAME/gsr-test-source-01
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

### 3.2 Mirror to new public repo

```bash
gh-safe-repo create YOUR_USERNAME/gsr-test-public-from-01 --from YOUR_USERNAME/gsr-test-source-01 --public
```

**Expected — scan output first (since source has content):**

```
Running pre-flight security scan... (truffleHog v3.x.x)
  No issues found.
```

Then the plan table, then prompt. Type `y`.

**Expected — success banner.**

**Verify on GitHub:**
- `gsr-test-public-from-01` is public
- README.md is present (code was mirrored)
- Branch protection enabled on `main`

### 3.3 Mirror to new private repo (default)

```bash
gh-safe-repo create YOUR_USERNAME/gsr-test-private-from-01 --from YOUR_USERNAME/gsr-test-source-01
```

**Expected — scan output first, then plan table, then prompt. Type `y`.**

**Verify on GitHub:**
- `gsr-test-private-from-01` is private
- README.md is present (code was mirrored)

### 3.4 --from with non-existent source

```bash
gh-safe-repo create YOUR_USERNAME/gsr-test-bad --from YOUR_USERNAME/gsr-test-does-not-exist-xyz --public
```

**Expected:**

```
Error: Source repo 'YOUR_USERNAME/gsr-test-does-not-exist-xyz' does not exist.
```

### 3.5 --from with bare repo name (no owner/) is rejected

```bash
gh-safe-repo create YOUR_USERNAME/gsr-test-bad --from gsr-test-source-01 --public
```

**Expected:**

```
Error: Use owner/repo format (e.g. myuser/gsr-test-source-01)
```

---

## 4. create --local Workflow (Push Local Directory/Repo)

### 4.1 Push a plain local directory (no .git)

```bash
mkdir /tmp/gsr-local-dir-test
echo "# Hello" > /tmp/gsr-local-dir-test/README.md
echo "secret = not_a_real_secret" > /tmp/gsr-local-dir-test/config.txt
gh-safe-repo create YOUR_USERNAME/gsr-test-local-dir-01 --local /tmp/gsr-local-dir-test
```

**Expected — scan output:**

```
Running pre-flight security scan... (...)
```

The `config.txt` file may or may not trigger a WARNING depending on the regex patterns (the word "secret" may match a WARNING pattern). If findings are shown, type `y` to continue.

After plan prompt, type `y`.

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
gh-safe-repo create YOUR_USERNAME/gsr-test-local-git-01 --local /tmp/gsr-local-git-test
```

Type `y` at the plan prompt.

**Verify:** README.md on GitHub shows "v2". Git log on GitHub shows 2 commits.

### 4.3 --local with non-existent path

```bash
gh-safe-repo create YOUR_USERNAME/gsr-test-bad --local /tmp/path-does-not-exist-xyz
```

**Expected:**

```
Error: --local: '/tmp/path-does-not-exist-xyz' is not a directory
```

Exits with status 2 (no API calls).

### 4.4 --local and --from are mutually exclusive

```bash
gh-safe-repo create YOUR_USERNAME/gsr-test-bad --local /tmp --from YOUR_USERNAME/gsr-test-source-01 --public
```

**Expected:**

```
Error: --local and --from are mutually exclusive
```

---

## 5. fix Workflow (Apply Safe Defaults to Existing Repo)

### 5.1 Fix a fully-configured repo (all settings already correct)

Fix one of the repos created in section 1 — it was already configured with safe defaults.

```bash
gh-safe-repo fix YOUR_USERNAME/gsr-test-private-01
```

**Expected — plan table:** All rows show SKIP with reason "already set" or "no change needed". No ADD or UPDATE rows (unless your GitHub plan differs from what was applied).

**Expected output:**

```
Already at desired state — nothing to do.
```

(No `Apply N changes?` prompt — the tool exits cleanly when there's nothing to do.)

### 5.2 Fix a repo with settings drift

Create a repo manually via `gh` CLI (without safe defaults), then fix it:

```bash
gh repo create gsr-test-fix-target-01 --private --confirm
gh-safe-repo fix YOUR_USERNAME/gsr-test-fix-target-01
```

**Expected — plan table shows UPDATE rows** for all settings that differ from safe defaults:
- `has_wiki`: true → false
- `has_projects`: true → false
- `sha_pinning_required`: false → true
- `default_workflow_permissions`: write → read
- `can_approve_pull_request_reviews`: true → false
- (Plus branch protection ADD if paid plan)

```
Apply N changes to YOUR_USERNAME/gsr-test-fix-target-01? [y/N]:
```

Type `y`.

**Expected — success banner:**

```
╭─ Done ──────────────────────────────────────────────╮
│  Repository updated successfully!                    │
│  https://github.com/YOUR_USERNAME/gsr-test-fix-target-01 │
╰─────────────────────────────────────────────────────╯
```

**Verify on GitHub:** Settings match safe defaults.

### 5.3 Fix with --yes skips confirmation

```bash
gh repo create gsr-test-fix-yes-01 --private --confirm
gh-safe-repo fix YOUR_USERNAME/gsr-test-fix-yes-01 --yes
```

**Expected:** Plan table is shown, changes are applied immediately **without** the "Apply N changes?" prompt.

**Verify on GitHub:** Settings match safe defaults.

### 5.4 Fix with --dry-run shows plan but applies nothing

```bash
gh-safe-repo fix YOUR_USERNAME/gsr-test-fix-target-01 --dry-run
```

**Expected:** Plan table is shown, then:

```
Dry run — no changes made.
```

### 5.5 Fix a non-existent repo

```bash
gh-safe-repo fix YOUR_USERNAME/gsr-test-does-not-exist-xyz
```

**Expected:**

```
Error: Repository 'YOUR_USERNAME/gsr-test-does-not-exist-xyz' does not exist. Use `gh-safe-repo create` to create it.
```

### 5.6 Fix has no secret scanning

Unlike the old `--audit` mode, `fix` does **not** clone the repo or run a pre-flight scan. Verify this by running fix on a repo with content — no "Running pre-flight security scan" message should appear.

```bash
gh-safe-repo fix YOUR_USERNAME/gsr-test-public-from-01 --dry-run
```

**Expected:** Plan table is shown immediately. No "Scanning" or "Running pre-flight security scan" output.

---

## 6. scan Workflow (Standalone Security Scan)

### 6.1 Scan a clean directory

```bash
mkdir /tmp/gsr-clean-scan
echo "# Hello World" > /tmp/gsr-clean-scan/README.md
gh-safe-repo scan /tmp/gsr-clean-scan
```

**Expected:**

```
Scanning /tmp/gsr-clean-scan...
  No issues found.
```

Exit code 0.

### 6.2 Scan a directory with a fake secret

```bash
mkdir /tmp/gsr-secret-scan
echo 'AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE' > /tmp/gsr-secret-scan/creds.env
gh-safe-repo scan /tmp/gsr-secret-scan
```

**Expected:**

```
Scanning /tmp/gsr-secret-scan...

  [CRITICAL] ...  creds.env:1  ...
```

Exit code 1 (critical findings present).

### 6.3 Scan a directory with an email address

```bash
mkdir /tmp/gsr-email-scan
echo 'Contact: user@example.com' > /tmp/gsr-email-scan/README.md
gh-safe-repo scan /tmp/gsr-email-scan
```

**Expected:**

```
Scanning /tmp/gsr-email-scan...

  [WARNING] ...  README.md:1  email_address  user@example.com
```

### 6.4 Scan a directory with a large file

```bash
mkdir /tmp/gsr-large-scan
dd if=/dev/urandom bs=1M count=150 of=/tmp/gsr-large-scan/bigfile.bin 2>/dev/null
gh-safe-repo scan /tmp/gsr-large-scan
```

**Expected:**

```
Scanning /tmp/gsr-large-scan...

  [WARNING] ...  bigfile.bin  large_file  150.0 MB (limit: 100 MB)
```

### 6.5 Scan a directory with a TODO comment

```bash
mkdir /tmp/gsr-todo-scan
echo '// TODO: remove hardcoded password before shipping' > /tmp/gsr-todo-scan/app.js
gh-safe-repo scan /tmp/gsr-todo-scan
```

**Expected:**

```
Scanning /tmp/gsr-todo-scan...

  [INFO] ...  app.js:1  todo_comment  ...
```

### 6.6 Scan a directory with a CLAUDE.md file

```bash
mkdir /tmp/gsr-ai-scan
echo '# Instructions' > /tmp/gsr-ai-scan/CLAUDE.md
gh-safe-repo scan /tmp/gsr-ai-scan
```

**Expected:**

```
Scanning /tmp/gsr-ai-scan...

  [CRITICAL] ...  CLAUDE.md  ai_context_file  ...
```

Exit code 1.

### 6.7 Scan with no path argument

```bash
gh-safe-repo scan
```

**Expected:**

```
error: the following arguments are required: path
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
gh-safe-repo scan /tmp/gsr-history-scan
```

**Expected:**

```
Scanning /tmp/gsr-history-scan...

  [CRITICAL] ...  CLAUDE.md  ai_context_file_history  ...
```

---

## 7. --json Flag

### 7.1 JSON output for a new repo plan

```bash
gh-safe-repo create YOUR_USERNAME/gsr-test-json-01 --dry-run --json
```

> Note: `--dry-run` is used here so the JSON test doesn't create a real repo.

**Expected stdout:** A valid JSON object. Human-readable info goes to stderr.

```json
{
  "changes": [
    {
      "type": "add",
      "category": "repository",
      "key": "has_wiki",
      "old": null,
      "new": false,
      "reason": null
    },
    ...
  ],
  "summary": {
    "add": 5,
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
gh-safe-repo create YOUR_USERNAME/gsr-test-json-01 --dry-run --json 2>/dev/null | python3 -m json.tool
```

**Expected:** Pretty-printed JSON with no errors. All info/warn output is suppressed (it went to stderr).

### 7.3 JSON for fix mode

```bash
gh-safe-repo fix YOUR_USERNAME/gsr-test-private-01 --dry-run --json 2>/dev/null | python3 -m json.tool
```

**Expected:** Valid JSON. All rows will be SKIP (repo already has safe defaults). `summary` will contain only `{"skip": N}`.

---

## 8. --debug Flag

### 8.1 Debug output shows API calls

```bash
gh-safe-repo create YOUR_USERNAME/gsr-test-debug-01 --dry-run --debug
```

**Expected:** All normal output PLUS lines like:

```
[debug] GET /user
[debug] GET /repos/YOUR_USERNAME/gsr-test-debug-01
```

No tokens or credentials appear in debug output (sanitized URLs).

---

## 9. Config File Customisation

### 9.1 Custom config with different settings

```bash
mkdir -p /tmp/gsr-config-test
cat > /tmp/gsr-config-test/gh-safe-repo.ini << 'EOF'
[repo]
has_issues = false
has_wiki = true

[branch_protection]
required_approving_reviews = 2
EOF
gh-safe-repo create YOUR_USERNAME/gsr-test-config-01 --config /tmp/gsr-config-test/gh-safe-repo.ini --dry-run
```

**Expected plan differences vs. defaults:**
- `has_issues` shows ADD false (or UPDATE true → false in fix mode)
- `has_wiki` shows SKIP (desired=true matches GitHub default=true, no change needed)
- `required_approving_reviews` shows 2 instead of 1 in branch protection

### 9.2 Config file that doesn't exist

```bash
gh-safe-repo create YOUR_USERNAME/gsr-test-config-01 --config /tmp/path-that-does-not-exist/gh-safe-repo.ini --dry-run
```

**Expected:** Tool runs normally using built-in safe defaults (missing config file is not an error — it's treated as "use defaults").

---

## 10. Plan-Level Gating

These tests verify the tool correctly detects GitHub plan level and gates features.

> **Note:** `sha_pinning_required` is **not** plan-gated or visibility-gated. It appears as an ADD in every create plan.

### 10.1 Free plan — private repo skips branch protection and security

(Only relevant if you are on a free GitHub plan.)

```bash
gh-safe-repo create YOUR_USERNAME/gsr-test-free-plan-private --dry-run
```

**Expected plan:** BRANCH_PROTECTION and SECURITY rows show SKIP with reason text like:
- "Requires paid GitHub plan for private repositories"

### 10.2 Free plan — public repo gets branch protection and Dependabot

```bash
gh-safe-repo create YOUR_USERNAME/gsr-test-free-plan-public --public --dry-run
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
gh-safe-repo create YOUR_USERNAME/gsr-test-abort-01 --local /tmp/gsr-abort-test
```

**Expected — scan output:**

```
Running pre-flight security scan... (...)

  [CRITICAL] ...  leak.txt:1  ...

Critical issues found. Continue anyway? [y/N]:
```

Type `n`.

**Expected:**

```
Aborted by user.
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
cat > /tmp/gsr-ruleset.ini << 'EOF'
[branch_protection]
use_rulesets = true
EOF
gh-safe-repo create YOUR_USERNAME/gsr-test-rulesets-01 --public --config /tmp/gsr-ruleset.ini
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
  gsr-test-fix-target-01 \
  gsr-test-fix-yes-01 \
  gsr-test-debug-01 \
  gsr-test-config-01 \
  gsr-test-free-plan-private \
  gsr-test-free-plan-public \
  gsr-test-abort-01 \
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

| Test | create | --from | --local | fix | scan | --json | --yes | Free plan | Paid plan |
|------|:------:|:------:|:-------:|:---:|:----:|:------:|:-----:|:---------:|:---------:|
| 0.4 No subcommand | | | | | | | | Y | Y |
| 0.5 Bare repo name | Y | | | | | | | Y | Y |
| 0.6 Wrong owner | Y | | | | | | | Y | Y |
| 1.1 Basic private create | Y | | | | | | | Y | Y |
| 1.2 Duplicate repo error | Y | | | | | | | Y | Y |
| 2.1 Basic public create | Y | | | | | | | Y | Y |
| 3.2 Mirror to public | Y | Y | | | Y | | | Y | Y |
| 3.3 Mirror to private repo | Y | Y | | | | | | Y | Y |
| 3.4 --from bad source | Y | Y | | | | | | Y | Y |
| 3.5 --from bare name | Y | Y | | | | | | Y | Y |
| 4.1 --local plain dir | Y | | Y | | Y | | | Y | Y |
| 4.2 --local git repo | Y | | Y | | Y | | | Y | Y |
| 4.3 --local bad path | Y | | Y | | | | | Y | Y |
| 4.4 --local + --from error | Y | Y | Y | | | | | Y | Y |
| 5.1 Fix fully-configured | | | | Y | | | | Y | Y |
| 5.2 Fix with drift | | | | Y | | | | Y | Y |
| 5.3 Fix with --yes | | | | Y | | | Y | Y | Y |
| 5.4 Fix with --dry-run | | | | Y | | | | Y | Y |
| 5.5 Fix non-existent repo | | | | Y | | | | Y | Y |
| 5.6 Fix has no scanning | | | | Y | | | | Y | Y |
| 6.2 Scan fake secret | | | | | Y | | | Y | Y |
| 6.3 Scan email | | | | | Y | | | Y | Y |
| 6.4 Scan large file | | | | | Y | | | Y | Y |
| 6.5 Scan TODO comment | | | | | Y | | | Y | Y |
| 6.6 Scan CLAUDE.md | | | | | Y | | | Y | Y |
| 6.8 Scan deleted history | | | | | Y | | | Y | Y |
| 7.1 --json output | Y | | | | | Y | | Y | Y |
| 7.2 --json pipeable | Y | | | | | Y | | Y | Y |
| 7.3 --json fix | | | | Y | | Y | | Y | Y |
| 9.1 Custom config | Y | | | | | | | Y | Y |
| 11.1 Abort on findings | Y | | Y | | Y | | | Y | Y |
| 12.1 Rulesets API | Y | | | | | | | | Y |

---

## Full Manual Test Run — 2026-03-25

Sections 0–13 executed on a free GitHub plan against the `cli-subcommands` branch.
All tests passed after fixes were applied. Cleanup (section 13) completed — all
test repos deleted and `/tmp/gsr-*` removed.

### Bugs Found and Fixed

Six commits, eight bugs total. Each commit message references the
MANUAL_TESTING.md section and line number where the bug was discovered.

| Commit | Section | Bug | Fix |
|--------|---------|-----|-----|
| `c81578a` | 0.6 (L78) | Owner check was case-sensitive (`ariesq` ≠ `AriESQ`) | `.lower()` comparison in `build_context()` |
| `c81578a` | 1.1 (L94) | `create` applied changes without confirmation prompt | Added `[y/N]` prompt + `--yes`/`-y` flag |
| `904e281` | 2.1 (L163) | `_resolve_branches` picked up CWD git branch (`cli-subcommands`) | Removed `git symbolic-ref` fallback; branches come only from explicit sources |
| `f787a67` | 5.1 (L340) | `fork_pr_approval_policy` showed perpetual UPDATE on private repos | Pass `is_public` to `ActionsPlugin`; skip fork endpoint on private repos |
| `b2bb638` | 6.2 (L460) | Regex secret patterns skipped when trufflehog succeeded | Always run regex alongside trufflehog with dedup; add `--results=verified,unverified` |
| `f9ac3cf` | 6.5 (L510) | TODO pattern only matched `# TODO`, missed `// TODO` etc. | Broadened regex to match any comment style |
| `f9ac3cf` | 9.2 (L676) | `--config` with non-existent path silently used defaults | `ConfigManager` raises `ConfigError` when explicit path missing |
| `b82ec36` | 12.1 (L760) | Rulesets POST returned 422; plain creates left repo empty | Added `require_code_owner_review` param; `auto_init=True` for plain creates |

### Tips, Tricks, and Learnings

These notes are relevant both for future manual testing and for designing E2E tests.

**GitHub token scopes matter.** The default `gh auth login` token cannot delete
repos. You need `gh auth refresh -h github.com -s delete_repo` before cleanup.
An E2E harness should acquire this scope upfront or document it as a prerequisite.

**trufflehog is a credential verifier, not a pattern scanner.** It requires paired
credentials (e.g. both AWS access key ID + secret access key) and will not flag
standalone patterns. Our regex scanner fills this gap. When writing scan test
fixtures, use realistic paired credentials or test the regex path directly.
See: https://github.com/trufflesecurity/trufflehog/issues/2940

**trufflehog scanner availability varies.** Results differ depending on whether
trufflehog is available (native or container). With trufflehog: only paired/verified
secrets are found. Without: regex catches standalone patterns. E2E tests should
cover both paths — run with trufflehog available and with `trufflehog_mode = off`.

**Heredoc copy-paste from markdown breaks in terminals.** Indented `EOF` blocks
from rendered markdown get pasted with leading spaces, causing the heredoc to
never terminate. Use `echo -e "line1\nline2" > file` instead for test fixture
creation, or write a setup script.

**GitHub defaults sometimes match safe defaults.** `has_wiki`, `has_issues`,
`default_workflow_permissions`, and `can_approve_pull_request_reviews` often
show SKIP even on un-configured repos because GitHub's defaults already align.
E2E assertions should not hardcode which settings show UPDATE vs SKIP — check
that actionable changes are non-negative and that re-running produces all SKIPs.

**Empty repos can't have branch protection.** If `auto_init` is false and no
code is pushed, the repo has zero branches. Both classic branch protection and
rulesets require at least one branch to exist. E2E tests for branch protection
must ensure the repo is initialized first.

**`fork_pr_approval_policy` is private-repo-hostile.** The GitHub API returns
422 for this endpoint on private repos. Both `fetch_current_state()` and `plan()`
must guard on `is_public`. E2E tests should verify this shows SKIP on private
repos and UPDATE/SKIP on public repos.

**Rulesets and classic branch protection are separate systems.** Creating
protection via rulesets does not populate the classic branch protection API (and
vice versa). A `fix` run after a rulesets-based `create` will see no classic
protection and try to apply it. This is expected — the two APIs are independent.
E2E tests should not mix the two unless explicitly testing this interaction.

**The user's GitHub default branch name matters.** Some accounts default to
`master`, others to `main`. The tool handles this via the API response
(`default_branch` field), but test assertions should not hardcode branch names.

**Idempotency is the key acceptance criterion.** For every `create` or `fix`,
run the operation twice. The second run must show 0 actionable changes and all
SKIPs. This is the single most important E2E assertion.

### E2E Test Design Notes

The manual test sections map naturally to E2E test cases. Key considerations:

- **Fixture repos are expensive.** Each `create` makes 4–8 API calls. Batch
  related assertions against the same repo where possible (e.g. create + fix
  idempotency in one test).
- **Cleanup must be robust.** Use `gh repo delete` in teardown with
  `delete_repo` scope. A failed test must not leave repos behind.
- **Plan output is the primary interface.** Parse `--json` output for assertions
  rather than scraping table output. The JSON schema is stable.
- **Two scanner configurations.** Test with trufflehog available and with
  `trufflehog_mode = off` to cover both the trufflehog and regex paths.
- **Free vs. paid plan gating.** Most testers will be on free plans. Tests
  should assert that free+private repos show SKIP for gated features, and
  free+public repos show ADD/UPDATE.
