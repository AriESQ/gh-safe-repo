"""
Git transport abstraction.

Owns every piece of state a single git operation needs (protocol, source dir,
ssh command, credential helper presence) and runs git subprocesses with the
right environment. Centralizes the preflight check so that what the preflight
tests is exactly what the real push/clone will use.

See docs/2026-05-06_auth-architecture-review.md for the design rationale.
"""

import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Literal, Optional

from .errors import AuthError


PREFLIGHT_REPO = "gh-safe-repo-preflight/nonexistent"


@dataclass(frozen=True)
class GitTransport:
    """Immutable description of how this invocation talks to the git host.

    `source_dir` is the working directory whose git config (notably
    `core.sshCommand` set via `includeIf`) we treat as authoritative for the
    duration of this command. `ssh_command` is captured from that source_dir
    at discovery time and propagated as `GIT_SSH_COMMAND` into every git
    subprocess — including those that run in a temp dir, which is what fixes
    the P1 multi-account-YubiKey failure.
    """

    protocol: Literal["ssh", "https"]
    source_dir: str
    ssh_command: Optional[str] = None
    has_credential_helper: bool = False
    host: str = "github.com"
    debug: bool = field(default=False, compare=False)

    # ------------------------------------------------------------------ URLs

    def remote_url(self, owner: str, repo: str) -> str:
        if self.protocol == "ssh":
            return f"git@{self.host}:{owner}/{repo}.git"
        return f"https://{self.host}/{owner}/{repo}.git"

    def preflight_url(self) -> str:
        return self.remote_url(*PREFLIGHT_REPO.split("/"))

    # ------------------------------------------------------------------- env

    def env(self) -> dict:
        """Subprocess env: parent env + transport-specific overrides.

        - `GIT_SSH_COMMAND` propagates the user's per-directory ssh setup
          into temp-dir subprocesses (fixes P1).
        - `GIT_TERMINAL_PROMPT=0` makes HTTPS auth fail fast instead of
          blocking on a username prompt (fixes P4).
        - `GCM_INTERACTIVE=false` and `GCM_GUI_PROMPT=false` block Git
          Credential Manager from launching a browser in headless contexts
          (fixes P12 hang). `false` is the modern GCM value; older versions
          accept `never` for the same effect.
        """
        env = dict(os.environ)
        if self.ssh_command:
            env["GIT_SSH_COMMAND"] = self.ssh_command
        if self.protocol == "https":
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GCM_INTERACTIVE"] = "false"
            env["GCM_GUI_PROMPT"] = "false"
        return env

    # --------------------------------------------------------------- runtime

    def run(
        self,
        cmd: list,
        *,
        cwd: Optional[str] = None,
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
        timeout: Optional[int] = None,
        input: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """Run a git subprocess with this transport's env.

        Note: cwd is NOT defaulted to source_dir. The transport's job is to
        carry env (notably GIT_SSH_COMMAND) into wherever the caller chooses
        to run git — the push happens in a temp dir, but its env still needs
        the user's ssh command from source_dir.
        """
        if self.debug:
            print(f"[debug] {' '.join(cmd)}", file=sys.stderr)
        return subprocess.run(
            cmd,
            cwd=cwd,
            check=check,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            input=input,
            env=self.env(),
        )

    # ------------------------------------------------------------- preflight

    def preflight(self) -> None:
        """Probe `<host>` with the same transport the next operation will use.

        Runs `git ls-remote <preflight_url>` against a known-nonexistent
        repo. GitHub returns "Repository not found" when authentication
        succeeds but the repo doesn't exist — that's our success signal.
        Permission/credential errors surface here instead of after the API
        call has already created an empty repo on GitHub.

        Raises AuthError with a protocol-specific remediation hint on
        recognizable failures; otherwise re-wraps unexpected errors.
        """
        url = self.preflight_url()
        try:
            result = self.run(
                ["git", "ls-remote", url],
                check=False,
                timeout=15,
            )
        except FileNotFoundError as e:
            raise AuthError(f"`git` not found on PATH: {e}")
        except subprocess.TimeoutExpired:
            raise AuthError(
                f"git ls-remote {url} timed out. "
                "Network issue, or the credential helper is hanging on a prompt."
            )

        if result.returncode == 0:
            # ls-remote succeeded against a nonexistent repo? Shouldn't happen,
            # but it means auth worked.
            return

        stderr = (result.stderr or "").strip()
        lower = stderr.lower()

        if "repository not found" in lower or "not found" in lower:
            return  # auth ok, repo just doesn't exist (expected)

        if self.protocol == "ssh":
            if "permission denied" in lower or "publickey" in lower:
                raise AuthError(
                    "SSH authentication to "
                    f"git@{self.host} failed (Permission denied / publickey). "
                    "Add your SSH key to GitHub and load it into the agent, "
                    "or switch to HTTPS via `gh config set git_protocol https`.\n"
                    f"  git said: {stderr}"
                )
            if "host key verification failed" in lower:
                raise AuthError(
                    f"Host key verification failed for {self.host}. "
                    "Run `ssh -T git@github.com` once to accept the host key.\n"
                    f"  git said: {stderr}"
                )
        else:  # https
            if (
                "could not read username" in lower
                or "terminal prompts disabled" in lower
                or "authentication failed" in lower
                or "401" in lower
            ):
                hint = (
                    "Run `gh auth setup-git` to configure gh as your credential "
                    "helper, or install Git Credential Manager."
                )
                if not self.has_credential_helper:
                    hint = (
                        "No credential.helper is configured for git. "
                        "Run `gh auth setup-git` (recommended) or install "
                        "Git Credential Manager."
                    )
                raise AuthError(
                    f"HTTPS authentication to {self.host} failed.\n"
                    f"  {hint}\n"
                    f"  git said: {stderr}"
                )

        raise AuthError(
            f"Pre-flight git ls-remote {url} failed unexpectedly.\n"
            f"  git said: {stderr or '(no stderr)'}"
        )


# ---------------------------------------------------------------- discovery


def _gh_get_protocol(host: str = "github.com") -> str:
    """Read git_protocol from gh config, host-specific then global, else https.

    `gh auth setup-git` writes the host-specific value, which is what
    `gh auth status` reports — the global key may still be the default.
    """
    for cmd in (
        ["gh", "config", "get", "-h", host, "git_protocol"],
        ["gh", "config", "get", "git_protocol"],
    ):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            continue
        if result.returncode == 0:
            value = result.stdout.strip().lower()
            if value in ("ssh", "https"):
                return value
    return "https"


def _git_config_get(source_dir: str, key: str) -> Optional[str]:
    """Return `git -C source_dir config --get <key>` value, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", source_dir, "config", "--get", key],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _has_credential_helper(source_dir: str) -> bool:
    """True if any credential.helper is configured (any scope)."""
    try:
        result = subprocess.run(
            ["git", "-C", source_dir, "config", "--get-all", "credential.helper"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def discover_transport(
    source_dir: str,
    *,
    host: str = "github.com",
    debug: bool = False,
) -> GitTransport:
    """Construct a GitTransport by inspecting source_dir's git config + gh config.

    `source_dir` should be the directory whose git config we want to honor —
    typically the user's PATH for `--local`, or os.getcwd() for `--from`
    (no local clone yet, but the user's shell-CWD includeIf still applies).
    """
    abs_source = os.path.abspath(source_dir)
    protocol = _gh_get_protocol(host)
    ssh_command = (
        _git_config_get(abs_source, "core.sshCommand")
        if protocol == "ssh" else None
    )
    helper = _has_credential_helper(abs_source) if protocol == "https" else False
    return GitTransport(
        protocol=protocol,  # type: ignore[arg-type]
        source_dir=abs_source,
        ssh_command=ssh_command,
        has_credential_helper=helper,
        host=host,
        debug=debug,
    )
