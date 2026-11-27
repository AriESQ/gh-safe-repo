"""Persona fixture factory for the auth persona matrix (#122).

A `Persona` is one declarative description of a user's auth environment:
what `gh` reports, what `git config` returns from a given directory, which
env vars are set, the `[git_transport] mode`, and how GitHub answers the git
connections. `FakeWorld(persona)` turns it into a single `subprocess.run`
replacement that dispatches on argv:

- `gh auth token`, `gh config get ... git_protocol`, `gh api ...` are faked.
- `git config --get core.sshCommand` / `--get-all credential.helper` are
  faked, honouring `ssh_command_scope` the way `includeIf "gitdir:..."` does.
- git commands that talk to GitHub (`ls-remote`, `push`, `clone <url>`) are
  faked. Their outcome is computed from the persona *and the env and URL the
  code actually passed*, so a change that stops propagating GIT_SSH_COMMAND or
  GIT_TERMINAL_PROMPT=0 fails here even if every unit test still passes.
- Everything else (local git on the test's temp repos) runs for real.

`drive()` runs `gh-safe-repo push|copy ... --yes` through `cli.main()` with
the world installed. The settings plugins, the Actions-usage warnings,
post-apply verification and the secret scanner are replaced: none of them
touch git transport, and each has its own test file.

See docs/2026-11-27_auth-persona-matrix-scope.md for the persona catalogue.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

REAL_RUN = subprocess.run

# Env vars that change git or gh behaviour. Cleared before every persona run
# so the developer's or CI runner's own values never leak in (#137).
CLEARED_ENV = (
    "GITHUB_TOKEN", "GH_TOKEN", "GH_HOST", "GH_CONFIG_DIR",
    "GIT_SSH_COMMAND", "GIT_SSH", "GIT_TERMINAL_PROMPT", "GIT_ASKPASS",
    "SSH_ASKPASS", "GCM_INTERACTIVE", "GCM_GUI_PROMPT",
)

# What real git / ssh / GitHub print. `{url}` is the URL as git was given it,
# token included, because older git versions echo it verbatim.
STDERR = {
    "not_found_ssh": (
        "ERROR: Repository not found.\n"
        "fatal: Could not read from remote repository."
    ),
    "not_found_https": (
        "remote: Repository not found.\n"
        "fatal: repository '{url}' not found"
    ),
    "publickey": (
        "git@github.com: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository."
    ),
    "sk_missed": (
        'sign_and_send_pubkey: signing failed for ED25519-SK "/k/id_ed25519_sk": '
        "invalid format\n"
        "git@github.com: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository."
    ),
    "host_key": (
        "Host key verification failed.\n"
        "fatal: Could not read from remote repository."
    ),
    "sso": (
        "ERROR: The '{owner}' organization has enabled or enforced SAML SSO. "
        "To access this repository, you must use the HTTPS remote with a "
        "personal access token or SSH with an SSH key and passphrase that has "
        "been authorized for this organization.\n"
        "fatal: Could not read from remote repository."
    ),
    "no_username": (
        "fatal: could not read Username for 'https://github.com': "
        "terminal prompts disabled"
    ),
    "auth_failed": (
        "remote: Invalid username or token. Password authentication is not "
        "supported for Git operations.\n"
        "fatal: Authentication failed for '{url}'"
    ),
    # `credential.helper = !gh auth git-credential` with gh no longer on PATH.
    "helper_missing": (
        "/bin/sh: gh: command not found\n"
        "fatal: could not read Username for 'https://github.com': "
        "terminal prompts disabled"
    ),
    "workflow_scope": (
        " ! [remote rejected] main -> main (refusing to allow an OAuth App to "
        "create or update workflow `.github/workflows/ci.yml` without "
        "`workflow` scope)\n"
        "error: failed to push some refs to '{url}'"
    ),
}


@dataclass(frozen=True)
class Persona:
    """One user's auth environment. Defaults are P2 (solo SSH developer)."""

    name: str
    login: str = "alice"

    # --- gh CLI
    gh_installed: bool = True
    gh_token: Optional[str] = "gho_alice_oauth"    # `gh auth token`; None = not logged in
    gh_protocol: Optional[str] = "ssh"              # hosts.github.com.git_protocol; None = unset
    installation_token: bool = False                # API token is a GitHub App ghs_ token

    # --- process environment (GITHUB_TOKEN, GH_TOKEN, ...)
    env: Dict[str, str] = field(default_factory=dict)

    # --- git config as `git -C <dir> config` sees it
    ssh_command: Optional[str] = None
    ssh_command_scope: Optional[str] = None         # includeIf gitdir prefix; None = global
    credential_helpers: Tuple[str, ...] = ()

    # --- gh-safe-repo config
    transport_mode: Optional[str] = None            # [git_transport] mode; None = unset

    # --- how GitHub and the local key answer
    ssh_key_works: bool = True                      # False: no usable key at all
    ssh_key_needs_command: bool = False             # key signs only via ssh_command (SK, broken agent)
    helper_state: str = "ok"                        # "ok" | "expired"
    token_scopes: Tuple[str, ...] = ("repo",)
    pushes_workflows: bool = False                  # the pushed code has .github/workflows/*
    sso_orgs: Tuple[str, ...] = ()                  # orgs enforcing SAML SSO for this key/helper
    repos: Dict[str, dict] = field(default_factory=dict)   # existing "owner/repo" -> repo JSON
    # Forced outcomes for the first git connections, in order ("auto" = compute).
    # Keys of STDERR, "ok", or "timeout".
    script: Tuple[str, ...] = ()

    @property
    def tokens(self) -> set:
        """Every token this persona holds; all are valid for `login`."""
        return {t for t in (self.gh_token, self.env.get("GITHUB_TOKEN"),
                            self.env.get("GH_TOKEN")) if t}


@dataclass
class Call:
    argv: List[str]
    cwd: Optional[str]
    env: Dict[str, str]


@dataclass
class NetOp:
    """One git connection to GitHub."""
    verb: str            # "ls-remote" | "push" | "clone"
    url: str
    cwd: Optional[str]   # the directory git ran in (-C or cwd)
    env: Dict[str, str]
    outcome: str


def _completed(argv, returncode=0, stdout="", stderr="", check=False):
    if check and returncode:
        raise subprocess.CalledProcessError(returncode, argv, stdout, stderr)
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _split_git(argv):
    """`git [-C dir] [-c k=v]... sub rest...` -> (dir, sub, rest)."""
    i, cdir = 1, None
    while i < len(argv) and argv[i] in ("-C", "-c"):
        if argv[i] == "-C":
            cdir = argv[i + 1]
        i += 2
    return cdir, argv[i], argv[i + 1:]


def _slug(url):
    m = re.search(r"github\.com[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group("slug") if m else url


class FakeWorld:
    """`subprocess.run` replacement for one persona. See module docstring."""

    def __init__(self, persona: Persona, plan: str = "free"):
        self.p = persona
        self.plan = plan
        self.calls: List[Call] = []
        self.net: List[NetOp] = []
        self.api: List[Tuple[str, str]] = []
        self._script = list(persona.script)

    # ------------------------------------------------------------ dispatch

    def __call__(self, cmd, *args, **kwargs):
        argv = [str(a) for a in cmd]
        env = dict(kwargs.get("env") or os.environ)
        self.calls.append(Call(argv, kwargs.get("cwd"), env))
        if argv[0] == "gh":
            return self._gh(argv, kwargs, env)
        if argv[0] == "git":
            faked = self._git(argv, kwargs, env)
            if faked is not None:
                return faked
        return REAL_RUN(cmd, *args, **kwargs)

    # ------------------------------------------------------------------ gh

    def _gh(self, argv, kwargs, env):
        check = kwargs.get("check", False)
        if not self.p.gh_installed:
            raise FileNotFoundError(2, "No such file or directory", "gh")
        if argv[1:3] == ["auth", "token"]:
            tok = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or self.p.gh_token
            if not tok:
                return _completed(argv, 1, "", "no oauth token found for github.com", check)
            return _completed(argv, 0, tok + "\n", "", check)
        if argv[1:3] == ["config", "get"]:
            if "-h" in argv:
                return _completed(argv, 0, (self.p.gh_protocol or "") + "\n", "", check)
            return _completed(argv, 0, "https\n", "", check)   # gh's global default
        if argv[1] == "api":
            return self._gh_api(argv, kwargs, env)
        raise AssertionError(f"persona {self.p.name}: unexpected gh call {argv}")

    def _gh_api(self, argv, kwargs, env):
        method = argv[argv.index("--method") + 1]
        endpoint = argv[argv.index("--method") + 2]
        self.api.append((method, endpoint))
        token = env.get("GH_TOKEN")

        def fail(status, message):
            return _completed(argv, 1, "", f"gh: {message} (HTTP {status})")

        def ok(body):
            return _completed(argv, 0, json.dumps(body), "")

        if token not in self.p.tokens:
            return fail(401, "Bad credentials")
        path = endpoint.split("?")[0]
        if method == "GET" and path == "/user":
            if self.p.installation_token:
                return fail(403, "Resource not accessible by integration")
            return ok({"login": self.p.login, "plan": {"name": self.plan}})
        m = re.match(r"^/repos/([^/]+)/([^/]+)(/.*)?$", path)
        if not m:
            raise AssertionError(f"persona {self.p.name}: unexpected API call {method} {endpoint}")
        slug, rest = f"{m.group(1)}/{m.group(2)}", m.group(3) or ""
        repo = {k.lower(): v for k, v in self.p.repos.items()}.get(slug.lower())
        if method == "PATCH" and not rest:
            return ok({})
        if method != "GET":
            raise AssertionError(f"persona {self.p.name}: unexpected API call {method} {endpoint}")
        if repo is None:
            return fail(404, "Not Found")
        if not rest:
            return ok({"default_branch": "main", "description": "", **repo})
        if rest == "/topics":
            return ok({"names": []})
        if rest.startswith("/git/matching-refs/heads"):
            return ok([{"ref": "refs/heads/main", "object": {"sha": "0" * 40}}])
        raise AssertionError(f"persona {self.p.name}: unexpected API call {method} {endpoint}")

    # ----------------------------------------------------------------- git

    def _git(self, argv, kwargs, env):
        """Fake config lookups and network ops; None means run it for real."""
        check = kwargs.get("check", False)
        cdir, sub, rest = _split_git(argv)
        where = cdir or kwargs.get("cwd") or os.getcwd()

        if sub == "config" and rest == ["--get", "core.sshCommand"]:
            if self.p.ssh_command and self._in_scope(where):
                return _completed(argv, 0, self.p.ssh_command + "\n", "", check)
            return _completed(argv, 1, "", "", check)
        if sub == "config" and rest == ["--get-all", "credential.helper"]:
            if self.p.credential_helpers:
                return _completed(argv, 0, "\n".join(self.p.credential_helpers) + "\n", "", check)
            return _completed(argv, 1, "", "", check)

        if sub == "ls-remote":
            return self._connect("ls-remote", rest[-1], where, env, argv, kwargs)
        if sub == "push":
            url = REAL_RUN(["git", "-C", where, "remote", "get-url", "--push", "origin"],
                           capture_output=True, text=True).stdout.strip()
            return self._connect("push", url, where, env, argv, kwargs)
        if sub == "clone":
            positional = [a for a in rest if not a.startswith("-")]
            if positional and "github.com" in positional[0]:
                url, dest = positional[0], positional[1]
                result = self._connect("clone", url, where, env, argv, kwargs)
                if result.returncode:
                    return result
                bare = "--mirror" in rest or "--bare" in rest
                REAL_RUN(["git", "init", "-q", *(["--bare"] if bare else []), dest],
                         check=True, capture_output=True)
                REAL_RUN(["git", "-C", dest, "remote", "add", "origin", url],
                         check=True, capture_output=True)
                return result
        if sub == "fetch":
            raise AssertionError(f"persona {self.p.name}: unexpected git fetch {argv}")
        return None

    def _in_scope(self, where):
        if self.p.ssh_command_scope is None:
            return True
        scope = os.path.realpath(self.p.ssh_command_scope)
        here = os.path.realpath(where)
        return here == scope or here.startswith(scope + os.sep)

    def _connect(self, verb, url, where, env, argv, kwargs):
        outcome = self._script.pop(0) if self._script else "auto"
        if outcome == "auto":
            outcome = self._decide(verb, url, env)
        self.net.append(NetOp(verb, url, where, env, outcome))
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)
        if outcome == "ok":
            return _completed(argv, 0, "", "", kwargs.get("check", False))
        if outcome == "not_found":
            outcome = "not_found_ssh" if url.startswith("git@") else "not_found_https"
        stderr = STDERR[outcome].format(url=url, owner=_slug(url).split("/")[0])
        return _completed(argv, 128, "", stderr, kwargs.get("check", False))

    def _decide(self, verb, url, env):
        p = self.p
        token_in_url = re.match(r"https://x-access-token:([^@]+)@", url)
        if url.startswith("git@"):
            if not p.ssh_key_works:
                return "publickey"
            if p.ssh_key_needs_command and env.get("GIT_SSH_COMMAND") != p.ssh_command:
                return "publickey"
        elif token_in_url:
            if token_in_url.group(1) not in p.tokens:
                return "auth_failed"
        elif p.credential_helpers:
            if p.helper_state == "expired":
                gcm = any("manager" in h for h in p.credential_helpers)
                if gcm and env.get("GCM_INTERACTIVE") != "false":
                    raise AssertionError(
                        f"persona {p.name}: GCM would open a browser (GCM_INTERACTIVE unset)")
                return "auth_failed"
        else:
            if env.get("GIT_TERMINAL_PROMPT") != "0":
                raise AssertionError(
                    f"persona {p.name}: git would block on a username prompt "
                    "(GIT_TERMINAL_PROMPT is not 0)")
            return "no_username"

        owner = _slug(url).split("/")[0]
        if owner in p.sso_orgs and not token_in_url:
            return "sso"
        if verb == "push":
            if token_in_url and p.pushes_workflows and "workflow" not in p.token_scopes:
                return "workflow_scope"
            return "ok"
        exists = _slug(url).lower() in {k.lower() for k in p.repos}
        return "ok" if exists else "not_found"

    # ------------------------------------------------------------- queries

    def ops(self, verb=None) -> List[NetOp]:
        return [op for op in self.net if verb is None or op.verb == verb]

    def ran(self, *prefix) -> List[Call]:
        """Calls whose argv starts with prefix (after `git -C dir`)."""
        out = []
        for c in self.calls:
            argv = c.argv
            if argv[:1] == ["git"] and len(argv) > 2 and argv[1] == "-C":
                argv = ["git"] + argv[3:]
            if argv[:len(prefix)] == list(prefix):
                out.append(c)
        return out


@dataclass
class Outcome:
    code: int
    out: str
    err: str
    world: FakeWorld
    created: bool          # RepositoryPlugin.apply ran (the repo would exist on GitHub)

    @property
    def text(self):
        return self.out + self.err


def _stub_scanner():
    scanner = MagicMock()
    scanner.scan.return_value = []
    scanner.engine_error.return_value = None
    scanner.engine_summary = "stub"
    scanner.scanner_description = "stub scanner"
    scanner.skipped_committed_dirs = []
    return scanner


def drive(persona, argv, *, tmp_path, monkeypatch, capsys, cwd=None, plan="free"):
    """Run `gh-safe-repo <argv> --yes` as `persona`; return an Outcome."""
    from gh_safe_repo.cli import main
    from gh_safe_repo.diff import Plan
    from gh_safe_repo.verify import Verification

    for var in CLEARED_ENV:
        monkeypatch.delenv(var, raising=False)
    for key, value in persona.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.chdir(cwd or tmp_path)

    ini = tmp_path / "persona.ini"
    lines = ["[pre_flight_scan]", "trufflehog_mode = off"]
    if persona.transport_mode is not None:
        lines += ["[git_transport]", f"mode = {persona.transport_mode}"]
    ini.write_text("\n".join(lines) + "\n")

    world = FakeWorld(persona, plan=plan)
    mod = "gh_safe_repo.commands.create"
    plugin_names = ("RepositoryPlugin", "ActionsPlugin", "BranchProtectionPlugin",
                    "SecurityPlugin", "TagProtectionPlugin", "ReleasesPlugin")
    patches = [patch("subprocess.run", world),
               patch(f"{mod}.SecurityScanner", return_value=_stub_scanner()),
               patch(f"{mod}.actions_usage.plan_warnings", return_value=[]),
               patch(f"{mod}.verify_create", return_value=Verification()),
               patch("sys.argv", ["gh-safe-repo", *argv, "--yes", "--config", str(ini)])]
    plugins = {name: patch(f"{mod}.{name}") for name in plugin_names}
    for p in patches:
        p.start()
    mocks = {name: p.start() for name, p in plugins.items()}
    try:
        for m in mocks.values():
            m.return_value.plan.return_value = Plan()
        mocks["RepositoryPlugin"].return_value.created_default_branch = "main"
        try:
            main()
            code = 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        for p in list(plugins.values()) + patches:
            p.stop()
    captured = capsys.readouterr()
    return Outcome(code, captured.out, captured.err, world,
                   created=mocks["RepositoryPlugin"].return_value.apply.called)


def make_local_repo(path, *, workflows=False):
    """A real git repo at path with one commit on `main`."""
    path.mkdir(parents=True, exist_ok=True)
    git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=example@example.com",
           "-c", "commit.gpgsign=false"]
    REAL_RUN(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "README.md").write_text("hello\n")
    if workflows:
        (path / ".github" / "workflows").mkdir(parents=True)
        (path / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    REAL_RUN(git + ["add", "-A"], check=True)
    REAL_RUN(git + ["commit", "-q", "-m", "init"], check=True)
    return path


def origin_url(path):
    """The user's repo `origin` after the run (what lands in .git/config)."""
    r = REAL_RUN(["git", "-C", str(path), "remote", "get-url", "origin"],
                 capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None
