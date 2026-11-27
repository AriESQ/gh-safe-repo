# Auth Persona Matrix: Scope and Status

**Date:** 2026-11-27
**Issue:** #122 (Recommendation D of `docs/2026-05-06_auth-architecture-review.md`)
**Status:** Parked. The scaffold works and runs with the normal suite (no network, no token). This document says what is covered, what isn't, and what was found on the way.

## What was built

- `tests/personas.py`: the persona fixture factory.
  - `Persona` is a frozen dataclass, one per auth environment. It holds the gh token and `git_protocol`, the env vars, `core.sshCommand` plus the directory prefix its `includeIf` applies to, the credential helpers, `[git_transport] mode`, and how GitHub and the key answer.
  - `FakeWorld(persona)` is a single `subprocess.run` replacement that dispatches on argv:
    - `gh auth token`, `gh config get … git_protocol` and `gh api …` are faked.
    - `git config --get core.sshCommand` and `git config --get-all credential.helper` are faked, and honour the `includeIf` scope of whichever directory git runs in.
    - Git commands that reach GitHub (`ls-remote`, `push`, `clone <url>`) are faked. The outcome is **computed from the URL and env the code actually passed**, not scripted per test. An SK-key persona gets `Permission denied (publickey)` if `GIT_SSH_COMMAND` is missing from the push env. A no-helper HTTPS persona raises "would block on a username prompt" if `GIT_TERMINAL_PROMPT=0` is missing. A GCM persona with an expired token raises "would open a browser" if `GCM_INTERACTIVE=false` is missing. `script=` can force outcomes for the first N connections (missed touch, timeout, SAML, and so on).
    - Everything else runs for real, so `push` really bare-clones a temp repo and really rewires the user's `.git/config`.
  - `drive()` runs `gh-safe-repo push|copy … --yes --config <tmp ini>` through `cli.main()`.
- `tests/test_auth_personas.py`: 45 tests plus 4 strict `xfail`s that record findings.

**Mock boundary.** `GitHubClient`, `build_context` (owner check, `/user`), `check_repo_exists`, `discover_transport`, `GitTransport.preflight/run/run_remote`, `push_local`, `copy_repo`, `clone_for_scan`, `_abort_on_push_failure` and `print_success` all run unmodified. Four things are replaced, because none of them touch git transport and each has its own test file:

- the six settings plugins (`RepositoryPlugin.apply` being called is the "repo was created" signal)
- `actions_usage.plan_warnings`
- `verify_create`
- `SecurityScanner` (a stub returning no findings; `run_preflight_scan` still runs, so `clone_for_scan` is exercised)

**Sanity check.** With a throwaway pytest plugin that drops one var from `GitTransport.env()`, the persona file fails as follows (all pass unmutated):

| Var dropped | Failing tests |
|---|---:|
| `GIT_SSH_COMMAND` | 11 |
| `GIT_TERMINAL_PROMPT` | 4 |
| `GCM_INTERACTIVE` | 2 |

## Persona coverage

Legend: ✅ covered in `test_auth_personas.py` · ◐ partly · ❌ not covered (reason given) · 🔒 needs a live account.

### P1: Multi-account YubiKey, macOS, `includeIf` + `core.sshCommand`

| | |
|---|---|
| **Fake** | `ssh_command=SK_COMMAND`, `ssh_command_scope=<home>/alt-account`, `ssh_key_needs_command=True` (the Apple agent can't sign; only the sshCommand path works). The repo lives inside the scope. |
| **Commands** | `push <scope>/proj alice/dst`; `copy alice/src alice/dst` from inside and from outside the scope |
| **Expected** | `push` succeeds, and `GIT_SSH_COMMAND` reaches the preflight *and* the push even though the push runs from a temp dir outside the scope. `copy` inside the scope succeeds. `copy` outside the scope exits 1 at the scan clone, before creation, with `Likely cause:` naming the cwd and `core.sshCommand: not set`. Missed touch on push: retried once ("Touch it when it blinks"), then success. Two misses: exit 1, repo created but empty, recovery commands printed. Missed touch at preflight: exit 1, nothing created, no retry. Preflight timeout: "timed out after 45s" plus the security-key hint. |
| **Status** | ✅ `TestSkKeyIncludeIf` (shared with P10), `TestSkKeyTouch`, `TestInvariants[P1]` |
| **Existing pieces** | `test_git_transport.py`: `TestDiscoverTransport::test_captures_ssh_command_from_source_dir`, `TestEnv`, `TestPreflightSsh::test_propagates_ssh_command_into_preflight_subprocess`, `TestRunRemote`, `TestCredentialHints`. `test_cli.py`: `test_from_scan_clone_publickey_names_ssh_config_dir`, `test_failed_local_push_exits_1_with_recovery` (both with `copy_repo`/`push_local` mocked). |
| **Gap** | Item 3 of the review (identity alignment: an `includeIf` key for the wrong account) is P8/P9 territory and is deferred with #121. 🔒 The real stderr of a missed touch on current OpenSSH. |

### P2: Solo developer, SSH agent (regression canary)

| | |
|---|---|
| **Fake** | Defaults: `gh_protocol=ssh`, agent key works, no sshCommand, no helper |
| **Commands** | `push`, `copy`, and `push` with `GITHUB_TOKEN` also exported |
| **Expected** | Exit 0. Connections are `ls-remote` then `push` (`copy`: scan clone, `ls-remote`, mirror clone, mirror push), all `git@github.com:`. No `GIT_SSH_COMMAND` injected. `origin` becomes `git@github.com:alice/dst.git`. With `GITHUB_TOKEN` exported there is still no token in any git argv. |
| **Status** | ✅ `TestP2SoloSsh`, `TestInvariants[P2]` |
| **Existing pieces** | `TestDiscoverTransportMode::test_auto_never_injects_token_for_ssh`; `test_github_client.py::test_copy_repo_uses_ssh_url_when_configured`, and the `push_local` tests (per-call). |

### P3: HTTPS with `gh auth setup-git` helper

| | |
|---|---|
| **Fake** | `gh_protocol=https`, `credential_helpers=("!/usr/bin/gh auth git-credential",)` |
| **Commands** | `push` |
| **Expected** | Helper OK: exit 0, clean `https://github.com/alice/dst.git` URL, `GIT_TERMINAL_PROMPT=0` on every connection. Stale helper: preflight fails, exit 1, nothing created, "HTTPS authentication to github.com failed" without the no-helper hint. No helper + `auto`: falls back to the gh OAuth token in the URL; succeeds; `origin` clean; token not printed. No helper + workflows in the push: rejected after creation, exit 1 (F4). No helper + `user_creds`: preflight fails with "No credential.helper is configured", nothing created, no token used. Helper binary gone (`/bin/sh: gh: command not found`): **should** fail at preflight; today it passes the preflight (F6, strict xfail). |
| **Status** | ✅ `TestP3HttpsHelper`, `TestInvariants[P3]` |
| **Existing pieces** | `TestPreflightHttps`, `TestDiscoverTransportMode::test_auto_prefers_credential_helper_when_present`, `test_user_creds_mode_never_attaches_token` |
| **Gap** | The review's "helper not configured" sub-case now hits the token fallback rather than a username prompt. Asserted as current behaviour. |

### P4: CI / headless with `GITHUB_TOKEN`

| | |
|---|---|
| **Fake** | `gh` installed but never logged in (`gh_token=None`, protocol unset → gh default `https`), `GITHUB_TOKEN=ghp_…`, no helper, no key |
| **Commands** | `push`, `copy`; `mode = token`; `mode = user_creds`; `gh` not installed |
| **Expected** | `auto`: `https://x-access-token:<tok>@github.com/alice/dst.git` for push (and for the scan clone, mirror clone and push in `copy`). `GIT_TERMINAL_PROMPT=0`. `origin` written clean. Token never in stdout/stderr, including when git echoes the token URL in a failed mirror push. `mode = token`: same result with zero `gh config` / `git config` lookups. `mode = user_creds`: preflight fails with the no-helper hint, nothing created. No `gh` binary: **should** exit 1 cleanly; today it raises `FileNotFoundError` (F2, strict xfail). |
| **Status** | ✅ `TestP4Ci`, `TestInvariants[P4, P4-token]` |
| **Existing pieces** | `TestTokenInjection` (URL, redaction, persistent URL, scopes named), `TestDiscoverTransportMode` (`auto` fallback, `token` skips discovery, empty token) |
| **Gap** | Expired or under-scoped CI token: `/user` 401s first in the fake, so it is not reached separately. 🔒 Fine-grained PAT `Contents`/`Workflows` permission shapes. |

### P5: Org member with SAML SSO

| | |
|---|---|
| **Fake** | `sso_orgs=("acme",)`. Any SSH or helper connection to `acme/*` returns GitHub's SAML message. The token is SSO-authorized, so the API works. |
| **Commands** | `copy acme/src alice/dst`; `push PATH acme/dst`; `push PATH alice/dst` |
| **Expected** | `copy` from the SSO org exits 1 at the **scan clone**, before the preflight and before creation, with git's SAML text passed through. `push` to the org is refused by the owner check before any git op. A personal push is unaffected. |
| **Status** | ✅ `TestP5Sso` (current behaviour; F5 for the missing hint) |
| **Gap** | Rec B's "probe the real target repo for SSO" was never built, and with the owner check it is only reachable through `copy` sources, which the scan clone already probes. 🔒 Needs a GHEC org with SAML; a free test account cannot provide one. |

### P6: GitHub App installation token

| | |
|---|---|
| **Fake** | `GITHUB_TOKEN=ghs_…`, `installation_token=True` (`GET /user` → 403 "Resource not accessible by integration") |
| **Commands** | `push` |
| **Expected (current)** | Exit 1 "Could not determine GitHub user: GET /user returned 403", no git op, nothing created, token not printed |
| **Status** | ✅ `TestP6AppToken` (current behaviour; see F3) |
| **Gap** | The review treats P6 as "architecturally identical to P4" once Rec C landed. For this tool it isn't: the owner check needs `/user`, and creation is `POST /user/repos`, which an installation can't use. 🔒 Confirm the 403 with a real app installation. |

### P9: Two gh accounts, one host (#121): placeholder

Not written, on purpose. #121 is being fixed in parallel and changes the expected behaviour (SSH identity probe in the preflight, owner-mismatch hint). Once it lands, add:

- **Fake.** Two tokens with two logins. `GH_TOKEN` overrides the active account. `ssh -T git@github.com` returns `Hi <login>!` for whichever key `GIT_SSH_COMMAND` / the default selects. `FakeWorld._git` needs an `ssh` branch, or `_connect` grows a "which account does this key auth as" field.
- **Cases.** Key matches token (success). Key for the other account (fails before creation, with the `includeIf` / host-alias / HTTPS hint). HTTPS with the gh helper following `GH_TOKEN` (success). Owner mismatch (hint names `gh auth switch -u` and the `GH_TOKEN=$(gh auth token --user …)` override). Touch budget: the identity probe replaces `ls-remote` on SSH, so one touch, with the retry kept.
- **Note for #121.** `GitHubClient._authenticate` reads `GITHUB_TOKEN` first and never reads `GH_TOKEN` itself. The `GH_TOKEN=…` override works only because the fake and real `gh auth token` echo it back. If both are set, `GITHUB_TOKEN` wins (F7).

### P10: Linux desktop with FIDO/SK key

| | |
|---|---|
| **Fake** | As P1, either with a **global** `core.sshCommand` (`scope=None`) or an `includeIf` scope. Headless variant: `ssh_key_works=False`, no sshCommand. |
| **Commands** | `push`, `copy` from an arbitrary cwd |
| **Expected** | Global sshCommand: works from any directory, `GIT_SSH_COMMAND` on every connection. `includeIf`: identical to P1 (parametrized together, as the review asks). Headless: preflight fails, exit 1, nothing created, with the publickey remediation plus `core.sshCommand: not set`. |
| **Status** | ✅ `TestSkKeyIncludeIf[P10-includeIf]`, `test_p10_global_ssh_command_works_from_any_directory`, `test_p10_headless_no_agent_fails_preflight_with_remediation`, `TestInvariants[P10-global]` |
| **Gap** | The plaintext `hosts.yml` token is a property of gh, not of this tool. Covered indirectly by the no-token-in-output invariant. |

### P12: Git Credential Manager

| | |
|---|---|
| **Fake** | `gh_protocol=https`, `credential_helpers=("manager",)`; `helper_state="expired"` for the stale case |
| **Commands** | `push` |
| **Expected** | OK: exit 0, `GCM_INTERACTIVE=false` and `GCM_GUI_PROMPT=false` on every connection, no token in the URL. Expired: preflight fails without opening a browser (the fake raises if the env would allow one), exit 1, nothing created. |
| **Status** | ✅ `TestP12Gcm`, `TestInvariants[P12]` |
| **Existing pieces** | `TestEnv::test_https_disables_terminal_and_gcm_prompts`, `TestPreflightHttps::test_disables_browser_prompts_in_subprocess_env` |
| **Gap** | Both gh and GCM as helpers (which one wins is git's ordering, not ours). GCM multi-account is P9-adjacent. Wrong remediation text (F8). 🔒 Real GCM stderr with `GCM_INTERACTIVE=false`. |

### Out of scope (per #122)

P7 (GHES), P8 (identity vs auth split, folded into #121), P11 (Windows), P13 (1Password agent).

### Matrix-wide invariants (`TestInvariants`, parametrized over every working persona)

1. **Preflight matches the push (Gap 2).** Exactly one `ls-remote`, before the push. It uses the same scheme and credential path (SSH, plain HTTPS, or token URL) and the same `GIT_SSH_COMMAND` / `GIT_TERMINAL_PROMPT` / `GCM_*` values.
2. **No token leaks.** No persona token appears in stdout or stderr, and the user's `origin` never contains `x-access-token`.

## Findings: documented behaviour vs. code

Source was not changed. Each finding that has an executable form is a strict `xfail` in `test_auth_personas.py`, so it flips to a failure once fixed.

**F1. `copy` pushes to the source owner, not the destination (high).**
- `create.py:649` calls `client.copy_repo(from_owner, from_repo, repo_name, …)`. `GitHubClient.copy_repo(owner, source_repo, dest_repo)` (`github_client.py:258`) builds *both* URLs and the follow-up `PATCH default_branch` from `owner`. The docstring even says "Both repos must belong to owner", but `copy`'s help says the source can be "any public repo, or one you can read".
- **Other owner** (`copy bob/src alice/dst`): `alice/dst` is created and configured, then the mirror is pushed to `bob/dst` and `bob/dst`'s default branch is PATCHed.
  - If `bob/dst` doesn't exist, the push fails, the user gets the "repo created but empty" recovery text, and that text points at the *correct* destination.
  - If `bob/dst` exists and the user can write to it (an org they belong to, a collaborator repo), `git push --mirror` rewrites its refs and deletes any not in the source.
- **Casing only** (`copy alicedev/src AliceDev/dst`): the push URL uses the typed, non-canonical casing. That is the redirected-push problem c6d2188 fixed for `push`, which GitHub rejects for workflow files.
- **Why existing tests miss it.** `test_github_client.py` uses one owner throughout, and `test_cli.py` mocks `copy_repo`.
- **Test.** `test_copy_pushes_to_destination_owner[other-owner|owner-casing]` (xfail).
- **Suggested fix.** Pass both owners (`copy_repo(src_owner, src_repo, dest_owner, dest_repo)`), with `ctx.owner` as the destination.

**F2. P4 "no gh CLI installed" is not supported, and the failure is a traceback.**
- The review's P4 setup allows `gh` to be absent. With `GITHUB_TOKEN` set, `_authenticate` never runs gh, but every API call is `gh api`, so `get_owner()` raises an uncaught `FileNotFoundError`.
- README "Requirements" ("gh CLI logged in, or GITHUB_TOKEN set") can be read as "gh optional".
- **Test.** `test_no_gh_installed_fails_cleanly` (xfail).
- **Fix shape.** Catch it in `call_api` (or `build_context`) and say that `gh` is required even with `GITHUB_TOKEN`. Clarify the README line.

**F3. P6 (installation token) cannot pass the owner check.**
- The review and `docs/LEARNINGS.md` ("Rec C … restores a push path for CI/headless environments (P4, P6)") present P6 as fixed by token injection.
- In practice:
  - `GET /user` is not available to installation tokens (403), so `build_context` exits 1.
  - Creation is `POST /user/repos`, which an app installation can't call either; an app needs `POST /orgs/{org}/repos`.
- P6 is unsupported for `new`/`push`/`copy`. It fails safely, and the test asserts that, but the docs should say so.
- Not yet confirmed live (🔒).

**F4. The `auto` token fallback reintroduces the workflow-scope failure for HTTPS users without a helper.**
- LEARNINGS argues `auto` is safe for P2 because SSH short-circuits it. But an HTTPS user who ran `gh auth login` without `setup-git` (P3 variant) gets the gh **OAuth** token injected, which normally lacks `workflow`.
- The preflight (`ls-remote`) cannot detect this. A push containing `.github/workflows/*` is rejected *after* the repo is created, and `_auth_hint` offers nothing for "refusing to allow an OAuth App … without `workflow` scope".
- The test asserts today's exit 1 with the repo created.
- Options:
  - add a hint for that stderr (pointing at `gh auth setup-git` or `mode = user_creds`)
  - or, when injecting the token and the push carries workflow files, check the token's scopes first (`X-OAuth-Scopes`).

**F5. P5 trace in the review is stale; no SSO hint.**
- The review says "anyone using `gh-safe-repo create` against an org they belong to will hit this" at push time. But `new`/`push`/`copy` refuse non-self owners, so an SSO error can only come from a `copy` **source**.
- That source is cloned for the scan before anything is created, so the outcome is already safe.
- What remains is UX: the SAML text is passed through with no `Likely cause:` line. Adding `"saml sso"` to `_auth_hint` would be cheap. Rec B's target-repo probe is unnecessary while the owner check stands.

**F6. Preflight's success test is a bare `"not found"` substring.**
- `git_transport.py:229` (`"repository not found" in lower or "not found" in lower`) treats `/bin/sh: gh: command not found` as success. That is what git prints when a `!gh auth git-credential` helper's binary is no longer on PATH (a moved Homebrew prefix, a cron PATH).
- The preflight then passes and the push fails after creation.
- **Test.** `test_missing_helper_binary_fails_preflight` (xfail).
- **Fix shape.** Match `repository not found` / `repository '…' not found` only.

**F7. Review §1a has the token order backwards; GH_TOKEN is indirect.**
- The review says `gh auth token` is tried first and `GITHUB_TOKEN` second. The code (`github_client.py:69`) and README use `GITHUB_TOKEN` first.
- `GH_TOKEN` is honoured only because `gh auth token` echoes it, and `GITHUB_TOKEN` beats it when both are set. That matters for the #121 workaround (`GH_TOKEN=$(gh auth token --user b)`).
- Doc-only. Raise it with whoever picks up #121.

**F8. Stale GCM credential gets advice to install GCM.**
- With any helper configured, an HTTPS auth failure says "Run `gh auth setup-git` … or install Git Credential Manager." For P12, whose GCM credential expired, that is the wrong advice.
- The review's remediation (re-authenticate GCM, `GCM_CREDENTIAL_STORE`) isn't surfaced. Minor; not xfailed, because the right text is a design call.

**F9. The preflight doesn't use `run_remote()`.**
- A missed touch at the preflight exits 1 (safely, before creation), while the same miss during the push is retried once. CLAUDE.md says "remote git ops go through `transport.run_remote()`".
- Strictly, the preflight is the exception: it is a probe, and a miss there costs a full re-run including the scan.
- Worth a deliberate decision, especially since #121 plans to replace this probe with an identity probe on SSH. Asserted as current behaviour (`test_missed_touch_at_preflight_creates_nothing`).

## Mockable vs. needs a real account

**Fully mockable, and done above:** everything the tool *decides*. That covers protocol and credential selection, env propagation into temp dirs, includeIf-scoped discovery, preflight ordering and error mapping, token injection and redaction, retry-once on a missed touch, recovery text, and the exit codes.

**Not mockable.** Whether the strings the fake returns are what real git, ssh, GCM and GitHub print *today*. Every fake outcome is a hand-copied stderr shape in `STDERR` in `tests/personas.py`. If GitHub rewords "Repository not found" or the SAML message, these tests keep passing while the real tool breaks. That is the job of a live tier.

### What a dedicated test account would unlock

Proposed name: a throwaway user such as `gsr-persona-b`, alongside the user's own account, as a Tier 3 suite (`tests/e2e/test_live_personas.py`, gated by `E2E_PERSONAS=1`). Per the no-`delete_repo` policy (#166), it would **reuse fixed scratch repos** rather than create and delete them: a public `gsr-persona-b/persona-src` with one commit and one `.github/workflows/ci.yml`, plus a small pool of destination names cleaned up in the web UI.

| Use | Needs | Unlocks |
|---|---|---|
| **P9 / #121 identity mismatch**, the main reason for the account | An SSH key registered **only** on the test account; its own `GH_CONFIG_DIR` (`~/.config/gh/accounts/gsr-persona-b`) with `gh auth login` | Real `ssh -T` "Hi <login>!" output per key. Proves the pre-fix failure (preflight passes, push fails as the wrong user) and the post-fix early failure. |
| **P1/P10 temp-dir push, live** | An `*-SK` key on the test account; a test-only git config via `GIT_CONFIG_GLOBAL` with an `includeIf "gitdir:<tmp>/"` sshCommand | The real temp-dir push honouring `GIT_SSH_COMMAND`. Real missed-touch stderr, to refresh `STDERR["sk_missed"]`. |
| **P3 / P12 HTTPS helpers** | HTTPS login in its own `GH_CONFIG_DIR` + `gh auth setup-git` in a temp `HOME`; optionally GCM with a deliberately revoked credential | Real "Authentication failed" / prompt-disabled stderr; GCM's behaviour under `GCM_INTERACTIVE=false` |
| **P4 / F4 token scopes** | A fine-grained PAT with Contents: write and *no* Workflows permission; a classic PAT with and without `workflow` | The real workflow-rejection text, and whether the preflight can see scopes. Decides F4's fix. |
| **F1 cross-owner copy** | The test account's public `persona-src`, copied into the main account | Confirms F1 safely: copy from `gsr-persona-b/persona-src` to `<main>/scratch` and observe the push target. Never run it with a writable same-named repo under the source owner. |
| **P6 / F3** | A GitHub App (free) installed on the test account; mint an installation token | Confirms `GET /user` 403 and the `POST /user/repos` refusal |
| **P5 SAML** | **Not unlockable** with a free account: needs a GitHub Enterprise Cloud org with SAML SSO | Stays mock-only |

**Setup checklist for the user:**

1. Create the account and enable 2FA.
2. `GH_CONFIG_DIR=~/.config/gh/accounts/gsr-persona-b gh auth login`.
3. Register one SSH key used nowhere else, and optionally one `-sk` key.
4. Create `persona-src` (public, one commit, one workflow file).
5. Optionally register a GitHub App and install it on the account.

Nothing in the current suite needs the account. CI stays token-free.

## Next steps when this is picked up

1. File or fix F1 (it can damage another repo) and F6 (cheap); each strict xfail flips when fixed.
2. Add P9 personas once #121 merges (see the P9 placeholder above).
3. Decide F4 and F8 wording; add `Likely cause:` for SAML (F5) and the workflow-scope rejection.
4. Correct the review doc (§1a token order, P5 trace, P6 "identical to P4", P4 "no gh installed") and the LEARNINGS P6 claim.
5. Build the live tier once the test account exists.
