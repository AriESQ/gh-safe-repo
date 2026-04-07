"""gh-safe-repo fix — Show settings diff and apply corrections to an existing repo."""

import sys

from ..diff import ChangeType, Plan
from ..errors import APIError, SafeRepoError
from ..plugins.actions import ActionsPlugin
from ..plugins.branch_protection import BranchProtectionPlugin
from ..plugins.repository import RepositoryPlugin
from ..plugins.security import SecurityPlugin
from ..plugins.tag_protection import TagProtectionPlugin
from ._common import (
    _BOLD,
    _GREEN,
    _RESET,
    _YELLOW,
    _c,
    _resolve_branches,
    add_common_args,
    build_context,
    error,
    format_plan_json,
    info,
    parse_repo_arg,
    print_plan,
    print_success_fix,
    warn,
)

NAME = "fix"
HELP = "Audit an existing repo and apply safe defaults"


def add_arguments(parser):
    parser.add_argument(
        "repo",
        help="Existing GitHub repository as owner/repo (e.g. myuser/my-repo or my-org/my-repo); requires admin access",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt and apply immediately",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show settings diff without applying changes",
    )
    add_common_args(parser)


def run(args):
    owner, repo_name = parse_repo_arg(args.repo)

    ctx = build_context(args, owner, require_owner_match=False)
    config = ctx.config
    client = ctx.client
    is_paid_plan = ctx.is_paid_plan

    json_mode = args.json
    _info = lambda msg: info(msg, json_mode=json_mode)

    _info(f"\nAuditing {_BOLD}{owner}/{repo_name}{_RESET}...")

    # Verify repo exists and fetch its data
    try:
        repo_data = client.get_repo_data(owner, repo_name)
    except APIError as e:
        if e.status_code == 404:
            error(
                f"Repository '{owner}/{repo_name}' does not exist. "
                "Use `gh-safe-repo create` to create it."
            )
        else:
            error(f"Failed to fetch repository info: {e}")
        sys.exit(1)

    # Verify the authenticated user has admin access (required to change settings)
    if not repo_data.get("permissions", {}).get("admin", False):
        error(
            f"You do not have admin permissions on '{owner}/{repo_name}'. "
            "Admin access is required to modify repository settings."
        )
        sys.exit(1)

    is_public = not repo_data.get("private", True)
    audit_default_branch = repo_data.get("default_branch")

    audit_branches = (
        [audit_default_branch] if audit_default_branch
        else _resolve_branches(config)
    )

    # Build plugins and fetch current state per plugin
    plugins = [
        RepositoryPlugin(client, owner, repo_name, config),
        ActionsPlugin(client, owner, repo_name, config, is_public=is_public),
        BranchProtectionPlugin(
            client, owner, repo_name, config,
            is_public=is_public, is_paid_plan=is_paid_plan,
            branches=audit_branches,
        ),
        SecurityPlugin(
            client, owner, repo_name, config,
            is_public=is_public, is_paid_plan=is_paid_plan,
        ),
        TagProtectionPlugin(
            client, owner, repo_name, config,
            is_public=is_public, is_paid_plan=is_paid_plan,
        ),
    ]

    full_plan = Plan()
    for plugin in plugins:
        try:
            current_state = plugin.fetch_current_state()
            plugin_plan = plugin.plan(current_state=current_state)
            full_plan.merge(plugin_plan)
        except SafeRepoError as e:
            error(f"Planning failed: {e}")
            sys.exit(1)

    # Print plan
    if json_mode:
        print(format_plan_json(full_plan))
    else:
        print_plan(full_plan)

    counts = full_plan.count_by_type()
    actionable_count = sum(v for k, v in counts.items() if k != ChangeType.SKIP)
    skipped = counts.get(ChangeType.SKIP, 0)
    _info(_c(_BOLD + "\033[2m", f"{actionable_count} change(s) to apply, {skipped} skipped"))

    if args.dry_run:
        _info(_c(_YELLOW, "\nDry run — no changes made."))
        sys.exit(0)

    # Check if there is anything to do
    actionable = full_plan.actionable_changes
    if not actionable:
        _info(_c(_GREEN, "\nAlready at desired state — nothing to do."))
        sys.exit(0)

    # Prompt confirmation (skip with --yes)
    if not args.yes:
        try:
            answer = input(
                f"\nApply {len(actionable)} change(s) to {owner}/{repo_name}? [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if answer not in ("y", "yes"):
            _info(_c(_YELLOW, "Aborted."))
            sys.exit(0)

    # Apply settings
    for plugin in plugins:
        try:
            plugin.apply(full_plan)
        except APIError as e:
            warn(f"Some settings failed to apply: {e}")

    print_success_fix(owner, repo_name)
