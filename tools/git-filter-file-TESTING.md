# Manual Testing: git-filter-file.sh

All tests run against throwaway repos in `/tmp`. Never run against a real repo.

```bash
SCRIPT=/path/to/git-filter-file.sh   # set this to your actual path
```

**Prerequisite:** git-filter-repo must be resolvable — either `git filter-repo`
is installed, or `uvx` (from uv) is on `PATH`, in which case the script runs
`uvx git-filter-repo==2.47.0` (the pinned release; first use downloads it,
so needs network). Check with `git filter-repo --version` or
`uvx git-filter-repo==2.47.0 --version`. The uvx path also needs
git >= 2.22.0 (see 18d).

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
- Shows the git-filter-repo command that would run: `git filter-repo --force --invert-paths --path 'secret.txt'` (or `uvx git-filter-repo==2.47.0 ...` when only uv is available), followed by a note that it also expires reflogs and repacks
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
5. Prints rewriting message and `Using: git filter-repo` (or `uvx git-filter-repo==2.47.0`), then git-filter-repo's own progress output, then `✓ History rewritten`
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
- `git push --force-with-lease origin --all` succeeds and `git -C "$up" log --all --oneline -- secret.txt` is then empty
- The repo has no tags, so step 2 reads "Force-push ALL branches to 'origin'"
  and prints no tag push

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

### 18d. git too old for the pinned git-filter-repo (uvx path)

The uvx fallback runs the pinned release (`GIT_FILTER_REPO_PIN`, 2.47.0),
which needs git >= 2.22.0 (`GIT_FILTER_REPO_MIN_GIT`). Simulate an old git
with a wrapper that reports an old version but passes every other command
to the real git. This test needs `git filter-repo` NOT to be installed (the
check only applies to the uvx path).

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
REALGIT="$(command -v git)"
mkdir -p /tmp/oldgit-$$
cat > /tmp/oldgit-$$/git <<EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "--version" ]]; then echo "git version 2.20.1"; exit 0; fi
exec "$REALGIT" "\$@"
EOF
chmod +x /tmp/oldgit-$$/git
PATH="/tmp/oldgit-$$:$PATH" $SCRIPT --yes . secret.txt
PATH="/tmp/oldgit-$$:$PATH" $SCRIPT --dry-run . secret.txt
```

**Expected:**
- Real run: `Error: git 2.20.1 is too old for git-filter-repo 2.47.0 (needs git >= 2.22.0).`
  followed by the options (upgrade git, install a matching git-filter-repo,
  or set `GIT_FILTER_REPO_VERSION`), exit 1, before the fetch prompt.
  `git log --oneline` is unchanged.
- Dry run: `Warning: a real run would fail:` with the same text, the plan
  is still printed (with `uvx git-filter-repo==2.47.0`), exit 0.
- With `git filter-repo` installed (`Using: git filter-repo`), no version
  check is made by this script; the installed tool does its own.

### 18e. GIT_FILTER_REPO_VERSION override

```bash
dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
GIT_FILTER_REPO_VERSION=2.45.0 $SCRIPT --dry-run . secret.txt
GIT_FILTER_REPO_VERSION=2.45.0 $SCRIPT --yes . secret.txt
git log --all --oneline -- secret.txt   # no output

dir=$(make_repo | tail -1 | awk '{print $3}')
cd "$dir"
GIT_FILTER_REPO_VERSION=latest $SCRIPT --yes . secret.txt
GIT_FILTER_REPO_VERSION=latest $SCRIPT --dry-run . secret.txt
```

**Expected:**
- `2.45.0`: dry run prints `uvx git-filter-repo==2.45.0 --force ...`; the
  real run prints `Using: uvx git-filter-repo==2.45.0` and removes the file
  from history. The script's git-version check is skipped for an override
  (even with the old-git wrapper from 18d on `PATH`).
- `latest` (not a version number): real run prints
  `Error: GIT_FILTER_REPO_VERSION='latest' is not a version number ...`,
  exit 1, history untouched; dry run shows it as a warning, exit 0.

### 18f. Rewritten tags are pushed with an explicit lease per tag (#135)

`git push --force-with-lease --tags` (what the script used to print) is
rejected with `stale info` for every rewritten tag: with no expected value
the lease compares against remote-tracking refs, and git has none for tags.
The script now records the remote's tags (`git ls-remote --tags`) before
the rewrite and prints one push with `--force-with-lease=refs/tags/<tag>:<sha>`
for each tag that differs.

```bash
up="/tmp/test-tags-up-$$.git"; dir="/tmp/test-tags-$$"; other="/tmp/test-tags-other-$$"
git init --bare "$up"
git init "$dir" && cd "$dir"
echo normal > readme.txt && git add . && git commit -m init && git tag pre
echo secret > secret.txt && git add . && git commit -m "add secret" && git tag v1
echo more >> readme.txt && echo s2 >> secret.txt && git add . && git commit -m both
git tag -a v2 -m "release v2"
echo again >> readme.txt && git add . && git commit -m "readme only"
# A tag on a branchless commit that holds only secret.txt: the rewrite prunes
# that commit, and git-filter-repo deletes the tag.
tree=$(printf '100644 blob %s\tsecret.txt\n' "$(echo x | git hash-object -w --stdin)" | git mktree)
git tag gone "$(git commit-tree -m orphan "$tree")"
git remote add origin "$up" && git push -u origin HEAD && git push origin pre v1 v2 gone
git clone "$up" "$other"                       # someone else's clone
git -C "$other" tag remote-only pre && git -C "$other" push origin remote-only
git tag local-new v2^{}                        # local only, never pushed
$SCRIPT --dry-run --yes . secret.txt
$SCRIPT --yes . secret.txt
```

**Expected:**
- Before the banner: `Recording tags on 'origin' (touch your key again, 10s timeout)... done (5 tag(s)).`
- Banner: `Tags: 4 of 5 local tag(s) reach this file and will be rewritten` and `gone local-new v1 v2`
- Dry run, under `Would print`: `v1  lease <sha> (lightweight)`, `v2  lease <sha> (annotated)`
  (the tag object's SHA, not the peeled commit), `local-new  lease empty`, and a
  note that `remote-only` is left as it is. No `.git/filter-file-push-tags.sh` is written.
- Real run, step 2 prints (paths absolute, per #34):
  ```
  cd /tmp/test-tags-NNN && git push --force-with-lease origin --all
  cd /tmp/test-tags-NNN && git push \
      --force-with-lease=refs/tags/local-new: \
      --force-with-lease=refs/tags/v1:<old v1 sha> \
      --force-with-lease=refs/tags/v2:<old v2 tag object sha> \
      --force-with-lease=refs/tags/gone:<old gone sha> \
      origin \
      refs/tags/local-new \
      refs/tags/v1 \
      refs/tags/v2 \
      :refs/tags/gone
  ```
  then `Updates 2, creates 1, deletes 1 tag(s) on 'origin'`, `gone` listed as
  deleted, and a note that `remote-only` is left alone. `pre` is not pushed (unchanged).
- The tag push is also saved as `.git/filter-file-push-tags.sh`
- `git tag` shows no `remote-only` (the fetches use `--no-tags`) and no `gone`
- Copy and run the two printed push commands: both succeed, then
  ```bash
  rm -rf /tmp/test-tags-fresh-$$ && git clone "$up" /tmp/test-tags-fresh-$$
  git -C /tmp/test-tags-fresh-$$ log --all --oneline -- secret.txt   # empty
  git -C "$up" log --tags --oneline -- secret.txt                    # empty
  git -C "$up" cat-file -t refs/tags/v2                              # tag
  git -C "$up" rev-parse -q --verify refs/tags/gone                  # nothing
  ```
  and `git -C "$up" rev-parse refs/tags/<t>` equals `git rev-parse refs/tags/<t>`
  for `pre`, `v1`, `v2` and `local-new`.
- Repeat with `--keep`: the same, except the fresh clone's
  `git log --all -- secret.txt` shows only the re-add commit, and
  `git log --tags -- secret.txt` is empty.

**Safety case (tag moved on the remote after the snapshot).** Repeat the
setup, run `$SCRIPT --yes . secret.txt`, and before pushing move a tag from
the other clone:

```bash
git -C "$other" fetch origin --tags
git -C "$other" tag -f v1 HEAD && git -C "$other" push -f origin v1
```

(Move it to a commit other than the one the rewrite gives `v1`: here the
"add secret" commit becomes empty and is pruned, so the rewritten `v1` is
the `pre` commit, and moving the remote tag there would make the push a no-op.)

**Expected:** running the printed tag push prints
`! [rejected]        v1 -> v1 (stale info)` and exits non-zero; the other
tags are still updated; `git -C "$up" rev-parse refs/tags/v1` is the other
clone's value, not overwritten.

**More than 10 tags:** create 12 or more tags on commits after the file was
added and push them. Step 2 prints `bash /abs/path/.git/filter-file-push-tags.sh`
and a `cat` to review it instead of an inline command; running it pushes all
tags. Tag names with shell characters (e.g. `git tag "we'ird\$tag&x"`) are
quoted in both the printed command and the file.

**Fetch skipped:** answer `n` at the fetch prompt. The remote's tags cannot be
recorded, so step 2 says so and prints `git ls-remote --tags origin` and
`git push --force origin --tags`, to be run only after checking that nobody
moved a tag. No push file is written.

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
