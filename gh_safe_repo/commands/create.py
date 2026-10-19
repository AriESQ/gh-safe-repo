"""gh-safe-repo new — Create an empty repo with safe defaults.

This module keeps the name `create` because `run()` is the engine for all
three creating verbs: `new` (nothing else set), `push` (args.local_path set)
and `copy` (args.from_repo set). The latter two only parse their positionals.
"""

import os
import shlex
import subprocess
import sys
from typing import Optional

from ..diff import Change, ChangeCategory, ChangeType, Plan
from ..errors import APIError, AuthError, ConfigError, RepoExistsError, SafeRepoError
from ..github_client import LocalWiring
from ..git_transport import (
    discover_transport,
    git_protocol_preference,
    security_key_hint,
)
from ..plugins.actions import ActionsPlugin
from ..plugins.branch_protection import BranchProtectionPlugin
from ..plugins.repository import RepositoryPlugin
from ..plugins.security import SecurityPlugin
from ..plugins.tag_protection import TagProtectionPlugin
from ..security_scanner import SecurityScanner
from ..verify import (
    default_branch_probe,
    local_wiring_probes,
    mirrored_ref_probes,
    pushed_ref_probes,
    verify_create,
)
from ._common import (
    _BOLD,
    _DIM,
    _GREEN,
    _RESET,
    _YELLOW,
    _c,
    _resolve_branches,
    add_common_args,
    add_create_args,
    confirm,
    build_context,
    check_repo_exists,
    error,
    format_plan_json,
    info,
    parse_repo_arg,
    print_plan,
    print_success,
    print_verification,
    prose_stream,
    prose_to_stderr,
    resolve_config_arg,
    run_preflight_scan,
    run_preflight_scan_local,
    warn,
)

NAME = "new"
HELP = "Create an empty repo with safe defaults"


def add_arguments(parser):
    parser.add_argument(
        "repo",
        metavar="OWNER/REPO",
        help="New repository; OWNER must be the authenticated user",
    )
    add_create_args(parser)
    add_common_args(parser)
    # `push` and `copy` set these before delegating to run(); `new` has no
    # code source.
    parser.set_defaults(local_path=None, from_repo=None)


def _abort_on_scan(decision, _info):
    """Exit after a pre-flight scan said stop. Never returns.

    A user declining at the prompt is a normal outcome (exit 0). A blocked run —
    criticals under --yes, or findings with no terminal to confirm on — is a
    failure (exit 1), so callers never mistake "nothing was created" for success.
    """
    if decision.blocked:
        error(f"{decision.reason}. Nothing was created.")
        sys.exit(1)
    _info(_c(_YELLOW, "\nAborted by user."))
    sys.exit(0)


def _local_branch(local_path):
    """Current branch of local_path, or None (not a repo, detached HEAD)."""
    # Same test push_local uses; `git -C` alone would walk up into a parent repo.
    if not os.path.isdir(os.path.join(local_path, ".git")):
        return None
    try:
        result = subprocess.run(
            ["git", "-C", local_path, "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _local_branches(local_path):
    """Return (branches, default) for a local git repo, or ([], None).

    default is the branch the new repo should use: init.defaultBranch, then
    main, then master, when one exists locally; otherwise the current branch
    (#86 — previously whatever happened to be checked out).
    """
    if not os.path.isdir(os.path.join(local_path, ".git")):
        return [], None
    def git(*args):
        try:
            r = subprocess.run(["git", "-C", local_path, *args],
                               capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""
    branches = git("for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
    for candidate in (git("config", "init.defaultBranch"), "main", "master"):
        if candidate and candidate in branches:
            return branches, candidate
    current = _local_branch(local_path)
    return branches, current or (branches[0] if branches else None)


def _auth_hint(transport, message):
    """Likely cause of a git auth failure: a missed key touch first, then
    an SSH key setting that didn't apply from this directory (#100)."""
    return security_key_hint(message) or (
        transport.auth_hint(message) if transport is not None else None
    )


def _abort_on_push_failure(e, owner, repo_name, recovery, transport=None):
    """A failed code push defeats the point of push/copy (#79).

    The repo already exists, so re-running `create` cannot recover. Say so,
    give the exact commands, and exit 1 so callers never mistake this for
    success. Branch and tag protection are skipped: there is no branch yet,
    and `fix` applies both once the code is pushed.
    """
    error(f"Code push failed: {e}")
    hint = _auth_hint(transport, str(e))
    if hint:
        error(f"Likely cause: {hint}")
    lines = [
        f"\nThe repository {owner}/{repo_name} was created and configured, but is empty.",
        "Re-running `create` will fail because the repo now exists. To finish:",
    ]
    lines += [f"  {cmd}" for cmd in recovery]
    lines.append(f"  gh-safe-repo fix {owner}/{repo_name} --yes   # applies branch/tag protection")
    print("\n".join(lines), file=sys.stderr)
    sys.exit(1)


def _code_probes(client, owner, repo_name, *, local_path=None, wiring=None,
                 from_owner=None, from_repo=None, source_default_branch=None):
    """Verification probes for the code `push` / `copy` pushed (#77).

    - Fatal (exit 1): every pushed branch and tag is on GitHub at the pushed
      SHA (`copy`: the source's branches and tags). Read through the REST
      API, never `git ls-remote`, so verifying costs no security-key touch.
      Remediation is the recovery command in `_abort_on_push_failure`'s style.
    - Settings-class (exit 3): the default branch is the one create chose.
    - Advisory (warning only): the user's repo is wired to the new remote.
    """
    dest = client.transport.persistent_url(owner, repo_name)
    then_fix = (
        f"then run `gh-safe-repo fix {owner}/{repo_name} --yes` "
        "(applies branch/tag protection)"
    )
    if from_repo:
        src = client.transport.persistent_url(from_owner, from_repo)
        probes = mirrored_ref_probes(
            client, from_owner, from_repo, owner, repo_name,
            remediation=(
                f"Mirror it again: git clone --mirror {src} {repo_name}.git && "
                f"git -C {repo_name}.git push --mirror {dest}; {then_fix}."
            ),
        )
        if source_default_branch:
            probes.append(default_branch_probe(
                client, owner, repo_name, source_default_branch,
            ))
        return probes

    if not isinstance(wiring, LocalWiring):
        wiring = LocalWiring()
    where = shlex.quote(local_path)
    is_git = os.path.isdir(os.path.join(local_path, ".git"))
    if is_git:
        remediation = (
            f"Push it again: cd {where} && git push {dest} "
            "'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*' "
            "(a SHA mismatch means GitHub holds different commits: compare "
            f"before forcing); {then_fix}."
        )
    else:
        remediation = f"No branch reached GitHub: push the code to {dest}; {then_fix}."
    probes = pushed_ref_probes(
        client, owner, repo_name,
        branches=wiring.pushed, tags=wiring.tags, remediation=remediation,
    )
    if wiring.default_branch:
        probes.append(default_branch_probe(
            client, owner, repo_name, wiring.default_branch,
        ))
    if is_git:
        probes += local_wiring_probes(
            local_path, dest, wiring.branch, foreign_origin=wiring.foreign_origin,
        )
    return probes


def run(args):
    owner, repo_name = parse_repo_arg(args.repo)
    resolve_config_arg(args)

    # Validate the `copy` source format
    if args.from_repo:
        from_owner, from_repo = parse_repo_arg(args.from_repo)

    ctx = build_context(args, owner)
    config = ctx.config
    client = ctx.client
    is_paid_plan = ctx.is_paid_plan

    # Git push URLs are case-sensitive; redirected pushes reject workflow files.
    owner = ctx.owner

    # Git transport for paths that push or clone. The OAuth token used for API
    # calls cannot push workflow files without the `workflow` scope, so we use
    # the user's own git credentials instead. The credential preflight runs
    # later, just before the repo is created.
    #
    # source_dir for `push PATH` is PATH itself (so per-directory git config
    # like includeIf-set core.sshCommand is honored). For `copy`, no local
    # clone exists yet — use os.getcwd() since that's what the user's shell-CWD
    # includeIf would have matched for any other git command they ran.
    if (args.local_path or args.from_repo) and not args.dry_run:
        source_dir = (
            os.path.abspath(args.local_path) if args.local_path else os.getcwd()
        )
        try:
            client.transport = discover_transport(
                source_dir,
                debug=args.debug,
                mode=config.get("git_transport", "mode", fallback="auto"),
                token=client.token,
            )
        except (AuthError, ConfigError) as e:
            error(str(e))
            sys.exit(1)

    # Apply CLI overrides
    overrides = {}
    if args.public:
        overrides[("repo", "private")] = "false"
    if overrides:
        config.apply_overrides(overrides)

    is_public = not config.getbool("repo", "private", fallback=True)

    json_mode = args.json
    _info = lambda msg: info(msg, json_mode=json_mode)

    _info(f"\nConfiguring {_BOLD}{owner}/{repo_name}{_RESET}...")

    # Check repo doesn't already exist
    if not args.dry_run:
        try:
            if check_repo_exists(client, owner, repo_name):
                raise RepoExistsError(owner, repo_name)
        except RepoExistsError as e:
            error(str(e))
            sys.exit(1)
        except APIError as e:
            error(f"Failed to check if repo exists: {e}")
            sys.exit(1)

    # Validate the `push` source path
    local_path = None
    if args.local_path:
        local_path = os.path.abspath(args.local_path)
        if not os.path.isdir(local_path):
            error(f"Source path '{args.local_path}' is not a directory")
            sys.exit(2)
        if not os.path.exists(os.path.join(local_path, ".git")):
            error(f"Source path '{args.local_path}' is not a git repository (run 'git init' first)")
            sys.exit(2)

    # Validate source repo exists (`copy`) and capture metadata
    source_default_branch = None
    source_description = ""
    source_topics: list = []
    if args.from_repo and not args.dry_run:
        try:
            if not check_repo_exists(client, from_owner, from_repo):
                error(f"Source repo '{from_owner}/{from_repo}' does not exist.")
                sys.exit(1)
            source_default_branch = client.get_default_branch(from_owner, from_repo)
            source_data = client.get_repo_data(from_owner, from_repo)
            source_description = source_data.get("description") or ""
            try:
                topics_resp = client.get_json(f"/repos/{from_owner}/{from_repo}/topics")
                source_topics = topics_resp.get("names", [])
            except APIError:
                source_topics = []
        except APIError as e:
            error(f"Failed to check source repo: {e}")
            sys.exit(1)

    # Create scanner once
    scanner: Optional[SecurityScanner] = None
    if args.from_repo or args.local_path:
        scanner = SecurityScanner(config, debug=args.debug, progress=not json_mode)

    # Pre-flight security scan (`copy`, non-dry-run only)
    if args.from_repo and not args.dry_run:
        try:
            # The scan report and its prompt print to stdout; under --json
            # stdout is reserved for the plan document.
            with prose_to_stderr(json_mode):
                decision = run_preflight_scan(
                    client, from_owner, from_repo, config,
                    debug=args.debug, scanner=scanner, yes=args.yes,
                )
        except APIError as e:
            error(f"Pre-flight scan failed (clone error): {e}")
            hint = _auth_hint(client.transport, str(e))
            if hint:
                error(f"Likely cause: {hint}")
            sys.exit(1)
        if not decision.proceed:
            _abort_on_scan(decision, _info)

    # Pre-flight security scan (`push`, non-dry-run only)
    if local_path and not args.dry_run:
        with prose_to_stderr(json_mode):
            decision = run_preflight_scan_local(
                local_path, config, debug=args.debug, scanner=scanner, yes=args.yes
            )
        if not decision.proceed:
            _abort_on_scan(decision, _info)

    # Detect local repo's default branch
    local_branches, local_default_branch = (
        _local_branches(local_path) if local_path else ([], None)
    )

    # Resolve branches to protect
    branches = _resolve_branches(
        config,
        source_default_branch=local_default_branch or source_default_branch,
    )

    # Plain create: auto_init=True so the repo has a branch for protection.
    # push / copy: auto_init=False to avoid push rejection.
    is_plain_create = not local_path and not args.from_repo
    repo_auto_init = True if is_plain_create else False
    # The README auto_init creates is only a side-effect of needing a branch.
    # Unless the user opts into an initialized README (auto_init = true), remove
    # it after creation so the new repo is left clean.
    suppress_readme = is_plain_create and not config.getbool(
        "repo", "auto_init", fallback=False
    )
    plugins = [
        RepositoryPlugin(client, owner, repo_name, config, auto_init=repo_auto_init,
                         source_description=source_description, source_topics=source_topics,
                         suppress_readme=suppress_readme),
        ActionsPlugin(client, owner, repo_name, config, is_public=is_public),
        BranchProtectionPlugin(client, owner, repo_name, config, is_public=is_public, is_paid_plan=is_paid_plan, branches=branches),
        SecurityPlugin(client, owner, repo_name, config, is_public=is_public, is_paid_plan=is_paid_plan),
        TagProtectionPlugin(client, owner, repo_name, config, is_public=is_public, is_paid_plan=is_paid_plan),
    ]

    full_plan = Plan()
    plugin_plans = []  # (plugin, its own plan) — verification re-audits per plugin
    for plugin in plugins:
        try:
            plugin_plan = plugin.plan()
            full_plan.merge(plugin_plan)
            # Keep a Plan of exactly this plugin's rows for verification.
            own = Plan()
            own.merge(plugin_plan)
            plugin_plans.append((plugin, own))
        except SafeRepoError as e:
            error(f"Planning failed: {e}")
            sys.exit(1)

    # Add scan + code mirror steps to the plan for `copy`
    if args.from_repo:
        scan_desc = scanner.scanner_description if scanner else ""
        full_plan.add(Change(
            type=ChangeType.ADD,
            category=ChangeCategory.SCAN,
            key="pre_flight_scan",
            new=(
                f"Scan {from_owner}/{from_repo} locally for secrets, emails, large files, TODOs"
                f" ({scan_desc})"
            ),
        ))
        full_plan.add(Change(
            type=ChangeType.ADD,
            category=ChangeCategory.FILE,
            key="code",
            new=f"Mirror all refs from {from_owner}/{from_repo}",
        ))

    # Add scan + code push steps to the plan for `push`
    if args.local_path:
        scan_desc = scanner.scanner_description if scanner else ""
        full_plan.add(Change(
            type=ChangeType.ADD,
            category=ChangeCategory.SCAN,
            key="pre_flight_scan",
            new=(
                f"Scan {local_path} locally for secrets, emails, large files, TODOs"
                f" ({scan_desc})"
            ),
        ))
        full_plan.add(Change(
            type=ChangeType.ADD,
            category=ChangeCategory.FILE,
            key="code",
            new=(
                f"Push {len(local_branches)} branch(es) from {local_path}"
                f" (default: {local_default_branch})"
                if local_branches else f"Push code from {local_path}"
            ),
        ))

    # Under --json, stdout carries exactly one document. --dry-run prints the
    # plan right away (unchanged); a real run prints it once at the end, with
    # the verification block (#77), same as `fix`.
    if json_mode and args.dry_run:
        print(format_plan_json(full_plan))
    elif not json_mode:
        print_plan(full_plan)
    elif not args.yes:
        # Interactive --json: show the table on stderr so the prompt has context.
        with prose_to_stderr(json_mode):
            print_plan(full_plan)

    counts = full_plan.count_by_type()
    actionable = sum(v for k, v in counts.items() if k != ChangeType.SKIP)
    skipped = counts.get(ChangeType.SKIP, 0)

    _info(_c(_BOLD + _DIM, f"{actionable} change(s) to apply, {skipped} skipped"))

    if args.dry_run:
        _info(_c(_YELLOW, "\nDry run — no changes made."))
        sys.exit(0)

    # Prompt confirmation (skip with --yes)
    if not args.yes:
        if not confirm(
            f"\nCreate {owner}/{repo_name} and apply {actionable} change(s)? [y/N]: ",
            json_mode=json_mode,
            nothing_done="Nothing was created.",
        ):
            _info(_c(_YELLOW, "Aborted."))
            if json_mode:
                print(format_plan_json(full_plan))
            sys.exit(0)

    # Verify git credentials before creating the repo, so a credential failure
    # can't leave an empty repo behind. Run it after the scan and the prompt,
    # not before: a security-key touch proves only that the key answered then,
    # so the gap to the real push should be as short as possible (#79).
    if args.local_path or args.from_repo:
        try:
            client.transport.preflight()
        except AuthError as e:
            error(str(e))
            sys.exit(1)

    # Apply changes
    repo_plugin      = plugins[0]
    actions_plugin   = plugins[1]
    bp_plugin        = plugins[2]
    security_plugin  = plugins[3]
    tag_plugin       = plugins[4]

    # Apply repo creation + settings
    try:
        repo_plugin.apply(full_plan)
    except RepoExistsError as e:
        error(str(e))
        sys.exit(1)
    except APIError as e:
        error(f"Failed to create repository: {e}")
        sys.exit(1)

    # Refine branch list from POST response
    post_default = repo_plugin.created_default_branch
    if post_default:
        bp_plugin.branches = [post_default]

    # Remove the auto_init README (before branch protection) unless the user
    # opted into an initialized README via auto_init = true.
    try:
        repo_plugin.remove_readme()
    except APIError as e:
        warn(f"Repository created but README removal failed: {e}")

    # Apply Actions settings
    try:
        actions_plugin.apply(full_plan)
    except APIError as e:
        warn(f"Repository created but Actions settings failed: {e}")

    # Apply security settings
    try:
        security_plugin.apply(full_plan)
    except APIError as e:
        warn(f"Security settings failed: {e}")

    # Mirror code from source repo (`copy`)
    if args.from_repo:
        _info(f"\nCopying code from {_BOLD}{from_owner}/{from_repo}{_RESET}...")
        try:
            # A mirror push carries no HEAD: set the source's default branch
            # explicitly, and protect that branch rather than the name the
            # empty repo was created with.
            client.copy_repo(from_owner, from_repo, repo_name,
                             default_branch=source_default_branch)
            if source_default_branch:
                bp_plugin.branches = [source_default_branch]
            _info(_c(_GREEN, f"  Code mirrored successfully."))
        except APIError as e:
            src = client.transport.persistent_url(from_owner, from_repo)
            dest = client.transport.persistent_url(owner, repo_name)
            _abort_on_push_failure(e, owner, repo_name, [
                f"git clone --mirror {src} {repo_name}.git",
                f"git -C {repo_name}.git push --mirror {dest}",
            ], transport=client.transport)

    # Push code from local directory (`push`)
    wiring = None
    if args.local_path:
        _info(f"\nPushing code from {_BOLD}{local_path}{_RESET}...")
        try:
            wiring = client.push_local(
                local_path, owner, repo_name, default_branch=local_default_branch
            )
            if wiring is not None and wiring.default_branch:
                bp_plugin.branches = [wiring.default_branch]
            _info(_c(_GREEN, "  Code pushed successfully."))
            if wiring is not None and wiring.error:
                warn(f"Code pushed, but local repo not fully wired: {wiring.error}")
        except APIError as e:
            url = client.transport.persistent_url(owner, repo_name)
            branch = _local_branch(local_path)
            # push_local raises before wiring origin, so add it here.
            # Default branch first so GitHub makes it the default (#86).
            # Explicit <b>:<b> refspec: cannot land on another branch.
            first = local_default_branch or branch
            recovery = [f"cd {local_path}", f"git remote add origin {url}"]
            if first:
                recovery.append(f"git push -u origin {first}:{first}")
            recovery += ["git push origin --all", "git push origin --tags"]
            _abort_on_push_failure(e, owner, repo_name, recovery,
                                   transport=client.transport)

    # Apply branch protection after code push
    try:
        bp_plugin.apply(full_plan)
    except APIError as e:
        warn(f"Repository created but branch protection failed: {e}")

    # Apply tag protection
    try:
        tag_plugin.apply(full_plan)
    except APIError as e:
        warn(f"Tag protection failed: {e}")

    # Verify: re-read what GitHub now holds. The apply steps above only prove
    # no exception escaped (#77). Settings use each plugin's own audit (the
    # same instances, so bp_plugin.branches is already narrowed); create-only
    # rows (repo exists, visibility, README removed, copy description and
    # topics, protection presence) get dedicated probes. push / copy add
    # the pushed refs (fatal), the default branch and the local wiring
    # (advisory) through `extra_probes`.
    code_probes = []
    if args.from_repo:
        code_probes = _code_probes(
            client, owner, repo_name, from_owner=from_owner, from_repo=from_repo,
            source_default_branch=source_default_branch,
        )
    elif args.local_path:
        code_probes = _code_probes(
            client, owner, repo_name, local_path=local_path, wiring=wiring,
        )
    _info(_c(_DIM, "\nVerifying..."))
    verification = verify_create(
        plugin_plans,
        private=not is_public,
        extra_probes=code_probes,
        remediation=(
            f"Run `gh-safe-repo fix {owner}/{repo_name} --yes` once; if it "
            "still does not stick, an organization policy or plan limit is "
            "overriding it — check the repo settings page."
        ),
    )

    out = prose_stream(json_mode)
    print_verification(verification, file=out)
    if verification.unknowns:
        warn(
            f"{len(verification.unknowns)} setting(s) could not be read back to "
            "confirm they were applied (see above). Not retrying further."
        )
    # Already warned at push time when push_local itself reported the problem.
    if verification.advisories and not getattr(wiring, "error", None):
        warn(
            "Your local repo is not wired to the new remote (see above). "
            "The new repo is fine; this does not change the exit status."
        )

    protocol = (
        client.transport.protocol
        if getattr(client, "transport", None) is not None
        else git_protocol_preference()
    )
    print_success(
        owner, repo_name, local_push=bool(args.local_path), protocol=protocol,
        wiring=wiring, file=out, verification=verification,
    )

    if json_mode:
        print(format_plan_json(full_plan, verification=verification))

    code = verification.exit_code()
    if code:
        sys.exit(code)
