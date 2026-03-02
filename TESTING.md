# Manual Testing: git-filter-file.sh

All tests run against throwaway repos in `/tmp`. Never run against a real repo.

```bash
SCRIPT=/path/to/git-filter-file.sh   # set this to your actual path
```

---

## Setup helpers

```bash
# Create a fresh test repo with a few commits
make_repo() {
    local dir="/tmp/test-gff-$$"
    git init "$dir"
    cd "$dir"
    echo "normal content" > readme.txt
    git add . && git commit -m "init"
    echo "secret=abc123" > secret.txt
    git add . && git commit -m "add secret"
    echo "more normal" >> readme.txt
    echo "updated secret" >> secret.txt
    git add . && git commit -m "update both"
    echo "Repo at: $dir"
}
```

---

## 1. Help / usage

```bash
$SCRIPT --help
$SCRIPT -h
$SCRIPT          # no args
$SCRIPT a b      # too many args
$SCRIPT --bogus  # unknown flag
```

**Expected:**
- `--help` / `-h` — prints usage and exits 0
- no args — `Error: Missing required argument` + usage, exits 1
- two positional args — `Error: Too many arguments`, exits 1
- `--bogus` — `Error: Unknown option`, exits 1

---

## 2. Not in a git repo

```bash
cd /tmp
$SCRIPT some-file.txt
```

**Expected:** `Error: Not inside a git repository.`, exits 1

---

## 3. Worktree guard

```bash
# Create a repo with a worktree
git init /tmp/wt-main && cd /tmp/wt-main
echo "hi" > f.txt && git add . && git commit -m "init"
git worktree add /tmp/wt-linked -b test-branch
cd /tmp/wt-linked
echo "secret" > s.txt && git add . && git commit -m "add"
$SCRIPT s.txt
```

**Expected:** `Error: Running inside a git worktree is not supported.`, exits 1

---

## 4. File does not exist on disk

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
$SCRIPT nonexistent.txt
```

**Expected:** `Error: File does not exist: ...nonexistent.txt`, exits 1

---

## 5. File exists on disk but is not tracked

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
echo "untracked" > untracked.txt
$SCRIPT untracked.txt
```

**Expected:** `Error: '...' is not tracked by git.` with hint about `git rm --cached`, exits 1

---

## 6. File tracked but has no commit history (staged only)

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
echo "staged" > staged.txt
git add staged.txt
$SCRIPT staged.txt
```

**Expected:** `Error: '...' has no commit history to scrub.` with hint about `git rm --cached`, exits 1

---

## 7. Dirty working tree

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
echo "dirty" >> readme.txt   # unstaged change
$SCRIPT secret.txt
```

```bash
# Also test staged dirty
git add readme.txt
$SCRIPT secret.txt
```

**Expected:** `Error: Working tree has uncommitted changes.` with stash instructions, exits 1. Script does NOT reach filter-branch.

---

## 8. Dry run

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
$SCRIPT --dry-run secret.txt
```

**Expected:**
- Prints banner with file name and commit count
- Shows the `git filter-branch` command that would run
- Prints `-- dry run: no changes made --`
- Exits 0
- `git log --oneline` unchanged — no commits added or removed
- `secret.txt` still exists with original content

---

## 9. Happy path — basic scrub

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
ORIGINAL=$(cat secret.txt)
$SCRIPT secret.txt   # confirm at the prompt
```

**Expected (step by step):**

1. Banner shows `File: secret.txt`, `Commits: 2 commit(s)` (it appeared in "add secret" and "update both")
2. No warnings (clean tree, no remotes)
3. Prompts `Proceed? [y/N]` — enter `y`
4. Prints `Backup saved: .git/filter-file-backups/<timestamp>_secret.txt`
5. Prints rewriting message, then `✓ History rewritten`
6. Prints gc message, then `✓ Objects purged`
7. Prints `✓ Re-committed 'secret.txt' as a fresh single commit`
8. Prints "Done. Required next steps" — no remotes section

**Verify after:**

```bash
# File exists with original content
cat secret.txt   # should match $ORIGINAL

# Only one commit touches secret.txt
git log --oneline -- secret.txt   # exactly 1 line

# That commit is the re-add commit
git log --oneline -- secret.txt | grep "re-added\|history scrub"

# secret.txt content is gone from all prior history
git log --all --oneline -- secret.txt   # still just 1 line

# refs/original/ is fully cleaned up
git for-each-ref refs/original/   # no output

# readme.txt history is intact
git log --oneline -- readme.txt   # should show original commits

# backup file exists
ls .git/filter-file-backups/
```

---

## 10. File in a subdirectory

```bash
dir="/tmp/test-subdir-$$"
git init "$dir" && cd "$dir"
mkdir -p secrets/nested
echo "normal" > readme.txt && git add . && git commit -m "init"
echo "key=xyz" > secrets/nested/api.key
git add . && git commit -m "add key"
$SCRIPT secrets/nested/api.key   # confirm
```

**Expected:** Same as happy path. Verify:

```bash
cat secrets/nested/api.key   # content preserved
git log --oneline -- secrets/nested/api.key   # 1 commit
```

---

## 11. --prune-empty: commit that only added the target file is dropped

```bash
dir="/tmp/test-prune-$$"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
echo "secret" > only-secret.txt
git add only-secret.txt && git commit -m "only the secret file"   # this commit becomes empty
echo "more normal" >> readme.txt && git add . && git commit -m "update readme"
$SCRIPT only-secret.txt   # confirm
```

**Expected:**
- The "only the secret file" commit is pruned entirely (it becomes empty after removing the file)
- `git log --oneline` shows `init` → `update readme` → re-add commit (3 commits, not 4)
- `only-secret.txt` content preserved in the re-add commit

---

## 12. Repo with remotes — force-push warning shown

```bash
dir="/tmp/test-remote-$$"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
echo "secret" > secret.txt && git add . && git commit -m "add secret"
git remote add origin https://github.com/example/fake.git
$SCRIPT --dry-run secret.txt
```

**Expected:** Dry run output includes the `Warning: This repo has remote(s): origin` block with force-push instructions. (Use `--dry-run` to avoid needing a real remote.)

---

## 13. Backup file is byte-for-byte identical to original

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
CHECKSUM=$(shasum secret.txt | awk '{print $1}')
$SCRIPT secret.txt   # confirm
BACKUP=$(ls -t .git/filter-file-backups/ | head -1)
BACKUP_CHECKSUM=$(shasum ".git/filter-file-backups/$BACKUP" | awk '{print $1}')
[ "$CHECKSUM" = "$BACKUP_CHECKSUM" ] && echo "PASS: checksums match" || echo "FAIL"
```

**Expected:** `PASS: checksums match`

---

## 14. Re-running the script on the same file (idempotent re-run)

After a successful run, the file has only 1 commit in history (the re-add). Run again:

```bash
$SCRIPT secret.txt   # confirm
```

**Expected:**
- Banner shows `Commits: 1 commit(s)`
- Runs successfully
- After: still 1 commit touching the file (the new re-add replaces the previous one)
- Content preserved

---

## 15. Binary file

```bash
dir="/tmp/test-binary-$$"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
dd if=/dev/urandom bs=1024 count=10 of=secret.bin 2>/dev/null
CHECKSUM=$(shasum secret.bin | awk '{print $1}')
git add . && git commit -m "add binary"
$SCRIPT secret.bin   # confirm
```

**Expected:** Script completes successfully. Verify:

```bash
[ "$(shasum secret.bin | awk '{print $1}')" = "$CHECKSUM" ] && echo "PASS" || echo "FAIL"
git log --oneline -- secret.bin   # 1 commit
```
