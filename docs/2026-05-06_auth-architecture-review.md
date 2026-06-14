# Authentication Architecture Review

**Date:** 2026-05-06
**Branch:** `design/auth-review`
**Status:** Design review — no implementation in this round.

## Why this review

A debugging session on 2026-05-05 turned up a class of bugs, not just a single
bug. The proximate symptom: `gh-safe-repo create alt-account/my-project --local
.` failed at `git push --all --tags` with "Permission denied (publickey)" even
though the user could push to that account from the same shell using ordinary
`git` commands. Root cause: the program clones the source into a temp
directory under `/var/folders/...` and pushes from there. The user's per-
directory `core.sshCommand` (set via `includeIf "gitdir:~/alt-account/"`)
no longer applies once the working directory leaves `~/alt-account/`. SSH falls
back to defaults, which on this user's machine routes through Apple's
launchd-managed `ssh-agent`, which cannot sign for FIDO/SK keys against
Homebrew's OpenSSH 10.3.

That fix is small (propagate `GIT_SSH_COMMAND` from the source dir into the
push subprocess). But the pattern around it isn't:

| SHA | Date | Patch |
|-----|------|-------|
| b822e9c | 2026-05-05 | Use user's git credentials instead of OAuth token (workflow scope) |
| d5f0be3 | 2026-05-05 | Read `git_protocol` from host-specific gh config |
| 7299df7 | 2026-05-05 | Tighten URL match in test |
| c6d2188 | 2026-05-03 | Use canonical owner casing for git push |
| 475449b | 2026-04-03 | Add timeout + suppress SSH noise |
| ad76fe3 | 2026-03-15 | Default `auto_init=false` to avoid push race |
| ac9dc8c | 2026-03-02 | Fix `--local` remote wiring |
| (uncommitted) | 2026-05-05 | Replace bare `ssh -T` preflight with `git ls-remote` (option C) |
| (proposed) | 2026-05-06 | Propagate `GIT_SSH_COMMAND` from source dir to temp-dir push |

Each commit was correct in isolation. None changed the underlying assumption
that authentication is an implicit, environment-derived property — something
git or `gh` will sort out at the moment of use, with no central model. Each
new user setup that doesn't match the implicit "happy path" produces a new
edge-case patch.

This document catalogs the variation space via user personas, traces each
persona through the current code paths, identifies the structural gaps, and
recommends a refactor. **No code changes in this round.** Recommendations are
presented for the user to decide which (if any) to implement next.

---

## 1. Auth Surfaces in the Current Code

The program touches authentication in three layers, all in
`gh_safe_repo/github_client.py`:

### 1a. GitHub API authentication
- **Source of truth:** `_authenticate()` at `github_client.py:97-120`.
  Tries `gh auth token` (subprocess) first, falls back to `GITHUB_TOKEN` env
  var, errors if neither.
- **Use site:** `call_api()` at `github_client.py:147-205`. Token is injected
  as `GH_TOKEN` into the env of the `gh api` subprocess. Used by every API
  call (`get_json`, `call_json`, every plugin).
- **Identity assumed:** the user owning the `gh` token. Owner case-sensitivity
  is normalized at `commands/create.py` (case-insensitive guard against the
  authenticated user's login).

### 1b. Git transport authentication (push, clone, ls-remote)
- **Protocol decision:** `_get_git_protocol()` at `github_client.py:28-54`.
  Reads `gh config get -h github.com git_protocol` then falls back to global
  `gh config get git_protocol`, defaults to `https`. Cached per `GitHubClient`
  instance.
- **URL construction:** `git_remote_url()` at `github_client.py:56-65`.
  Returns `git@github.com:owner/repo.git` for SSH or
  `https://github.com/owner/repo.git` for HTTPS. **No token injection.**
  Justification (per `docs/LEARNINGS.md` and CLAUDE.md): OAuth App tokens
  require the `workflow` scope to push `.github/workflows/*` files, even
  though the user's SSH key has no such restriction.
- **Use sites:**
  - `copy_repo()` at `github_client.py:237-288` — `git clone --mirror` +
    `git push --mirror` (used by `create --from owner/repo`).
  - `push_local()` at `github_client.py:290-392` — `git clone <local_path>`
    or `git init` + `git add` + `git commit`, then `git push origin --all`
    + `git push origin --tags` (used by `create --local PATH`).
  - `clone_for_scan()` at `github_client.py:394-414` — full `git clone` for
    pre-flight scanning of a remote repo (used by `create --from`).
- **Identity assumed:** whatever the user's git environment supplies.
  Decoupled from the `gh` token. May or may not be the same identity as
  the API token (see persona P8).

### 1c. Pre-flight credential verification
- **Implementation:** `verify_git_credentials()` at `github_client.py:67-111`.
  After the option-C patch (uncommitted on `design/auth-review`'s parent),
  runs `git ls-remote git@github.com:gh-safe-repo-preflight/nonexistent.git`
  from the user's CWD. Treats "Repository not found" as auth-success and
  "Permission denied" / "publickey" as failure.
- **Use site:** `commands/create.py:102` — only when `--local` or `--from`
  is set, only when not `--dry-run`.
- **HTTPS:** short-circuits (`return` at line 75-76). Never probed.

### 1d. What is not authentication code, but interacts with it
- `commands/create.py` builds the plan, prompts, calls each layer in turn.
- `commands/_common.py:234-244` prints success messages with raw SSH and
  HTTPS URLs (no auth involved, but worth noting since these are what the
  user copies if push fails and they want to retry manually).
- `security_scanner.py:219-239` calls `git ls-files` locally — no network
  auth, but these are the only other git calls in the codebase.

---

## 2. User Personas

Each persona is a realistic, observable setup. P1 is the user who commissioned
this review; P2-P8 are inferred from the variation surface that any auth
abstraction must accommodate.

### P1 — Multi-account YubiKey on macOS (the user)

**Setup**
- macOS, Homebrew OpenSSH 10.3, Apple launchd-managed `ssh-agent`.
- Two GitHub accounts: `primary-account` (active in `~/primary/`) and `alt-account`
  (active in `~/alt-account/`). Both authenticated via `gh auth login` and
  stored in macOS keychain.
- `~/.config/git/config` uses `includeIf "gitdir:~/primary/"` and
  `includeIf "gitdir:~/alt-account/"` to switch identity per
  directory.
- Each per-account file sets:
  ```
  [core]
      sshCommand = ssh -i <SK_keyfile> -o IdentitiesOnly=yes -o IdentityAgent=none
  ```
  This bypasses the agent entirely and routes signing directly to the
  YubiKey via libfido2. Necessary because Apple's `ssh-agent` cannot sign
  for FIDO/SK keys against newer OpenSSH clients.
- `gh config get git_protocol` → `ssh` for both accounts.

**What goes wrong today**
1. **Preflight (pre option-C):** ran bare `ssh -T git@github.com`, ignoring
   `core.sshCommand`. Default ssh hit the broken Apple agent and failed.
   Patched in option C by switching to `git ls-remote` from the user's CWD,
   which honors `includeIf`.
2. **Push:** clone+push happens from a temp dir under `/var/folders/...`,
   which doesn't match any `includeIf` glob. `core.sshCommand` is unset
   for those subprocesses. Default ssh hits the broken agent. Push fails.
   Not yet patched.
3. **Identity correctness:** even when push works, there is no check that
   the authenticated `gh` account matches the SSH identity that will sign
   the push. `gh-safe-repo create alt-account/foo` from a directory matched
   by `includeIf "~/primary/"` would create the repo under
   `alt-account` (via the active `gh` account) but try to push as the
   `primary` SSH key, which github would reject as a permissions issue.

**Why this persona matters for design**
Per-directory `core.sshCommand` is a first-class git feature documented at
git-config(5). Any abstraction that pretends git's transport is "stateless"
will keep breaking on this persona. The transport has state, and that state
is keyed by working directory.

### P2 — Solo developer (the implicit happy path)

**Setup**
- Linux or macOS, single GitHub account, single ed25519 key in `ssh-agent`,
  `gh auth login` once, `git_protocol=ssh`. No per-directory git config.

**What works**
Everything. This is the persona the codebase was originally designed for.

**Why it matters**
It is the regression-detection canary. Any refactor must keep P2 working
with no behavior change.

### P3 — HTTPS user with credential helper

**Setup**
- `gh auth login` followed by `gh auth setup-git` (which configures `git`
  to use `gh` itself as the credential helper).
- `git_protocol=https`. Push and clone go to `https://github.com/...`.
  Helper supplies the OAuth token transparently.

**What works**
Happy path, equivalent to P2.

**What goes wrong**
1. **Preflight skips HTTPS.** `verify_git_credentials()` returns immediately
   if `_get_git_protocol() != "ssh"`. There is no probe of HTTPS at all.
2. **Helper not configured:** if the user ran `gh auth login` but never ran
   `gh auth setup-git`, push fails at runtime with a generic credential-
   helper error. We do not detect this upfront.
3. **Helper has stale credentials:** the OAuth token in `gh`'s store has
   been rotated or revoked. Helper returns the old token, push fails with
   "Authentication failed". Same problem — no upfront detection.

**Why it matters**
HTTPS is the documented recommended path for cross-platform setups. It has
the same need for a preflight as SSH.

### P4 — CI / headless environment

**Setup**
- `GITHUB_TOKEN` env var set to a fine-grained PAT.
- No `gh` CLI installed (or installed but never run `gh auth login`).
- No SSH key.
- No git credential helper configured.

**What works**
- API calls: `_authenticate()` falls through to the env var. ✓
- `_get_git_protocol()`: `gh config get` fails (no gh, or no config),
  defaults to `https`. ✓

**What goes wrong**
- `git push https://github.com/owner/repo.git` from the temp dir asks for
  username/password. There is no helper, no terminal, no token in the URL.
  Push fails. The error surfaced to the user is "could not read Username".
- Before commit b822e9c (2026-05-05), the program injected
  `https://x-access-token:<token>@github.com/...` into URLs, which would
  have worked here. Removing that injection fixed P2's workflow-scope
  problem (an OAuth-App-specific limitation) but stranded P4 entirely.
- The README still says "or `GITHUB_TOKEN` set in your environment" as if
  this case is supported.

**Why it matters**
This persona is every CI runner, every Codespace, every Docker container.
Today it works for `fix` (API only) but fails for `create --local` and
`create --from` whenever a push is needed. The current design has no path
between "user's own credentials" and "no credentials" — only the former.

### P5 — Org user with SAML SSO

**Setup**
- Personal SSH key works for personal repos.
- Org repos require the SSH key to be SAML-SSO-authorized via the org's
  SSO provider (configured per-key in GitHub user settings).
- `gh auth login --with-token` may have been called with an org PAT, or
  the user may have run `gh auth login` and selected the SSO org.

**What works**
- API calls succeed (token is org-authorized).
- Pushes to personal repos succeed.

**What goes wrong**
- `verify_git_credentials()` probes a nonexistent repo under
  `gh-safe-repo-preflight/`. That probe doesn't exercise SSO at all because
  SSO authorization is per-org, per-key. Preflight passes, then the actual
  push to the org repo fails with a distinct error: `ERROR: The 'org' org
  has enabled or enforced SAML SSO. To access this repository, you must use
  the HTTPS remote with a personal access token or SSH with an authorized
  SSH key.`
- We have no detection of this case and no remediation hint.

**Why it matters**
Anyone using `gh-safe-repo create` against an org they belong to will hit
this. Currently the failure is wrapped in our generic "git push failed"
error message that obscures the actual cause.

### P6 — GitHub App / installation token

**Setup**
- A short-lived installation token (`ghs_*`) is in `GITHUB_TOKEN`. No user
  identity exists at all.
- Used by automation that creates repos on behalf of an installed app.

**What works**
- API calls work — installation tokens authenticate against the API.

**What goes wrong**
- The token is not associated with an SSH key (apps don't have keys).
- The token can be used as an HTTPS basic-auth password, but the URL
  injection path is gone (per b822e9c).
- `_authenticate()` is happy. `_get_git_protocol()` defaults to https. Push
  fails for the same reason as P4, with no remediation path.

**Why it matters**
Less common than P4 but architecturally identical. A clean design needs to
treat both as "token-only, must use token for transport."

### P7 — GitHub Enterprise Server / non-github.com host

**Setup**
- `github.com` is replaced by `git.example.com` or similar.
- `gh` is configured for the GHES host.

**What goes wrong**
- The codebase hardcodes `github.com` everywhere: URL construction
  (`github_client.py:64`), preflight probe URL
  (`github_client.py:81-ish`), API endpoints (`/repos/{owner}/{repo}`,
  which `gh api` rewrites for the configured host but the URLs we
  construct ourselves don't).

**Why it matters**
Out of scope for this review (flagged as future work). Calling it out so
that the recommended refactor doesn't accidentally make GHES support
harder.

### P8 — Identity vs auth split

**Setup**
- `gh auth login` was done as account A (so `gh auth token` returns A's
  token).
- The user's SSH key (`~/.ssh/id_ed25519`) is registered to account B.
- No `includeIf`, no per-directory override.

**What happens**
- `gh-safe-repo create A/foo --local .` creates the repo under A (correct).
- Push uses SSH key for B. GitHub accepts the push because A's repo is
  public or B has push permission; commits are authored as
  `git config user.name` (which may be either account, depending on what
  the user has globally configured).
- The user ends up with a repo under A containing commits authored as B,
  pushed via B's SSH connection. No error, just ongoing confusion about
  whose identity is whose.

**Why it matters**
Not a failure mode, but a footgun. Currently undetected and
undocumented. Worth flagging in the doc as an explicit non-feature so
users with multi-account setups know the program does not check identity
alignment.

### P9 — Multiple `gh auth` tokens for distinct accounts (multi-keyring)

**Setup**

A single workstation has two GitHub accounts authenticated via `gh auth login`,
both stored in the system keyring. Exactly one is marked active at any time.
The two accounts use *different* git transport protocols:

```
github.com
  ✓ Logged in to github.com account dev-primary (keyring)
  - Active account: true
  - Git operations protocol: ssh
  - Token: gho_****

  ✓ Logged in to github.com account dev-secondary (keyring)
  - Active account: false
  - Git operations protocol: https
  - Token: gho_****
```

`gh auth token` always returns the active account's token. The user switches
active accounts with `gh auth switch -u <account>`.

**What works**
- `gh-safe-repo create dev-primary/foo --local .` — API token matches the
  active account, owner check passes, SSH push uses the primary key. ✓
- `gh-safe-repo fix dev-primary/foo` — same account alignment, works. ✓

**What goes wrong**

1. **`create` against the inactive account is blocked at the wrong layer.**
   Running `gh-safe-repo create dev-secondary/foo` with `dev-primary` active
   fails the owner check (case-insensitive comparison of `dev-secondary` vs
   `dev-primary`). The current error is generic:
   `ConfigError: owner 'dev-secondary' does not match authenticated user 'dev-primary'`.
   This is technically correct but unhelpful — the user knows both accounts
   exist and has credentials for both. There is no hint that
   `gh auth switch -u dev-secondary` (or the `GH_TOKEN` override described
   below) would resolve it.

2. **`fix` against the inactive account silently uses the wrong token.**
   `fix` skips the owner check and instead verifies admin permissions on the
   target repo. If `dev-primary` has admin access to `dev-secondary`'s repo
   (e.g., they are collaborators), the command proceeds — API calls are made
   as `dev-primary`, not `dev-secondary`. The user gets no warning that the
   wrong identity is acting. If `dev-primary` does *not* have admin access,
   the failure is a generic 403 or 404 with no account-mismatch hint.

3. **`GH_TOKEN` override triggers a protocol mismatch.**
   The user's documented escape hatch is:
   ```
   GH_TOKEN=$(gh auth token --hostname github.com --user dev-secondary) \
     gh-safe-repo create dev-secondary/foo --local .
   ```
   This overrides the API token, so the owner check now passes. But
   `_get_git_protocol()` still reads from `gh config get -h github.com
   git_protocol`, which reflects the *active* account (`dev-primary`). It
   returns `ssh`. The tool constructs `git@github.com:dev-secondary/foo.git`
   and issues an SSH push — using `dev-primary`'s SSH key against a repo
   owned by `dev-secondary`. Push succeeds only if `dev-primary` has push
   permission; otherwise it fails with a key-not-authorized error that makes
   no mention of the protocol mismatch or account switch.
   The user's intention — use HTTPS + `dev-secondary`'s credential helper —
   is never honored. The tool has no awareness that `dev-secondary`'s
   preferred transport is HTTPS.

4. **No way to target a specific stored token without switching the global
   active account.** The only fully-correct escape today is:
   - `gh auth switch -u dev-secondary` before running the tool, then switch
     back — a manual, two-step, error-prone dance that also changes the
     global active account mid-session.
   Neither account-switch guidance nor the `GH_TOKEN` workaround appears
   in help output or error messages.

**Why this persona matters**

`gh auth status` with two accounts in keyring is a common developer setup on
macOS (where the keyring persists across shell sessions). Crucially, the
accounts having *different* protocols is realistic: a developer might have
set up their primary account with SSH keys and a secondary account using the
HTTPS credential helper (`gh auth setup-git`). The multi-account case can't
be detected by reading the environment alone — it requires querying
`gh auth status`, which the tool never does. Protocol and identity are
currently coupled to the *active* `gh` account but must be coupled to the
*target* account to work correctly in this persona. The right remedy requires
teaching `_authenticate()` and `_get_git_protocol()` to both accept a
`--user` hint so they query the same account, and at minimum surfacing a
clear error message and `gh auth switch` hint for the `create` owner-mismatch
case.

### P10 — Linux desktop developer (with FIDO/SK hardware key)

**Setup**
- Ubuntu or Fedora, standard desktop session (GNOME or KDE).
- Single GitHub account, `gh auth login` completed.
- **SSH key:** hardware FIDO/SK key (e.g., YubiKey) used for GitHub. On
  Linux, OpenSSH >= 8.2 communicates with FIDO/SK keys directly via
  `libfido2` without an Apple-launchd equivalent intercept. However,
  desktop SSH agents (GNOME Keyring, KWallet / `ksshaskpass`) have
  inconsistent SK support depending on version. If the user's agent does
  not handle SK key types, the correct workaround is the same as P1:
  ```
  [core]
      sshCommand = ssh -i ~/.ssh/id_ed25519_sk -o IdentitiesOnly=yes -o IdentityAgent=none
  ```
  set either globally or via `includeIf`. This is a real, common
  configuration for YubiKey users on Linux.
- **Token storage:** on macOS `gh` stores the token in the hardware-backed
  system keychain. On Linux, `gh` stores the token in plaintext at
  `~/.config/gh/hosts.yml` unless the user has explicitly configured a
  `libsecret`-backed credential store (e.g., `gh-credential-libsecret`).
  `gh auth status` shows `(keyring)` when `libsecret` is wired up, but
  is silent otherwise. The tool cannot distinguish the two cases.
- `gh config get git_protocol` → `ssh`.

**What works**
- When the GNOME/KDE agent supports SK keys (recent versions do) and the
  key is loaded, this behaves like P2. ✓
- When the user has set `core.sshCommand` globally (not via `includeIf`),
  the temp-dir push also works because `GIT_SSH_COMMAND` would be picked
  up from the global git config by every subprocess. ✓

**What goes wrong**

1. **`core.sshCommand` via `includeIf` breaks in temp dir — same as P1.**
   If `core.sshCommand` is set per-directory (e.g., for multi-account
   isolation), it silently drops when `push_local()` clones into
   `/tmp/...`. The SK key signing falls back to the desktop agent, which
   may not support the SK type, producing "Permission denied (publickey)"
   with no indication that the wrong binary handled the signing attempt.
   This is the same structural failure as P1, triggered by the same code
   path, just with a different root cause (agent incompatibility rather
   than Apple's launchd intercept).

2. **Headless / detached sessions.** If invoked via a cron job, a `tmux`
   session started before login, or `sudo`/`su`, `SSH_AUTH_SOCK` is absent
   or points to a dead socket. With SK keys this is especially visible:
   even `ssh-add -K ~/.ssh/id_ed25519_sk` won't help if the agent isn't
   running, and `IdentityAgent=none` with a direct `-i` is the only
   remaining path. The tool has no detection for this state.

3. **Token stored in plaintext.** Not a runtime failure, but a security
   property difference from macOS. On macOS the token is in the hardware-
   backed Secure Enclave chain; on Linux it lives in `~/.config/gh/hosts.yml`
   world-readable unless explicitly locked down. If `gh-safe-repo` ever
   surfaces the raw `gh auth token` output in an error message, Linux users
   are more exposed. Worth noting in any error-message design.

**Why this persona matters**
YubiKey / FIDO/SK SSH auth on Linux is a first-class use case, not a
corner case. The same `GIT_SSH_COMMAND` propagation fix that resolves P1
on macOS also resolves P10's `includeIf`-in-temp-dir failure. These two
personas should be tested together: any fix that is verified on macOS P1
should have a corresponding Linux P10 test, and vice versa. The underlying
structural gap (Gap 1, no `GitTransport` abstraction) is the same for both.

### P11 — Windows (scoped stub)

**Note:** Windows is flagged here for completeness but is explicitly out of
scope for the current review. The variation space on Windows is large enough
(Git for Windows, Git Credential Manager, native Win32 OpenSSH, WSL2, Cygwin,
PowerShell vs. cmd) that designing for it correctly warrants its own dedicated
session. The items below are failure hypotheses, not verified traces.

**Setup variations (not exhaustive)**
- **Git for Windows + Git Credential Manager (GCM):** the most common Windows
  developer setup. GCM stores credentials in the Windows Credential Manager
  (analogous to macOS Keychain; a software store backed by DPAPI, not hardware).
  SSH can use the OpenSSH bundled with Git for Windows, or the native Win32
  OpenSSH in `System32`.
- **WSL2:** the Linux subsystem can forward credentials to the Windows host via
  a bridge (`git-credential-manager.exe` called from within WSL). The Linux
  persona inside WSL is otherwise P10 but with a Windows-native binary in the
  credential path. Path handling (Windows `C:\` paths vs. WSL `/mnt/c/`) can
  break `includeIf "gitdir:..."` matching depending on whether the config uses
  Windows or POSIX paths.
- **PowerShell / native Win11 terminal:** closer to the Unix model than older
  Windows; `SSH_AUTH_SOCK` is surfaced via the Win32 OpenSSH agent (`OpenSSH
  Authentication Agent` Windows service). Some developers have this service
  disabled.

**Hypothesized failure modes**
1. **`core.sshCommand` value contains Windows paths.** If the user has set
   `sshCommand = "C:\\Program Files\\Git\\usr\\bin\\ssh.exe" -i ...`, passing
   this string as `GIT_SSH_COMMAND` from Python via `subprocess.run` may need
   quoting or escaping that differs from Unix. Untested.
2. **`includeIf` path matching on WSL2.** `gitdir:` conditions that work under
   WSL's `/mnt/c/Users/...` paths may not match when the same config is read
   by the native Win32 git binary. Not currently an issue because the tool
   doesn't run on Win32, but relevant if WSL2 support is desired.
3. **Temp dir path.** Python's `tempfile.mkdtemp()` on Windows returns a path
   under `%TEMP%` (e.g., `C:\Users\...\AppData\Local\Temp\`). GCM and the SSH
   agent should be process-wide rather than CWD-scoped, so P1's specific
   failure mode (CWD-keyed `includeIf` breaks in tmpdir) may not reproduce —
   but any tool that uses the tmpdir path as a git context (e.g., `-C <tmpdir>`)
   could still produce surprising behavior.

**Decision:** defer. If a Windows user reports a failure, use this stub as the
starting point for a dedicated Windows review. Until then, the `GitTransport`
abstraction (Recommendation A) should avoid hard-coding POSIX path assumptions
so that a future Windows trace doesn't require a complete rewrite.

### P12 — Git Credential Manager (GCM)

**Background**
[Git Credential Manager](https://github.com/git-ecosystem/git-credential-manager)
is a cross-platform credential helper maintained by the git-ecosystem project.
It is distinct from `gh auth setup-git` (which sets `gh` itself as the helper).
GCM is the default credential helper bundled with Git for Windows, and is also
installable on macOS and Linux. It handles HTTPS authentication, not SSH.
Configured via:
```
git config --global credential.helper manager
```
On macOS it stores credentials in Keychain; on Linux it targets `libsecret`,
`gpg`, or plaintext; on Windows, the Windows Credential Manager.

**Setup**
- `git_protocol=https` (GCM only handles HTTPS).
- `gh auth login` completed, but `gh auth setup-git` was NOT run — the
  user relies on GCM as their credential helper instead of `gh`.
- GCM is configured and has a valid GitHub OAuth token stored.
- This overlaps with P3 (HTTPS user) but is a distinct helper with
  different failure modes.

**What works**
- `gh-safe-repo fix` (API only): works, API token comes from `gh auth token`. ✓
- `gh-safe-repo create owner/repo --local .` on a configured desktop: GCM
  supplies the credential to git, push succeeds. ✓

**What goes wrong**

1. **GCM and `gh` both configured as credential helpers.**
   If the user has both `credential.helper=manager` (GCM) and
   `credential.helper=gh auth git-credential` in their git config (from a
   prior `gh auth setup-git` call), git invokes both in order and uses the
   first non-empty response. Which one "wins" depends on config layering
   and may not be obvious to the user or to the tool. The tool currently
   has no visibility into which helper is active.

2. **GCM's interactive OAuth flow fails headlessly.**
   If GCM's stored token has expired or been revoked, GCM attempts to
   re-authenticate by launching a browser window. In a headless environment
   (CI, SSH session, cron), this hangs or fails with `fatal: unable to
   access ... The requested URL returned error: 401`. The tool has no
   detection for this; the push fails with a confusing error after the API
   calls have already succeeded and the repo has already been created.
   The user must set `GCM_CREDENTIAL_STORE=plaintext` and re-authenticate
   in a headless-compatible mode before retrying.

3. **Preflight skips HTTPS entirely (Gap 3).**
   `verify_git_credentials()` short-circuits for HTTPS, so the GCM token
   expiry is not caught before push. This is the same gap as P3, but GCM's
   expired-token behavior (browser launch) is more disruptive than the
   generic "Authentication failed" that a bare helper would return.

4. **Multi-account with GCM.**
   GCM namespaces credentials by host (e.g., `github.com`) and optionally
   by username (with `GCM_GITHUB_AUTHMODES` set). With a basic
   `credential.helper=manager` config, GCM returns the most recently stored
   credential for `github.com` regardless of which `owner` was passed to
   `gh-safe-repo`. This may be the wrong account if the user has multiple
   GitHub accounts stored in GCM, and produces no warning — the same
   silent-wrong-account risk as P8/P9 but on the HTTPS side.

**Why this persona matters**
GCM is the default on Git for Windows, meaning any Windows user who hasn't
explicitly switched to SSH is almost certainly in this persona. On Linux
and macOS it is less prevalent but present, especially for developers who
prefer HTTPS across platforms. The HTTPS preflight gap (Gap 3) directly
affects this persona. Recommendation C (`git_transport.mode = token` for
CI) does not help here because GCM's headless failure is about token
refresh, not token injection. The right fix is an HTTPS preflight probe
that can distinguish "helper returned a credential" from "helper launched
a browser and hung."

### P13 — 1Password SSH agent

**Setup**
- 1Password desktop app installed and configured to act as an SSH agent.
- SSH keys are stored in the 1Password vault; the app exposes them via a
  Unix socket:
  - macOS: `~/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock`
  - Linux: `~/.1password/agent.sock`
- The user connects git to the 1Password agent via one of two mechanisms:
  - **`SSH_AUTH_SOCK` env var** (globally or in shell profile):
    `export SSH_AUTH_SOCK=~/.1password/agent.sock`
  - **`~/.ssh/config` `IdentityAgent` directive** (per-host or global):
    ```
    Host github.com
        IdentityAgent "~/.1password/agent.sock"
    ```
- `gh config get git_protocol` → `ssh`. No key files on disk; all signing
  is delegated to the 1Password app process.

**What works**
- When the app is running and unlocked, and `SSH_AUTH_SOCK` is set in the
  shell environment, the agent socket is inherited by child processes
  (including the `git push` subprocess in the temp dir). This works
  correctly from temp dirs because `SSH_AUTH_SOCK` is a process-level env
  var, not CWD-scoped. P1's `includeIf` problem does not apply here as long
  as the user hasn't additionally set `core.sshCommand`. ✓
- When configured via `~/.ssh/config` `IdentityAgent`, the ssh binary
  reads the config file directly, so temp-dir pushes also work — the config
  is global, not directory-scoped. ✓

**What goes wrong**

1. **App locked or not running → silent socket failure.**
   The 1Password agent socket path exists on disk but the app is not
   running, or the vault is locked (e.g., auto-lock after idle timeout).
   `git push` fails with "Permission denied (publickey)" — the same error
   as a missing key. The tool's error message gives no hint that the cause
   is a locked password manager. The preflight would catch this at startup
   if the app is already locked, but if the vault auto-locks *between*
   preflight and push (on a long-running plan + prompt cycle), the preflight
   passes and the push fails anyway.

2. **`core.sshCommand` interaction.**
   If the user has also set `core.sshCommand` (e.g., for multi-account
   isolation per P1), that command may point to a specific `-i <keyfile>`
   rather than delegating to an agent. `GIT_SSH_COMMAND` from `core.sshCommand`
   takes precedence over `SSH_AUTH_SOCK`, so 1Password is bypassed. The
   user must ensure their `core.sshCommand` is compatible with their
   1Password setup — the tool cannot verify this.

3. **Biometric confirmation prompt.**
   1Password can be configured to require a biometric confirmation (Touch
   ID on macOS, fingerprint on Linux) for each SSH signing operation.
   In an automated or scripted invocation (batch `fix` over many repos,
   or a CI-like context), this produces an interactive prompt that blocks
   indefinitely. There is no timeout on the signing request in standard
   SSH, and the tool does not set one. The process hangs rather than failing
   with a clear error.

4. **HTTPS credential helper (separate from SSH agent).**
   1Password also supports HTTPS git credentials via `op` CLI plugins
   (`op plugin init gh`). This is a separate, less common configuration
   that acts as a git credential helper. It has the same headless-failure
   mode as GCM (P12): the `op` process may require the app to be unlocked
   and present an interactive prompt in headless environments.

**Why this persona matters**
1Password as SSH agent is a mainstream choice for security-conscious
developers on macOS and Linux who want hardware-protected keys without
FIDO/SK hardware. The app-lock failure mode is a realistic TOCTOU hazard:
preflight passes, user reads the plan and confirms, vault auto-locks during
the confirmation prompt, push fails. No other persona produces this specific
race. The biometric-hang issue is also unique: unlike other failure modes
that surface a clear error, a blocked `ssh-keysign` just stalls the process
with no output. Any `run()` implementation in `GitTransport` should include
a timeout on git subprocesses so a stalled signing prompt doesn't hang the
tool indefinitely.

---

## 3. Persona-to-Code-Path Trace

For each currently-broken persona, here is the exact path through
`commands/create.py --local PATH`:

```
1. cli.py: parse args, build config
2. commands/create.py: build GitHubClient
   → github_client.py:97 _authenticate()
       P4, P6: GITHUB_TOKEN env var (✓)
       P1, P2, P3, P5: gh auth token (✓)
3. commands/create.py: validate owner case
   → github_client.py: get_owner() → /user → matches owner (✓)
4. commands/create.py:102: client.verify_git_credentials()
   → github_client.py:67 verify_git_credentials()
       Reads _get_git_protocol():
           P1, P2, P5: ssh
           P3: https (returns immediately — UNPROBED)
           P4, P6: https default (returns immediately — UNPROBED)
       For ssh:
           Runs `git ls-remote ...` from os.getcwd()
           P1: succeeds (CWD is in includeIf scope, sshCommand applied)
           P2: succeeds (default ssh + agent)
           P5: succeeds (probe is for personal preflight repo, not org repo)
   ALL PERSONAS PASS THIS CHECK — but P1, P3, P4, P5, P6 will all fail later.
5. commands/create.py: build plan, prompt, apply
   → github_client.py: API calls (✓ for all personas)
6. commands/create.py: push_local(local_path, owner, dest_repo)
   → github_client.py:290 push_local()
       a. tmpdir = /var/folders/.../work
       b. git clone <local_path> <tmpdir>      ← runs from cwd (P1: ✓)
       c. cd <tmpdir>; git remote set-url origin <dest_url>
       d. git push origin --all
          P1: FAILS — sshCommand not set in tmpdir, agent broken
          P2: succeeds
          P3: succeeds if helper configured
          P4: FAILS — no helper, no token in URL
          P5: FAILS — for org repos, "SSO not authorized for this key"
          P6: FAILS — no helper, no token in URL
       e. git push origin --tags
   → APIError("git push failed to <url>: <stderr>")
```

The preflight passes for everyone but stops only some failures. The push
exposes structural assumptions that the preflight doesn't share.

---

## 4. Structural Gaps

Five themes emerge from the persona traces, in order of how often they
recur.

### Gap 1: No `GitTransport` abstraction

Every method that runs git assembles command, env, cwd, and URL ad hoc.
There are four current call sites
(`copy_repo`, `push_local`, `clone_for_scan`, `verify_git_credentials`)
plus the local `git ls-files` calls in `security_scanner.py`. Any new
constraint — propagating `GIT_SSH_COMMAND`, injecting a token URL,
adding a hostname override — must be retrofitted into every site.
The proposed fix to P1 (propagate `GIT_SSH_COMMAND` from source dir)
is exactly this kind of retrofit.

### Gap 2: Preflight is not the same as the actual transport

`verify_git_credentials()` runs `git ls-remote` from the user's CWD.
The actual push runs `git push` from a temp dir under `/var/folders/...`.
Different env, different cwd, different effective `core.sshCommand`.
The preflight cannot prove the push will work. P1 demonstrates this.
P5 demonstrates it differently: the probe URL doesn't exercise the same
auth path as the real org URL.

The right shape is "preflight runs the exact same transport, with the
exact same env and cwd, that the next operation will use." That is hard
to do today because the transport is not a thing — it is a series of
inline subprocess calls.

### Gap 3: HTTPS path is unprobed

The current preflight returns immediately for HTTPS, citing helper
variation. But `git ls-remote https://github.com/preflight/nonexistent.git`
works fine: it triggers the helper, the helper produces a credential, git
attempts auth. We can read the result. The "trust-and-surface at push
time" choice means P3, P4, P6 all fail later than they need to, with
worse error messages.

### Gap 4: No model of "user setup persona" at startup

The program never explicitly answers the question "what kind of git
auth setup does this user have?" Each method asks a small piece of the
question on demand: `_get_git_protocol()` for one piece, `subprocess.run`
inside `verify_git_credentials` for another. The `core.sshCommand`
question is asked nowhere. The credential-helper-presence question is
asked nowhere. The "is GH_TOKEN the only credential" question is asked
nowhere.

A single startup probe — call it `discover_transport()` — could populate
a struct used by all subsequent calls. This would also let `create.py`
print a one-line "Using <SSH key from agent> for push" so the user can
catch wrong-account scenarios (P8) before commits go out.

### Gap 5: Token-as-credential is gone with no replacement

Commit b822e9c removed token URL injection because the OAuth App scope
on workflow files was a real bug. But the change was global. P4 and P6
need token URL injection or they can't push at all. There is no design
decision recorded for "what does the headless user do?" — the change
just stranded them.

The right shape is probably: respect the user's credentials by default
(today's behavior, fixes P2's workflow scope), but fall back to token
URL injection when no user credentials exist (would fix P4, P6). The
fallback is unsafe for P2 because of the workflow scope, but P2's user
credentials always work, so the fallback never triggers for them.
Detection is straightforward: try `git credential fill` against an
HTTPS URL; if it returns nothing usable, fall back to token URL.

---

## 5. Recommended Refactor

Three changes, each independently shippable. Each has trade-offs called
out so the user can pick a subset.

### Recommendation A: `GitTransport` class

**What:** A new module `gh_safe_repo/git_transport.py` containing a
`GitTransport` dataclass and `discover_transport(source_dir)` factory.

```python
@dataclass(frozen=True)
class GitTransport:
    protocol: Literal["ssh", "https"]
    source_dir: str               # for resolving includeIf / core.sshCommand
    ssh_command: str | None       # value of git -C source_dir config core.sshCommand
    credential_helper: bool       # has a credential.helper configured for github.com
    token: str | None             # the OAuth token, kept for fallback URL injection
    # … host stays github.com for now; future-proofed for GHES

    def remote_url(self, owner: str, repo: str) -> str: ...
    def env(self) -> dict[str, str]: ...    # propagates GIT_SSH_COMMAND
    def run(self, cmd: list[str], cwd: str | None = None) -> CompletedProcess: ...
    def preflight(self) -> None: ...        # raises AuthError on detected failure
```

**Wires in:** `GitHubClient.__init__` constructs one transport from the
user's source dir (PATH for `--local`, CWD for `--from`). All four
existing call sites use `transport.run(...)` instead of inline
`subprocess.run`.

**Trade-offs**
- Pro: every future auth concern goes in one place. Today's option-C
  patch becomes one line in `preflight()`. Today's `GIT_SSH_COMMAND`
  bug becomes one line in `env()`.
- Pro: testable in isolation. Persona tests (recommendation D below)
  construct a `GitTransport` directly without mocking subprocesses
  through three layers.
- Con: bigger diff. Existing test suite mocks `subprocess.run` at fine
  grain — every test that touches push/clone needs reworking.
- Con: introduces a class for what is currently four functions. If we
  don't add B and C as well, the abstraction is overkill.

### Recommendation B: Preflight uses the same transport as actual operations

**What:** Replace `verify_git_credentials()` with `GitTransport.preflight()`.
The preflight runs the exact same subprocess invocation that the push will
use, just against a known-nonexistent repo on the same host. Probes both
SSH and HTTPS uniformly.

For SSH:
```
git -C <source_dir> ls-remote git@github.com:gh-safe-repo-preflight/nonexistent.git
```
For HTTPS:
```
git -C <source_dir> -c credential.helper=… ls-remote https://github.com/gh-safe-repo-preflight/nonexistent.git
```

Detect "Repository not found" as success, "Permission denied" /
"could not read Username" / "Authentication failed" as failure with
distinct messages.

For P5 (SSO): the preflight should additionally probe the actual target
repo URL (not just the nonexistent preflight repo) when the target is in
an org. That triggers SSO check before push.

**Trade-offs**
- Pro: closes Gap 2. What passes preflight will succeed in actual use,
  modulo network flakes.
- Pro: HTTPS gets probed (Gap 3).
- Con: an extra `git ls-remote` per invocation (~1 round trip). Negligible.
- Con: the SSO probe against the real target adds another round trip
  when the org check is enabled. Could be made opt-in.

### Recommendation C: Documented escape hatch for token-as-git-credential

**What:** New config knob in `[git_transport]` section of `gh-safe-repo.ini`:

```ini
[git_transport]
mode = auto       # one of: auto, user_creds, token
```

Behavior:
- `auto` (default): use user's credentials if present, fall back to
  token-URL injection for HTTPS if no helper / no SSH key. Detection via
  `git credential fill` and presence of any SSH identity.
- `user_creds`: explicit, current behavior. Push fails fast if user
  credentials don't exist. Use this when you specifically want the
  workflow-scope-free push (P2's case).
- `token`: always inject `x-access-token:<token>@github.com` into HTTPS
  URLs. Only use in CI where you've granted the token `workflow` scope
  intentionally.

**Trade-offs**
- Pro: P4 and P6 work again without re-introducing P2's workflow scope
  bug.
- Pro: explicit config makes the trade-off visible to the user.
- Con: third config knob in this area (we have `git_protocol` already in
  `gh config`). Risk of confusion.
- Con: the `auto` fallback mechanism needs careful design — wrong
  detection could regress P2.

### Recommendation D: Persona-driven test matrix

**What:** A new `tests/test_auth_personas.py` that constructs each
persona's environment via fixtures and asserts end-to-end behavior of
`create --local` and `create --from`.

Today's tests in `tests/test_github_client.py` are fine-grained mocks
of `subprocess.run` — they cover one call at a time and don't catch
persona-level failures. The new file would be the regression net for
the refactor: every persona either works (P2) or fails with a specific
documented error (P5: "SSO not authorized" with remediation hint).

**Trade-offs**
- Pro: future patches that fix one persona without breaking another are
  caught by tests, not by user reports months later.
- Con: persona fixtures are non-trivial to build (mock `gh`, mock `git
  config`, mock filesystem layout for `includeIf`). High up-front cost.

---

## 6. Decision Matrix

| Path | What it gets you | What it costs | When to pick it |
|------|------------------|---------------|-----------------|
| Just patch P1 | Fixes the immediate user-reported bug | One method addition | Today, regardless of larger plan |
| A only | Cleaner code, easier future patches | Refactor existing tests | If you expect more auth changes |
| A + B | Above + preflight that actually predicts push outcome | + reworked preflight tests | If you keep getting "preflight passed but push failed" reports |
| A + B + C | Above + supports CI / GitHub App | + new config knob, + token-fallback detection | If headless / CI is a real use case |
| A + B + C + D | All of above + persona regression net | + heavy test fixtures | If you want the refactor to stick |
| Do nothing | Status quo, plus today's small patches | Continued whack-a-mole | If gh-safe-repo's user base is just P1 and P2 forever |

The user-reported P1 bug needs a fix regardless. The cheapest version of
that fix (propagate `GIT_SSH_COMMAND` from source dir to push subprocess,
~10 lines) is independent of recommendations A-D and could ship today
on its own branch.

---

## 7. Out of Scope

- **Implementation.** This document recommends only.
- **GHES / non-github.com host (P7).** Flagged here so that any A/B/C
  implementation deliberately keeps the hostname pluggable (a single
  `host: str = "github.com"` field on `GitTransport`), but no GHES
  support is being designed.
- **Identity-vs-auth alignment check (P8).** Worth a one-line warning in
  `create` ("Pushing as <ssh-key-comment> to repo owned by <gh-account>;
  these may differ"), but not a structural change. Could be added as a
  follow-up to recommendation A.
- **Schema changes to `gh-safe-repo.ini`** beyond recommendation C.

---

## 8. Verification of this Document

This is a documentation deliverable, no code or tests to run. Verify by:

1. Reading the document end-to-end on the `design/auth-review` branch.
2. Confirming any persona's failure mode with a targeted experiment if
   doubted. Examples:
   - **P1:** the user already confirmed by hitting the bug. Logs in
     conversation history of 2026-05-05.
   - **P4:** in a clean shell, `unset GH_*; GITHUB_TOKEN=<pat>
     gh-safe-repo create some/repo --local .` should fail with "could
     not read Username" if no credential helper.
   - **P5:** create a test repo in an SSO-enforcing org, attempt push
     with a non-SSO-authorized SSH key, observe error.
3. The branch stays open as `design/auth-review` until the user decides
   which (if any) of A / B / C / D to implement. Each decision becomes
   its own branch off `master`.

---

## Appendix A: Files referenced

- `gh_safe_repo/github_client.py` — all current auth code
- `gh_safe_repo/commands/create.py:102` — only caller of preflight
- `gh_safe_repo/commands/_common.py:234-244` — success message URLs
- `gh_safe_repo/security_scanner.py:219-239` — non-network git calls
- `tests/test_github_client.py:430-487` — current preflight tests
- `tests/test_cli.py:184-241` — current preflight integration tests
- `docs/LEARNINGS.md` — Phase-2 entry on git credentials decision
- `README.md:122-123` — current user-facing auth claims
- `CLAUDE.md` — Git transport section (current invariants)
