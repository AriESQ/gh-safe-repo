# Manual Testing: scrub-ai-context.sh

All tests run against throwaway repos in `/tmp`. Never run against a real repo.

```bash
SCRIPT=/path/to/scrub-ai-context.sh   # set this to your actual path
```

---

## Setup helpers

```bash
# Create a fresh test repo with CLAUDE.md in history
make_repo() {
    local dir="/tmp/test-sac-$$"
    git init "$dir"
    cd "$dir"
    echo "normal content" > readme.txt
    git add . && git commit -m "init"
    echo "# Claude instructions" > CLAUDE.md
    git add . && git commit -m "add CLAUDE.md"
    echo "more normal" >> readme.txt
    echo "## More instructions" >> CLAUDE.md
    git add . && git commit -m "update both"
    echo "Repo at: $dir"
}

# Create a repo with multiple AI context files
make_multi_repo() {
    local dir="/tmp/test-sac-multi-$$"
    git init "$dir"
    cd "$dir"
    echo "normal" > readme.txt
    git add . && git commit -m "init"
    echo "claude rules" > CLAUDE.md
    echo "agent rules" > AGENTS.md
    mkdir .cursor && echo "cursor config" > .cursor/settings.json
    git add . && git commit -m "add AI context files"
    echo "Repo at: $dir"
}
```

---

## 1. Help / usage

```bash
$SCRIPT --help
$SCRIPT -h
```

**Expected:** Prints usage with options, known file list, examples, and post-run instructions. Exits 0.

---

## 2. Unknown option

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
$SCRIPT --bogus
```

**Expected:** `Error: Unknown option: --bogus` + usage, exits 1.

---

## 3. Not in a git repo

```bash
cd /tmp
$SCRIPT CLAUDE.md
```

**Expected:** `Error: Not inside a git repository.`, exits 1.

---

## 4. Worktree guard

```bash
git init /tmp/wt-main-$$ && cd /tmp/wt-main-$$
echo "claude rules" > CLAUDE.md && git add . && git commit -m "init"
git worktree add /tmp/wt-linked-$$ -b test-branch
cd /tmp/wt-linked-$$
$SCRIPT CLAUDE.md
```

**Expected:** `Error: Running inside a git worktree is not supported.`, exits 1.

---

## 5. Auto-detect: no known AI context files in history

```bash
dir="/tmp/test-sac-empty-$$"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
$SCRIPT
```

**Expected:**
- Prints "No paths specified — scanning for known AI context files..."
- Prints `✓ No known AI context files found in this repository's history.`
- Exits 0. No changes made.

---

## 6. Target has no commit history

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
echo "agents" > AGENTS.md
$SCRIPT AGENTS.md
```

**Expected:** `Error: 'AGENTS.md' has no commit history to scrub.` with staged-only hint, exits 1.

---

## 7. Dirty working tree

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
echo "dirty" >> readme.txt   # unstaged change
$SCRIPT CLAUDE.md
```

```bash
# Also test staged dirty
git add readme.txt
$SCRIPT CLAUDE.md
```

**Expected:** `Error: Working tree has uncommitted changes.` with stash instructions, exits 1. No filter-branch is run.

---

## 8. Dry run — single explicit file

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
$SCRIPT --dry-run CLAUDE.md
```

**Expected:**
- Banner shows `scrub-ai-context (dry run)`
- Lists `CLAUDE.md  (2 commit(s))`
- Shows the `git filter-branch` command and re-add steps
- Prints `-- dry run: no changes made --`
- Exits 0
- `git log --oneline` unchanged — no commits added or removed
- `CLAUDE.md` still exists with original content

---

## 9. Dry run — auto-detect

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
$SCRIPT --dry-run
```

**Expected:**
- Prints "No paths specified — scanning..."
- Finds and lists `CLAUDE.md` with its commit count
- Shows dry-run plan
- No changes made

---

## 10. Happy path — single file scrub

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
ORIGINAL=$(cat CLAUDE.md)
$SCRIPT CLAUDE.md   # confirm at the prompt
```

**Expected (step by step):**

1. Banner shows `CLAUDE.md  (2 commit(s))`
2. No warnings (clean tree, no remotes)
3. Prompts `Proceed? [y/N]` — enter `y`
4. Prints `✓ Backup saved: .git/filter-file-backups/<timestamp>_CLAUDE.md`
5. Prints rewriting message, then `✓ History rewritten`
6. Prints gc message, then `✓ Objects purged`
7. Prints `✓ Re-committed 1 file(s) as a fresh single commit.`
8. Prints "Done. Required next steps" — no remotes section

**Verify after:**

```bash
# File exists with original content
cat CLAUDE.md   # should match $ORIGINAL

# Only one commit touches CLAUDE.md
git log --oneline -- CLAUDE.md   # exactly 1 line

# That commit is the re-add commit
git log --oneline -- CLAUDE.md | grep -i "re-added\|history scrub\|AI context"

# CLAUDE.md is gone from all prior history
git log --all --oneline -- CLAUDE.md   # still just 1 line

# refs/original/ is fully cleaned up
git for-each-ref refs/original/   # no output

# readme.txt history is intact
git log --oneline -- readme.txt   # shows original commits

# Backup file exists
ls .git/filter-file-backups/
```

---

## 11. Happy path — auto-detect, single file

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
$SCRIPT   # confirm at the prompt
```

**Expected:** Same result as test 10, but the target was found automatically rather than specified on the command line.

---

## 12. Multi-file scrub — files and a directory

```bash
dir=$(make_multi_repo | tail -1 | awk '{print $3}')
cd "$dir"
$SCRIPT CLAUDE.md AGENTS.md .cursor   # confirm
```

**Expected:**
- Banner lists all three targets with commit counts
- Single filter-branch pass removes all three
- `✓ Re-committed 3 file(s) as a fresh single commit.`

**Verify after:**

```bash
# All three exist with original content
cat CLAUDE.md
cat AGENTS.md
cat .cursor/settings.json

# Each has exactly one commit in history
git log --oneline -- CLAUDE.md    # 1 line
git log --oneline -- AGENTS.md    # 1 line
git log --oneline -- .cursor      # 1 line

# Objects purged
git for-each-ref refs/original/   # no output
```

---

## 13. History-only file (deleted from HEAD before scrub)

```bash
dir="/tmp/test-sac-deleted-$$"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
echo "claude rules" > CLAUDE.md && git add . && git commit -m "add CLAUDE.md"
git rm CLAUDE.md && git commit -m "remove CLAUDE.md"
$SCRIPT CLAUDE.md   # confirm
```

**Expected:**
- Banner shows `CLAUDE.md  (2 commit(s))`
- Note: `(not at HEAD — history-only)` shown in dim text
- After scrub: `✓ No files to re-add (all targets were history-only).`
- `git log --all --oneline -- CLAUDE.md` → no output (gone from all history)
- `CLAUDE.md` does not exist on disk (was already absent)

---

## 14. Mixed: one file at HEAD, one history-only

```bash
dir="/tmp/test-sac-mixed-$$"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
echo "claude" > CLAUDE.md && echo "agents" > AGENTS.md
git add . && git commit -m "add both"
git rm AGENTS.md && git commit -m "remove AGENTS.md"
$SCRIPT CLAUDE.md AGENTS.md   # confirm
```

**Expected:**
- Banner shows both targets; AGENTS.md marked as history-only
- After scrub: CLAUDE.md re-committed; AGENTS.md is not (was absent)
- `git log --all -- CLAUDE.md`  → 1 commit (re-add)
- `git log --all -- AGENTS.md`  → no output
- AGENTS.md does not exist on disk

---

## 15. --push flag (requires a bare remote)

```bash
# Set up a bare remote
bare="/tmp/test-sac-bare-$$"
git init --bare "$bare"

dir="/tmp/test-sac-push-$$"
git init "$dir" && cd "$dir"
git remote add origin "$bare"
echo "normal" > readme.txt && git add . && git commit -m "init"
git push -u origin master 2>/dev/null || git push -u origin main
echo "claude rules" > CLAUDE.md && git add . && git commit -m "add CLAUDE.md"
git push
$SCRIPT --push CLAUDE.md   # confirm
```

**Expected:**
- Scrub runs as normal
- After re-commit, automatically runs `git push --force-with-lease --all` and `--tags`
- `✓ Force-push complete.` printed
- "Done" section does NOT include the manual push instruction (already done)

**Verify:**
```bash
# Local and remote history agree
git log --oneline
git log --oneline origin/master 2>/dev/null || git log --oneline origin/main
```

---

## 16. --push flag with no remotes

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
$SCRIPT --push CLAUDE.md   # confirm
```

**Expected:** `Warning: No remotes configured — skipping push.` Push is skipped gracefully.

---

## 17. Repo with remotes — force-push warning shown

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
git remote add origin https://github.com/example/fake.git
$SCRIPT --dry-run CLAUDE.md
```

**Expected:** Dry-run output includes `Warning: This repo has remote(s): origin` with force-push instructions.

---

## 18. --prune-empty: commit that only added the target is dropped

```bash
dir="/tmp/test-sac-prune-$$"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
echo "claude" > CLAUDE.md
git add CLAUDE.md && git commit -m "only CLAUDE.md"   # becomes empty after scrub
echo "more" >> readme.txt && git add . && git commit -m "update readme"
$SCRIPT CLAUDE.md   # confirm
```

**Expected:**
- The "only CLAUDE.md" commit is pruned entirely
- `git log --oneline` shows: `init` → `update readme` → re-add (3 commits, not 4)
- CLAUDE.md content preserved in the re-add commit

---

## 19. Backup is byte-for-byte identical to original

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
CHECKSUM=$(shasum CLAUDE.md | awk '{print $1}')
$SCRIPT CLAUDE.md   # confirm
BACKUP=$(ls -t .git/filter-file-backups/ | head -1)
BACKUP_CHECKSUM=$(shasum ".git/filter-file-backups/$BACKUP" | awk '{print $1}')
[ "$CHECKSUM" = "$BACKUP_CHECKSUM" ] && echo "PASS: checksums match" || echo "FAIL"
```

**Expected:** `PASS: checksums match`

---

## 20. Abort at confirmation prompt

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
echo "n" | $SCRIPT CLAUDE.md
```

**Expected:**
- Prompts `Proceed? [y/N]`
- Prints `Aborted.`
- Exits 0
- No changes made — `git log --oneline` unchanged, CLAUDE.md intact

---

## 21. Re-running the script (idempotent)

After a successful run, the file has only 1 commit in history. Run again:

```bash
$SCRIPT CLAUDE.md   # confirm
```

**Expected:**
- Banner shows `Commits: 1 commit(s)`
- Runs successfully
- After: still 1 commit touching CLAUDE.md (new re-add replaces previous one)
- Content preserved
