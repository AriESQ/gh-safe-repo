# Manual Testing: scrub-ai-context.sh

All tests run against throwaway repos in `/tmp`. Never run against a real repo.

`scrub-ai-context` is now a thin wrapper around `git-filter-file.sh`. It calls
`git-filter-file --keep --force --yes . <target>` for each detected file.

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

## 4. Auto-detect: no known AI context files in history

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

## 5. Dry run — single explicit file

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
$SCRIPT --dry-run CLAUDE.md
```

**Expected:**
- Banner shows `scrub-ai-context (dry run)` and lists `CLAUDE.md`
- Passes `--dry-run` through to `git-filter-file` which shows filter-branch commands
- Exits 0
- No changes made — `git log --oneline` unchanged, `CLAUDE.md` intact

---

## 6. Dry run — auto-detect

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
$SCRIPT --dry-run
```

**Expected:**
- Prints "No paths specified — scanning..."
- Finds and lists `CLAUDE.md` with its commit count
- Shows dry-run plan via `git-filter-file --dry-run`
- No changes made

---

## 7. Happy path — single file scrub

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
ORIGINAL=$(cat CLAUDE.md)
$SCRIPT CLAUDE.md   # confirm at the prompt
```

**Expected (step by step):**

1. Banner lists `CLAUDE.md` as a target
2. Prompts `Proceed? [y/N]` — enter `y`
3. Calls `git-filter-file --keep --force --yes . CLAUDE.md`
4. git-filter-file prints backup, rewrite, gc, and re-commit messages
5. `✓ Completed: CLAUDE.md`

**Verify after:**

```bash
# File exists with original content
cat CLAUDE.md   # should match $ORIGINAL

# Only one commit touches CLAUDE.md
git log --oneline -- CLAUDE.md   # exactly 1 line

# That commit is the re-add commit
git log --oneline -- CLAUDE.md | grep -i "history scrub\|filter-file"

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

## 8. Happy path — auto-detect, single file

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
$SCRIPT   # confirm at the prompt
```

**Expected:** Same result as test 7, but the target was found automatically rather than specified on the command line.

---

## 9. Multi-file scrub — files and a directory

```bash
dir=$(make_multi_repo | tail -1 | awk '{print $3}')
cd "$dir"
$SCRIPT CLAUDE.md AGENTS.md .cursor   # confirm
```

**Expected:**
- Banner lists all three targets
- Runs `git-filter-file` three times (one per target)
- Each target: backup → rewrite → gc → re-add
- `✓ Completed: CLAUDE.md`, `✓ Completed: AGENTS.md`, `✓ Completed: .cursor`

**Verify after:**

```bash
# All three exist with original content
cat CLAUDE.md
cat AGENTS.md
cat .cursor/settings.json

# Each has exactly one commit in history (its re-add)
git log --oneline -- CLAUDE.md    # 1 line
git log --oneline -- AGENTS.md    # 1 line
git log --oneline -- .cursor      # 1 line

# Objects purged
git for-each-ref refs/original/   # no output
```

---

## 10. History-only file (deleted from HEAD before scrub)

```bash
dir="/tmp/test-sac-deleted-$$"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
echo "claude rules" > CLAUDE.md && git add . && git commit -m "add CLAUDE.md"
git rm CLAUDE.md && git commit -m "remove CLAUDE.md"
$SCRIPT CLAUDE.md   # confirm
```

**Expected:**
- The wrapper detects the file doesn't exist on disk and calls
  `git-filter-file --force --yes . CLAUDE.md` (without `--keep`)
- git-filter-file scrubs history and deletes the file (already absent)
- `git log --all --oneline -- CLAUDE.md` → no output (gone from all history)
- `CLAUDE.md` does not exist on disk (was already absent)

---

## 11. --push flag (requires a bare remote)

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
- Scrub runs as normal via git-filter-file
- After all targets, automatically runs `git push --force-with-lease --all` and `--tags`
- `✓ Force-push complete.` printed
- "Done" section does NOT include the manual push instruction

**Verify:**
```bash
# Local and remote history agree
git log --oneline
git log --oneline origin/master 2>/dev/null || git log --oneline origin/main
```

---

## 12. --push flag with no remotes

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
$SCRIPT --push CLAUDE.md   # confirm
```

**Expected:** `Warning: No remotes configured — skipping push.` Push is skipped gracefully.

---

## 13. Abort at confirmation prompt

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

## 14. Backup is byte-for-byte identical to original

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

## 15. Re-running the script (idempotent)

After a successful run, the file has only 1 commit in history. Run again:

```bash
$SCRIPT CLAUDE.md   # confirm
```

**Expected:**
- git-filter-file shows `Commits: 1 commit(s)`
- Runs successfully
- After: still 1 commit touching CLAUDE.md (new re-add replaces previous one)
- Content preserved
