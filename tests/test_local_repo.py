"""Linked worktrees and submodules as `push` / `scan` sources (#103).

Real git throughout: the bug lived in how git resolves a `.git` *file*, which
a mocked subprocess cannot show. The "remote" is a local bare repo, so no
network or token is needed. Every source check records `rev-list --count
--all` and `status --porcelain` before and after; any change is a failure.
"""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from gh_safe_repo.commands import create
from gh_safe_repo.github_client import GitHubClient
from gh_safe_repo.git_transport import GitTransport
from gh_safe_repo.local_repo import external_git_dir, is_git_checkout
from gh_safe_repo.security_scanner import FindingCategory

from .test_security_scanner import make_scanner, mock_popen, write_file


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", "-c", "core.excludesFile=" + os.devnull,
         "-c", "protocol.file.allow=always", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def snapshot(path):
    return git(path, "rev-list", "--count", "--all"), git(path, "status", "--porcelain")


def heads(bare):
    return set(git(bare, "for-each-ref", "--format=%(refname:lstrip=2)", "refs/heads").split())


@pytest.fixture
def repos(tmp_path):
    """main checkout (main, other, tag v1), a linked worktree on `feat` with an
    uncommitted edit and a gitignored .env, and a superproject whose `sub`
    submodule is a clone of main."""
    main = tmp_path / "main"
    git(tmp_path, "init", "-q", "-b", "main", str(main))
    write_file(str(main), "a.txt", "hi\n")
    write_file(str(main), ".gitignore", ".env\n")
    git(main, "add", "-A")
    git(main, "commit", "-qm", "one")
    git(main, "tag", "v1")
    git(main, "branch", "other")

    linked = tmp_path / "linked"
    git(main, "worktree", "add", "-q", str(linked), "-b", "feat")
    write_file(str(linked), "a.txt", "hi\nwip\n")
    write_file(str(linked), ".env", "key = AKIAIOSFODNN7EXAMPLE\n")

    sup = tmp_path / "super"
    git(tmp_path, "init", "-q", "-b", "main", str(sup))
    git(sup, "submodule", "add", "-q", str(main), "sub")
    git(sup, "commit", "-qm", "add sub")
    sub = sup / "sub"

    return {"main": main, "linked": linked, "super": sup, "sub": sub, "tmp": tmp_path}


def make_client(remote):
    """A GitHubClient whose transport runs real git against a local bare repo."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="ghp_token\n", stderr="", returncode=0)
        client = GitHubClient()
    real = GitTransport(protocol="https", source_dir=str(remote),
                        ssh_command=None, has_credential_helper=False)
    transport = MagicMock()
    transport.run.side_effect = real.run
    transport.run_remote.side_effect = real.run
    transport.remote_url.return_value = str(remote)
    transport.persistent_url.return_value = str(remote)
    transport.redact.side_effect = lambda s: s
    client.transport = transport
    client.call_api = MagicMock()
    return client


def remote_repo(tmp_path, name="remote.git"):
    path = tmp_path / name
    git(tmp_path, "init", "-q", "--bare", str(path))
    return path


class TestIsGitCheckout:
    def test_main_worktree_and_submodule(self, repos):
        assert is_git_checkout(str(repos["main"]))
        assert is_git_checkout(str(repos["linked"]))
        assert is_git_checkout(str(repos["sub"]))

    def test_plain_directory(self, tmp_path):
        assert not is_git_checkout(str(tmp_path))

    def test_dot_git_file_without_gitdir_line(self, tmp_path):
        write_file(str(tmp_path), ".git", "not a gitfile\n")
        assert not is_git_checkout(str(tmp_path))

    def test_external_git_dir(self, repos):
        assert external_git_dir(str(repos["main"])) is None
        assert os.path.realpath(external_git_dir(str(repos["linked"]))) == \
            os.path.realpath(repos["main"] / ".git")
        assert os.path.realpath(external_git_dir(str(repos["sub"]))) == \
            os.path.realpath(repos["super"] / ".git" / "modules" / "sub")


class TestPushLocalFromGitFileCheckout:
    def test_linked_worktree_with_uncommitted_edits(self, repos):
        linked, main = repos["linked"], repos["main"]
        before = snapshot(linked)
        remote = remote_repo(repos["tmp"])

        wiring = make_client(remote).push_local(str(linked), "alice", "dest", "main")

        # Nothing committed into the real repo; the WIP edit is still WIP.
        assert snapshot(linked) == before
        assert "M a.txt" in before[1]
        # A worktree shares its main repo's refs: all of them are pushed.
        assert heads(remote) == {"main", "other", "feat"}
        assert git(remote, "tag") == "v1"
        assert git(remote, "rev-parse", "feat") == git(main, "rev-parse", "feat")
        assert wiring.default_branch == "main"
        assert set(wiring.pushed) == {"main", "other", "feat"}
        # Wiring lands in the shared repo config; upstream is the worktree's branch.
        assert wiring.branch == "feat" and not wiring.error
        assert git(linked, "rev-parse", "--abbrev-ref", "feat@{u}") == "origin/feat"

    def test_clean_linked_worktree(self, repos):
        linked = repos["linked"]
        git(linked, "checkout", "--", "a.txt")
        before = snapshot(linked)
        remote = remote_repo(repos["tmp"])

        wiring = make_client(remote).push_local(str(linked), "alice", "dest", "main")

        assert snapshot(linked) == before
        assert heads(remote) == {"main", "other", "feat"}
        assert wiring.pushed

    def test_submodule(self, repos):
        sub = repos["sub"]
        write_file(str(sub), "a.txt", "edited in submodule\n")
        before = snapshot(sub)
        super_before = snapshot(repos["super"])
        remote = remote_repo(repos["tmp"])

        make_client(remote).push_local(str(sub), "alice", "dest", "main")

        assert snapshot(sub) == before
        assert snapshot(repos["super"]) == super_before
        # The submodule's own local branches, not the superproject's or
        # its upstream's remote-tracking refs.
        assert heads(remote) == set(git(sub, "for-each-ref",
                                        "--format=%(refname:lstrip=2)", "refs/heads").split())

    def test_non_git_path_never_copies_dot_git(self, repos):
        """Defence in depth: a .git entry the detector rejects, or a nested
        one, must not reach the copy, where it would aim `git add`/`commit`
        at another repository."""
        main = repos["main"]
        src = repos["tmp"] / "plain"
        write_file(str(src), "f.txt", "x\n")
        write_file(str(src), ".git", "not a gitfile\n")
        write_file(str(src), "nested/.git", f"gitdir: {main / '.git'}\n")
        write_file(str(src), "nested/g.txt", "y\n")
        before = snapshot(main)
        remote = remote_repo(repos["tmp"])

        make_client(remote).push_local(str(src), "alice", "dest")

        assert snapshot(main) == before
        files = git(remote, "ls-tree", "-r", "--name-only", "HEAD").split()
        assert sorted(files) == ["f.txt", "nested/g.txt"]


class TestBranchDetection:
    def test_linked_worktree(self, repos):
        branches, default = create._local_branches(str(repos["linked"]))
        assert sorted(branches) == ["feat", "main", "other"]
        assert default == "main"
        assert create._local_branch(str(repos["linked"])) == "feat"

    def test_submodule(self, repos):
        branches, default = create._local_branches(str(repos["sub"]))
        assert branches == ["main"] and default == "main"


class TestScanGitFileCheckout:
    def test_worktree_skips_gitignored_files(self, repos):
        findings = make_scanner().scan(str(repos["linked"]))
        assert not [f for f in findings if f.category == FindingCategory.SECRET]

    def test_trufflehog_uses_git_mode(self, repos):
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "native"})
        scanner._discovery = {"method": "native", "version": "3.99.0"}
        with mock_popen() as mock_cls:
            scanner._try_trufflehog(str(repos["linked"]))
        assert mock_cls.call_args.args[0][1] == "git"

    @pytest.mark.parametrize("which,gitdir", [
        ("linked", ("main", ".git")),
        ("sub", ("super", ".git", "modules", "sub")),
    ])
    def test_container_mounts_external_git_dir(self, repos, which, gitdir):
        scanner = make_scanner({("pre_flight_scan", "trufflehog_mode"): "docker"})
        scanner._discovery = {"method": "container", "runtime": "podman",
                              "runtime_path": "/usr/bin/podman"}
        real_popen = subprocess.Popen
        with mock_popen() as mock_cls:
            # Let external_git_dir()'s own `git rev-parse` run for real.
            fake = mock_cls.return_value
            mock_cls.side_effect = lambda cmd, *a, **kw: (
                real_popen(cmd, *a, **kw) if cmd[0] == "git" else fake)
            scanner._try_trufflehog(str(repos[which]))
        cmd = mock_cls.call_args.args[0]
        mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--volume"]
        expected = os.path.realpath(os.path.join(repos[gitdir[0]], *gitdir[1:]))
        assert any(os.path.realpath(m.split(":")[0]) == expected for m in mounts)
        assert "git" in cmd
