"""
What kind of local source directory `push` / `scan` was handed.

One test for "is this a git checkout?" shared by validation, `push_local`,
the pre-flight scan and branch detection (#103). A main checkout has a `.git`
directory; a linked worktree (`git worktree add`) and a submodule have a
`.git` *file* (`gitdir: <path>`) pointing at a repository elsewhere. Testing
`os.path.isdir(.git)` treated the latter as plain directories, and the
non-git copy-and-commit path then committed into the real repository through
the copied `.git` file.

`is_git_checkout()` is filesystem-only on purpose: `git -C PATH rev-parse`
walks up into a parent repo, and callers already mock `subprocess` in tests.

`branch_state()` answers the next question `push` has to ask: is there
anything to push? A `git init`-ed directory with no commits is a git checkout
with zero refs (#134).
"""

import os
import subprocess
from typing import Optional

GITFILE_PREFIX = "gitdir:"

# branch_state() results.
HAS_BRANCHES = "has-branches"
NO_COMMITS = "no-commits"      # unborn HEAD: `git init`, nothing committed
NO_BRANCHES = "no-branches"    # commits exist (detached HEAD) but no branch
UNKNOWN = "unknown"            # not a checkout, or git could not say


def branch_state(path: str) -> str:
    """Whether the checkout at path has a local branch `push` could send.

    Returns HAS_BRANCHES, NO_COMMITS (`git init` with nothing committed:
    HEAD is unborn and refs/heads is empty), NO_BRANCHES (HEAD resolves to a
    commit but refs/heads is empty) or UNKNOWN. Local git only, no network,
    so it is safe under --dry-run.

    UNKNOWN means "could not tell" (not a checkout, git missing, git
    failed) and callers must not refuse on it: only a positive answer from
    git counts. A linked worktree shares its main repo's branches, so it
    reports HAS_BRANCHES whenever the main repo has any.
    """
    if not is_git_checkout(path):
        return UNKNOWN
    # `.git` exists, but if it is not a usable repository git would walk up
    # and answer for a parent repo. The ceiling stops that.
    env = dict(os.environ)
    env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(os.path.realpath(path))

    def git(*args):
        try:
            return subprocess.run(
                ["git", "-C", path, *args],
                capture_output=True, text=True, timeout=5, env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    heads = git("for-each-ref", "--count=1", "--format=%(refname)", "refs/heads")
    if heads is None or heads.returncode != 0:
        return UNKNOWN
    if (heads.stdout or "").strip():
        return HAS_BRANCHES
    head = git("rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if head is None:
        return UNKNOWN
    return NO_BRANCHES if head.returncode == 0 else NO_COMMITS


def is_git_checkout(path: str) -> bool:
    """True when path is the top of a git work tree: a `.git` directory,
    or a `.git` file (linked worktree, submodule) naming its gitdir."""
    dot_git = os.path.join(path, ".git")
    if os.path.isdir(dot_git):
        return True
    if not os.path.isfile(dot_git):
        return False
    try:
        with open(dot_git, encoding="utf-8", errors="replace") as f:
            return f.read(len(GITFILE_PREFIX)) == GITFILE_PREFIX
    except OSError:
        return False


def external_git_dir(path: str) -> Optional[str]:
    """For a `.git`-file checkout, the absolute repository directory that
    holds its objects and refs (the main repo's `.git` for a worktree, the
    superproject's `.git/modules/<name>` for a submodule); None otherwise.

    The common dir, not the per-worktree gitdir: a worktree's gitdir has no
    objects of its own, and lives inside the common dir anyway.
    """
    if not os.path.isfile(os.path.join(path, ".git")) or not is_git_checkout(path):
        return None
    try:
        r = subprocess.run(
            ["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.strip()
    # As git reports it, not realpath'd: the .git file names this path,
    # and the container must find it there.
    return out if r.returncode == 0 and out else None
