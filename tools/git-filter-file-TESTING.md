# Manual Testing: git-filter-file.sh

All tests run against throwaway repos in `/tmp`. Never run against a real repo.

```bash
SCRIPT=/path/to/git-filter-file.sh   # set this to your actual path
```

**Prerequisite:** git-filter-repo must be resolvable — either `git filter-repo`
is installed, or `uvx` (from uv) is on `PATH`, in which case the script runs
`uvx git-filter-repo` (first use downloads it, so needs network). Check with
`git filter-repo --version` or `uvx git-filter-repo --version`.

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
$SCRIPT                  # no args
$SCRIPT . a b            # too many positional args
$SCRIPT --bogus . f.txt  # unknown flag
$SCRIPT .                # only one positional
```

**Expected (all usage errors exit 2):**
- `--help` / `-h` — prints usage and exits 0
- no args — `Error: Expected two arguments: <repo> <file>` + usage, exits 2
- three positionals — `Error: Too many arguments`, exits 2
- `--bogus` — `Error: Unknown option`, exits 2
- one positional — `Error: Expected two arguments`, exits 2

---

## 2. Not in a git repo

```bash
$SCRIPT /tmp some-file.txt
```

**Expected:** `Error: '/tmp' is not inside a git repository.` (stderr), exits 2

---

## 3. Worktree guard

```bash
# Create a repo with a worktree
git init /tmp/wt-main && cd /tmp/wt-main
echo "hi" > f.txt && git add . && git commit -m "init"
git worktree add /tmp/wt-linked -b test-branch
cd /tmp/wt-linked
echo "secret" > s.txt && git add . && git commit -m "add"
$SCRIPT . s.txt
```

**Expected:** `Error: Running inside a git worktree is not supported.` (stderr), exits 2

---

## 4. Bare filename not found (not on disk or in history)

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
$SCRIPT . nonexistent.txt
```

**Expected (stdout, exit 1):**
```
File not found on disk.
File not found in git history.
Nothing to do.
```

---

## 5. Repo-relative path not found (not on disk or in history)

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
$SCRIPT . wrong/path/secret.txt
```

**Expected (stdout, exit 1):**
```
File not found on disk.
File not found in git history.
The path is treated as repo-relative (it contains a /).
Nothing to do.
```

### 5b. Repo-relative path not in history but exists on disk

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
mkdir -p wrong/path
echo "hi" > wrong/path/newfile.txt
$SCRIPT . wrong/path/newfile.txt
```

**Expected (stdout, exit 1):**
```
File exists on disk: <repo>/wrong/path/newfile.txt
File not found in git history.
The path is treated as repo-relative (it contains a /).
Nothing to do.
```

---

## 6. Ambiguous bare filename

```bash
dir="/tmp/test-ambig-$$"
git init "$dir" && cd "$dir"
echo "a" > readme.txt && git add . && git commit -m "init"
mkdir -p a b
echo "secret" > a/key.txt && echo "secret" > b/key.txt
git add . && git commit -m "add keys"
$SCRIPT . key.txt
```

**Expected (stdout, exit 1):** `Multiple files named 'key.txt' found in history:` followed by the list, with hint to specify repo-relative path

---

## 7. File tracked but has no commit history (staged only)

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
echo "staged" > staged.txt
git add staged.txt
$SCRIPT . staged.txt
```

**Expected (stdout, exit 1):** `'...' has no commit history to scrub.` with hint about `git rm --cached`

---

## 8. Dirty working tree

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
echo "dirty" >> readme.txt   # unstaged change
$SCRIPT . secret.txt
```

```bash
# Also test staged dirty
git add readme.txt
$SCRIPT . secret.txt
```

**Expected (stderr, exit 1):** `Error: Working tree has uncommitted changes.` with stash instructions. Script does NOT reach git-filter-repo.

---

## 9. --keep requires file on disk

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
git rm secret.txt && git commit -m "remove secret"
$SCRIPT --keep . secret.txt
```

**Expected (stdout, exit 1):** `--keep requires the file to exist on disk.`

---

## 10. Dry run

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
$SCRIPT --dry-run . secret.txt
```

**Expected:**
- Prints banner with repo, file name and commit count
- Shows the git-filter-repo command that would run: `git filter-repo --force --invert-paths --path 'secret.txt'` (or `uvx git-filter-repo ...` when only uv is available), followed by a note that it also expires reflogs and repacks
- Prints `-- dry run: no changes made --`
- Exits 0
- `git log --oneline` unchanged — no commits added or removed
- `secret.txt` still exists with original content

### 10b. Dry run with --keep

```bash
cd "$(make_repo | tail -1 | awk '{print $3}')"
$SCRIPT --dry-run --keep . secret.txt
```

**Expected:**
- Banner shows `Operation: Remove from all history; re-add current content as one commit`
- Dry run output shows the re-add steps
- Exits 0, no changes made

---

## 11. Happy path — default (delete)

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
$SCRIPT . secret.txt   # confirm at the prompt
```

**Expected (step by step):**

1. Banner shows `File: secret.txt`, `Commits: 2 commit(s)` (appeared in "add secret" and "update both")
2. Banner shows `Operation: Remove from all history and working tree`
3. No warnings (clean tree, no remotes)
4. Prompts `Proceed? [y/N]` — enter `y`
5. Prints rewriting message and `Using: git filter-repo` (or `uvx git-filter-repo`), then git-filter-repo's own progress output, then `✓ History rewritten`
6. Prints `✓ Objects purged` (git-filter-repo expires reflogs and repacks itself)
7. Prints `✓ Deleted 'secret.txt' from working tree and all history.`
8. Prints "Done. Required next steps" — no remotes section

**Verify after:**

```bash
# File does not exist on disk
[ ! -f secret.txt ] && echo "PASS: deleted" || echo "FAIL: still exists"

# No commits reference secret.txt
git log --all --oneline -- secret.txt   # no output

# readme.txt history is intact
git log --oneline -- readme.txt   # should show original commits
```

---

## 12. Happy path — --keep (scrub history, preserve file)

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
ORIGINAL=$(cat secret.txt)
$SCRIPT --keep . secret.txt   # confirm at the prompt
```

**Expected (step by step):**

1. Banner shows `Operation: Remove from all history; re-add current content as one commit`
2. Prompts `Proceed? [y/N]` — enter `y`
3. Prints `Backup saved: .git/filter-file-backups/<timestamp>_secret.txt`
4. Prints rewriting message and git-filter-repo's progress output, then `✓ History rewritten`
5. Prints `✓ Objects purged`
6. Prints `✓ Re-committed 'secret.txt' as a fresh single commit`
7. Prints "Done. Required next steps"

**Verify after:**

```bash
# File exists with original content
cat secret.txt   # should match $ORIGINAL

# Only one commit touches secret.txt
git log --oneline -- secret.txt   # exactly 1 line

# That commit is the re-add commit
git log --oneline -- secret.txt | grep "history scrub"

# secret.txt content is gone from all prior history
git log --all --oneline -- secret.txt   # still just 1 line

# No backup refs left behind (git-filter-repo does not create refs/original/)
git for-each-ref refs/original/   # no output

# readme.txt history is intact
git log --oneline -- readme.txt   # should show original commits

# backup file exists
ls .git/filter-file-backups/
```

---

## 13. File in a subdirectory (repo-relative path)

```bash
dir="/tmp/test-subdir-$$"
git init "$dir" && cd "$dir"
mkdir -p secrets/nested
echo "normal" > readme.txt && git add . && git commit -m "init"
echo "key=xyz" > secrets/nested/api.key
git add . && git commit -m "add key"
$SCRIPT . secrets/nested/api.key   # confirm
```

**Expected:** File deleted from disk and history. Verify:

```bash
[ ! -f secrets/nested/api.key ] && echo "PASS" || echo "FAIL"
git log --all --oneline -- secrets/nested/api.key   # no output
```

---

## 14. Bare filename search (unique match)

```bash
dir="/tmp/test-basename-$$"
git init "$dir" && cd "$dir"
mkdir -p config
echo "normal" > readme.txt && git add . && git commit -m "init"
echo "secret=abc" > config/credentials.json
git add . && git commit -m "add creds"
$SCRIPT --dry-run . credentials.json
```

**Expected:**
- Prints `Warning: Resolved to repo path: config/credentials.json`
- Banner shows `File: config/credentials.json`
- Dry run completes successfully

---

## 15. History-only file (deleted from disk, still in history)

```bash
dir="/tmp/test-history-only-$$"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
echo "secret" > secret.txt && git add . && git commit -m "add secret"
git rm secret.txt && git commit -m "remove secret"
$SCRIPT . secret.txt   # confirm
```

**Expected:**
- File is found in history despite not being on disk
- History rewritten successfully
- No backup (file not on disk)
- Prints `✓ Removed 'secret.txt' from all history.`

**Verify after:**

```bash
git log --all --oneline -- secret.txt   # no output
```

---

## 16. Invoking from outside the repo

```bash
dir="/tmp/test-outside-$$"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
echo "secret" > secret.txt && git add . && git commit -m "add secret"
cd /tmp
$SCRIPT --dry-run "$dir" secret.txt
```

**Expected:**
- Repo resolved from first positional arg
- File found in history
- Dry run completes successfully from `/tmp`

---

## 17. Empty-commit pruning: commit that only added the target file is dropped

```bash
dir="/tmp/test-prune-$$"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
echo "secret" > only-secret.txt
git add only-secret.txt && git commit -m "only the secret file"   # this commit becomes empty
echo "more normal" >> readme.txt && git add . && git commit -m "update readme"
$SCRIPT . only-secret.txt   # confirm
```

**Expected:**
- The "only the secret file" commit is pruned entirely (it becomes empty after removing the file)
- `git log --oneline` shows `init` → `update readme` (2 commits, not 3)
- `only-secret.txt` does not exist on disk

---

## 18. Repo with remotes — force-push warning shown

```bash
dir="/tmp/test-remote-$$"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
echo "secret" > secret.txt && git add . && git commit -m "add secret"
git remote add origin https://github.com/example/fake.git
$SCRIPT --dry-run . secret.txt
```

**Expected:** Dry run output includes force-push instructions and the line
`restore remote config removed by git-filter-repo: origin`. (Use `--dry-run`
to avoid needing a real remote; answer `n` at the fetch prompt.)

### 18b. Remote is restored after a real run

git-filter-repo deletes the `origin` remote (and its tracking refs) after
rewriting. The script saves the remote and branch-upstream config beforehand
and restores it, then fetches so `--force-with-lease` has accurate tracking refs.

```bash
up="/tmp/test-restore-up-$$.git"; dir="/tmp/test-restore-$$"
git init --bare "$up"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
echo "secret" > secret.txt && git add . && git commit -m "add secret"
git remote add origin "$up" && git push -u origin HEAD
$SCRIPT --yes . secret.txt
```

**Expected:**
- git-filter-repo prints `NOTICE: Removing 'origin' remote`, then the script prints `✓ Restored remote configuration removed by git-filter-repo.` and refreshes tracking refs
- `git remote get-url origin` prints `$up`; `git rev-parse --abbrev-ref @{u}` still prints `origin/<branch>`
- Post-scrub step 2 tells you to check `git remote -v` before force-pushing
- `git push --force-with-lease --all` succeeds and `git -C "$up" log --all --oneline -- secret.txt` is then empty

Known limitation (predates the switch to git-filter-repo): `git push
--force-with-lease --tags` is rejected with `stale info` for tags that were
rewritten, because there are no remote-tracking refs for tags. Push those
tags with `git push --force origin <tag>` after checking them.

### 18c. git-filter-repo not available

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
# A PATH with git but without git-filter-repo or uvx:
mkdir -p /tmp/nofr-$$ && ln -sf "$(command -v git)" /tmp/nofr-$$/git
PATH="/tmp/nofr-$$:/usr/bin:/bin" $SCRIPT --yes . secret.txt
PATH="/tmp/nofr-$$:/usr/bin:/bin" $SCRIPT --dry-run . secret.txt
```

(If `/usr/bin` contains `git-filter-repo` or `uvx`, adjust the PATH so it does not.)

**Expected:**
- Real run: `Error: git-filter-repo not found.` plus install instructions (uv, or git-filter-repo via brew/apt/pipx), exit 1, history untouched. There is no fallback to `git filter-branch`.
- Dry run: warning that a real run would fail, the plan is still printed, exit 0.

---

## 19. --force bypasses divergence check

```bash
# Create a "remote" repo and clone it
git init --bare /tmp/test-force-remote-$$
git clone /tmp/test-force-remote-$$ /tmp/test-force-local-$$
cd /tmp/test-force-local-$$
echo "normal" > readme.txt && echo "secret" > secret.txt
git add . && git commit -m "init" && git push
# Simulate divergence: commit locally AND push a different commit to remote
echo "local change" >> readme.txt && git add . && git commit -m "local"
cd /tmp && git clone /tmp/test-force-remote-$$ /tmp/test-force-other-$$
cd /tmp/test-force-other-$$ && echo "remote change" >> readme.txt
git add . && git commit -m "remote" && git push
cd /tmp/test-force-local-$$ && git fetch
```

**Without --force:**

```bash
$SCRIPT . secret.txt
```

**Expected (stderr, exit 1):** `Error: Local branch has diverged from remote` with hint about `--force`.

**With --force:**

```bash
$SCRIPT --force --dry-run . secret.txt
```

**Expected:**
- Banner shows `Remote: diverged` in red
- Warning includes `--force enabled, upstreaming changes will be very difficult.`
- Dry run completes successfully (exits 0)

---

## 20. Backup file is byte-for-byte identical to original (--keep)

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
CHECKSUM=$(shasum secret.txt | awk '{print $1}')
$SCRIPT --keep . secret.txt   # confirm
BACKUP=$(ls -t .git/filter-file-backups/ | head -1)
BACKUP_CHECKSUM=$(shasum ".git/filter-file-backups/$BACKUP" | awk '{print $1}')
[ "$CHECKSUM" = "$BACKUP_CHECKSUM" ] && echo "PASS: checksums match" || echo "FAIL"
```

**Expected:** `PASS: checksums match`

---

## 21. Re-running the script on the same file (--keep, idempotent)

After a successful `--keep` run, the file has only 1 commit in history (the re-add). Run again:

```bash
$SCRIPT --keep . secret.txt   # confirm
```

**Expected:**
- Banner shows `Commits: 1 commit(s)`
- Runs successfully
- After: still 1 commit touching the file (the new re-add replaces the previous one)
- Content preserved

---

## 22. File not found in external repo (no silent failure)

Regression test: previously a bare filename miss in an external repo caused a
silent exit (blank output) due to `set -e` + `grep` pipeline failure.

```bash
dir="/tmp/test-extrepo-$$"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
cd /tmp
$SCRIPT "$dir" nonexistent.json
```

**Expected (stdout, exit 1):**
```
File not found on disk.
File not found in git history.
Nothing to do.
```

Must NOT produce blank output.

---

## 23. Binary file

```bash
dir="/tmp/test-binary-$$"
git init "$dir" && cd "$dir"
echo "normal" > readme.txt && git add . && git commit -m "init"
dd if=/dev/urandom bs=1024 count=10 of=secret.bin 2>/dev/null
git add . && git commit -m "add binary"
$SCRIPT . secret.bin   # confirm
```

**Expected:** Script completes successfully. Verify:

```bash
[ ! -f secret.bin ] && echo "PASS: deleted" || echo "FAIL"
git log --all --oneline -- secret.bin   # no output
```
