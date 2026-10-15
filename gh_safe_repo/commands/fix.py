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
    _DIM,
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
    confirm,
    print_success_fix,
    print_verification,
    prose_stream,
    prose_to_stderr,
    warn,
)
from ..verify import Verification, verify_plugins

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
    parser.add_argument(
        "--migrate-branch-protection",
        action="store_true",
        help=(
            "Migrate an existing classic branch protection to a ruleset. "
            "Classic-only rules (required_status_checks, push restrictions) have "
            "no ruleset equivalent and will be dropped; the classic protection is "
            "deleted once the ruleset is in place."
        ),
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

    # Verify repo exists and fetch its data
    try:
        repo_data = client.get_repo_data(owner, repo_name)
    except APIError as e:
        if e.status_code == 404:
            error(
                f"Repository '{owner}/{repo_name}' does not exist, or it is "
                f"private and not visible to your active GitHub account "
                f"('{ctx.owner}').\n"
                "  - If it exists under a different account, you may be "
                "authenticated as the wrong GitHub account.\n"
                "  - To create it, use `gh-safe-repo create`."
            )
        else:
            error(f"Failed to fetch repository info: {e}")
        sys.exit(1)

    if args.debug:
        repo_id = repo_data.get("id", "unknown")
        full_name = repo_data.get("full_name", f"{owner}/{repo_name}")
        owner_type = repo_data.get("owner", {}).get("type", "unknown")
        print(f"[debug] repo: {full_name} (id={repo_id}, owner_type={owner_type})", file=sys.stderr)

    # Use canonical casing from the API; `fix` accepts org/collaborator repos.
    canonical_owner = repo_data.get("owner", {}).get("login")
    canonical_name = repo_data.get("name")
    if canonical_owner and canonical_name:
        owner, repo_name = canonical_owner, canonical_name

    _info(f"\nAuditing {_BOLD}{owner}/{repo_name}{_RESET}...")

    # Verify the authenticated user has admin access (required to change settings)
    if not repo_data.get("permissions", {}).get("admin", False):
        error(
            f"You do not have admin permissions on '{owner}/{repo_name}' as "
            f"GitHub account '{ctx.owner}'. Admin access is required to read or "
            "modify repository settings — you may be authenticated as the wrong "
            "account."
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
            migrate=args.migrate_branch_protection,
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
    plugin_plans = []  # (plugin, its own plan) — verification re-audits per plugin
    for plugin in plugins:
        try:
            current_state = plugin.fetch_current_state()
            plugin_plan = plugin.plan(current_state=current_state)
            full_plan.merge(plugin_plan)
            plugin_plans.append((plugin, plugin_plan))
        except SafeRepoError as e:
            error(f"Planning failed: {e}")
            sys.exit(1)

    # Under --json, stdout carries exactly one document. --dry-run prints the
    # plan right away (unchanged); a real run prints it once at the end, with
    # the verification block (#77).
    if json_mode and args.dry_run:
        print(format_plan_json(full_plan))
    elif not json_mode:
        print_plan(full_plan)
    elif not args.yes and full_plan.actionable_changes:
        # Interactive --json: show the table on stderr so the prompt has context.
        with prose_to_stderr(json_mode):
            print_plan(full_plan)

    counts = full_plan.count_by_type()
    actionable_count = sum(v for k, v in counts.items() if k != ChangeType.SKIP)
    skipped = counts.get(ChangeType.SKIP, 0)
    _info(_c(_BOLD + _DIM, f"{actionable_count} change(s) to apply, {skipped} skipped"))

    if args.dry_run:
        _info(_c(_YELLOW, "\nDry run — no changes made."))
        sys.exit(0)

    # Check if there is anything to do
    actionable = full_plan.actionable_changes
    if not actionable:
        _info(_c(_GREEN, "\nAlready at desired state — nothing to do."))
        if json_mode:
            print(format_plan_json(full_plan, verification=Verification()))
        sys.exit(0)

    # Prompt confirmation (skip with --yes)
    if not args.yes:
        if not confirm(
            f"\nApply {len(actionable)} change(s) to {owner}/{repo_name}? [y/N]: ",
            json_mode=json_mode,
        ):
            _info(_c(_YELLOW, "Aborted."))
            if json_mode:
                print(format_plan_json(full_plan))
            sys.exit(0)

    # Apply settings
    for plugin in plugins:
        try:
            plugin.apply(full_plan)
        except APIError as e:
            warn(f"Some settings failed to apply: {e}")

    # Verify: re-read every plugin that had something to change. The apply
    # loop above only proves no exception escaped; it says nothing about what
    # GitHub now holds (#77).
    _info(_c(_DIM, "\nVerifying..."))
    verification = verify_plugins(
        plugin_plans,
        remediation=(
            f"Run `gh-safe-repo fix {owner}/{repo_name} --yes` once more; if it "
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
    print_success_fix(owner, repo_name, verification=verification, file=out)

    if json_mode:
        print(format_plan_json(full_plan, verification=verification))

    code = verification.exit_code()
    if code:
        sys.exit(code)
