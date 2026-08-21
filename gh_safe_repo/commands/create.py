"""gh-safe-repo create — Create a new repo with safe defaults."""

import os
import subprocess
import sys
from typing import Optional

from ..diff import Change, ChangeCategory, ChangeType, Plan
from ..errors import APIError, AuthError, ConfigError, RepoExistsError, SafeRepoError
from ..git_transport import discover_transport, git_protocol_preference
from ..plugins.actions import ActionsPlugin
from ..plugins.branch_protection import BranchProtectionPlugin
from ..plugins.repository import RepositoryPlugin
from ..plugins.security import SecurityPlugin
from ..plugins.tag_protection import TagProtectionPlugin
from ..security_scanner import SecurityScanner
from ._common import (
    _BOLD,
    _DIM,
    _GREEN,
    _RESET,
    _YELLOW,
    _c,
    _resolve_branches,
    add_common_args,
    build_context,
    check_repo_exists,
    error,
    format_plan_json,
    info,
    parse_repo_arg,
    print_plan,
    print_success,
    run_preflight_scan,
    run_preflight_scan_local,
    warn,
)

NAME = "create"
HELP = "Create a new repo with safe defaults"


def add_arguments(parser):
    parser.add_argument(
        "repo",
        help="New GitHub repository as owner/repo (e.g. myuser/my-repo)",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Create a public repository instead of private",
    )
    parser.add_argument(
        "--local",
        dest="local_path",
        metavar="PATH",
        help="Push code from a local git repository into the new repo",
    )
    parser.add_argument(
        "--from",
        dest="from_repo",
        metavar="OWNER/REPO",
        help="Mirror code from an existing repo into the new repo (runs pre-flight scan)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be configured without creating anything",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt and apply immediately",
    )
    add_common_args(parser)


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


def run(args):
    owner, repo_name = parse_repo_arg(args.repo)

    # --local and --from are mutually exclusive
    if args.local_path and args.from_repo:
        error("--local and --from are mutually exclusive")
        sys.exit(2)

    # Validate --from format
    if args.from_repo:
        from_owner, from_repo = parse_repo_arg(args.from_repo)

    ctx = build_context(args, owner)
    config = ctx.config
    client = ctx.client
    is_paid_plan = ctx.is_paid_plan

    # Git push URLs are case-sensitive; redirected pushes reject workflow files.
    owner = ctx.owner

    # Pre-flight git credential check for paths that push or clone.
    # The OAuth token used for API calls cannot push workflow files without the
    # `workflow` scope, so we use the user's own git credentials instead. Verify
    # they work before creating the repo, so we don't leave an empty repo behind.
    #
    # source_dir for `--local PATH` is PATH itself (so per-directory git config
    # like includeIf-set core.sshCommand is honored). For `--from`, no local
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
            client.transport.preflight()
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

    # Validate --local path
    local_path = None
    if args.local_path:
        local_path = os.path.abspath(args.local_path)
        if not os.path.isdir(local_path):
            error(f"--local: '{args.local_path}' is not a directory")
            sys.exit(2)
        if not os.path.exists(os.path.join(local_path, ".git")):
            error(f"--local: '{args.local_path}' is not a git repository (run 'git init' first)")
            sys.exit(2)

    # Validate source repo exists (--from workflow) and capture metadata
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
        scanner = SecurityScanner(config, debug=args.debug)

    # Pre-flight security scan (--from workflow, non-dry-run only)
    if args.from_repo and not args.dry_run:
        try:
            decision = run_preflight_scan(
                client, from_owner, from_repo, config,
                debug=args.debug, scanner=scanner, yes=args.yes,
            )
        except APIError as e:
            error(f"Pre-flight scan failed (clone error): {e}")
            sys.exit(1)
        if not decision.proceed:
            _abort_on_scan(decision, _info)

    # Pre-flight security scan (--local workflow, non-dry-run only)
    if local_path and not args.dry_run:
        decision = run_preflight_scan_local(
            local_path, config, debug=args.debug, scanner=scanner, yes=args.yes
        )
        if not decision.proceed:
            _abort_on_scan(decision, _info)

    # Detect local repo's default branch
    local_default_branch = None
    if local_path and os.path.isdir(os.path.join(local_path, ".git")):
        try:
            r = subprocess.run(
                ["git", "-C", local_path, "symbolic-ref", "--short", "HEAD"],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0:
                local_default_branch = r.stdout.strip() or None
        except Exception:
            pass

    # Resolve branches to protect
    branches = _resolve_branches(
        config,
        source_default_branch=local_default_branch or source_default_branch,
    )

    # Plain create: auto_init=True so the repo has a branch for protection.
    # --local / --from: auto_init=False to avoid push rejection.
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
    for plugin in plugins:
        try:
            plugin_plan = plugin.plan()
            full_plan.merge(plugin_plan)
        except SafeRepoError as e:
            error(f"Planning failed: {e}")
            sys.exit(1)

    # Add scan + code mirror steps to the plan if --from is specified
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

    # Add scan + code push steps to the plan if --local is specified
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
            new=f"Push code from {local_path}",
        ))

    # Print the plan
    if json_mode:
        print(format_plan_json(full_plan))
    else:
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
        try:
            answer = input(
                f"\nCreate {owner}/{repo_name} and apply {actionable} change(s)? [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if answer not in ("y", "yes"):
            _info(_c(_YELLOW, "Aborted."))
            sys.exit(0)

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

    # Mirror code from source repo (--from workflow)
    if args.from_repo:
        _info(f"\nCopying code from {_BOLD}{from_owner}/{from_repo}{_RESET}...")
        try:
            client.copy_repo(from_owner, from_repo, repo_name)
            _info(_c(_GREEN, f"  Code mirrored successfully."))
        except APIError as e:
            warn(f"Code copy failed: {e}")

    # Push code from local directory (--local workflow)
    if args.local_path:
        _info(f"\nPushing code from {_BOLD}{local_path}{_RESET}...")
        try:
            client.push_local(local_path, owner, repo_name)
            _info(_c(_GREEN, "  Code pushed successfully."))
        except APIError as e:
            warn(f"Code push failed: {e}")

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

    protocol = (
        client.transport.protocol
        if getattr(client, "transport", None) is not None
        else git_protocol_preference()
    )
    print_success(
        owner, repo_name, local_push=bool(args.local_path), protocol=protocol
    )
