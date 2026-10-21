"""
What kind of local source directory `push` / `scan` was handed.

One test for "is this a git checkout?" shared by validation, `push_local`,
the pre-flight scan and branch detection (#103). A main checkout has a `.git`
directory; a linked worktree (`git worktree add`) and a submodule have a
`.git` *file* (`gitdir: <path>`) pointing at a repository elsewhere. Testing
`os.path.isdir(.git)` treated the latter as plain directories, and the
non-git copy-and-commit path then committed into the real repository through
the copied `.git` file.

Filesystem-only on purpose: `git -C PATH rev-parse` walks up into a parent
repo, and callers already mock `subprocess` in tests.
"""

import os
import subprocess
from typing import Optional

GITFILE_PREFIX = "gitdir:"


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
