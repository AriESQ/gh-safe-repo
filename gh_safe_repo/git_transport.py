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

from .errors import AuthError, ConfigError


PREFLIGHT_REPO = "gh-safe-repo-preflight/nonexistent"

TRANSPORT_MODES = ("auto", "user_creds", "token")


@dataclass(frozen=True)
class GitTransport:
    """Immutable description of how this invocation talks to the git host.

    `source_dir` is the working directory whose git config (notably
    `core.sshCommand` set via `includeIf`) we treat as authoritative for the
    duration of this command. `ssh_command` is captured from that source_dir
    at discovery time and propagated as `GIT_SSH_COMMAND` into every git
    subprocess — including those that run in a temp dir, which is what fixes
    the P1 multi-account-YubiKey failure.

    When `token` is set, the API token is injected into HTTPS URLs as
    `x-access-token:<token>@` — the CI/headless path (P4, P6) where no SSH
    key or credential helper exists. Anything containing such a URL (debug
    output, error messages, git stderr) must go through `redact()` before
    leaving the process.
    """

    protocol: Literal["ssh", "https"]
    source_dir: str
    ssh_command: Optional[str] = None
    has_credential_helper: bool = False
    host: str = "github.com"
    token: Optional[str] = field(default=None, repr=False)
    debug: bool = field(default=False, compare=False)

    # ------------------------------------------------------------------ URLs

    def remote_url(self, owner: str, repo: str) -> str:
        if self.token and self.protocol == "https":
            return f"https://x-access-token:{self.token}@{self.host}/{owner}/{repo}.git"
        return self.persistent_url(owner, repo)

    def persistent_url(self, owner: str, repo: str) -> str:
        """URL safe to write into long-lived git config — never embeds the token.

        Use this for any remote that outlives the current process (the user's
        own repo); use remote_url() for transport operations and temp dirs.
        """
        if self.protocol == "ssh":
            return f"git@{self.host}:{owner}/{repo}.git"
        return f"https://{self.host}/{owner}/{repo}.git"

    def preflight_url(self) -> str:
        return self.remote_url(*PREFLIGHT_REPO.split("/"))

    def redact(self, text: str) -> str:
        """Strip the token from text destined for terminal output or errors."""
        if self.token and text:
            return text.replace(self.token, "***")
        return text

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
            print(f"[debug] {self.redact(' '.join(cmd))}", file=sys.stderr)
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
        display_url = self.redact(url)
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
                f"git ls-remote {display_url} timed out. "
                "Network issue, or the credential helper is hanging on a prompt."
            )

        if result.returncode == 0:
            # ls-remote succeeded against a nonexistent repo? Shouldn't happen,
            # but it means auth worked.
            return

        stderr = self.redact((result.stderr or "").strip())
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
                or "invalid username or password" in lower
                or "401" in lower
                or "403" in lower
            ):
                if self.token:
                    raise AuthError(
                        f"HTTPS authentication to {self.host} failed using the "
                        "injected token.\n"
                        "  Check that the token is valid and has the `repo` scope — "
                        "plus the `workflow`\n"
                        "  scope if the push includes .github/workflows files.\n"
                        f"  git said: {stderr}"
                    )
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
            f"Pre-flight git ls-remote {display_url} failed unexpectedly.\n"
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


def git_protocol_preference(host: str = "github.com") -> str:
    """Public: the user's preferred git protocol ("ssh" or "https").

    Wraps the gh-config lookup so callers (e.g. the success banner) can order
    remote suggestions by preference without constructing a full GitTransport.
    """
    return _gh_get_protocol(host)


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
    mode: str = "auto",
    token: Optional[str] = None,
) -> GitTransport:
    """Construct a GitTransport by inspecting source_dir's git config + gh config.

    `source_dir` should be the directory whose git config we want to honor —
    typically the user's PATH for `--local`, or os.getcwd() for `--from`
    (no local clone yet, but the user's shell-CWD includeIf still applies).

    `mode` is the `[git_transport] mode` config value:
    - `auto` (default): use the user's own credentials when a path exists;
      fall back to token-in-URL only for HTTPS with no credential helper
      (the CI/headless case, P4/P6).
    - `user_creds`: never inject the token. Pin this to guarantee the
      workflow-scope-free push (P2's case) even without a helper configured.
    - `token`: always push over HTTPS with the token in the URL. For CI
      where the token was granted `workflow` scope intentionally.
    """
    mode = (mode or "auto").strip().lower()
    if mode not in TRANSPORT_MODES:
        raise ConfigError(
            f"[git_transport] mode: invalid value {mode!r} "
            f"(expected one of: {', '.join(TRANSPORT_MODES)})"
        )
    token = token or None
    abs_source = os.path.abspath(source_dir)

    if mode == "token":
        if not token:
            raise AuthError(
                "[git_transport] mode = token, but no token is available. "
                "Set GITHUB_TOKEN or run `gh auth login`."
            )
        if debug:
            print("[debug] git transport: https via token-in-URL (mode=token)",
                  file=sys.stderr)
        return GitTransport(
            protocol="https",
            source_dir=abs_source,
            host=host,
            token=token,
            debug=debug,
        )

    protocol = _gh_get_protocol(host)
    ssh_command = (
        _git_config_get(abs_source, "core.sshCommand")
        if protocol == "ssh" else None
    )
    helper = _has_credential_helper(abs_source) if protocol == "https" else False

    # `auto` falls back to token-in-URL only when the user has no credential
    # path of their own: HTTPS with no helper configured. SSH users are never
    # switched to token injection — that would re-introduce the workflow-scope
    # failure (P2) that token injection was originally removed for.
    use_token = (
        mode == "auto"
        and protocol == "https"
        and not helper
        and token is not None
    )
    if debug:
        cred = "token-in-URL" if use_token else "user credentials"
        print(f"[debug] git transport: {protocol} via {cred} (mode={mode})",
              file=sys.stderr)
    return GitTransport(
        protocol=protocol,  # type: ignore[arg-type]
        source_dir=abs_source,
        ssh_command=ssh_command,
        has_credential_helper=helper,
        host=host,
        token=token if use_token else None,
        debug=debug,
    )
