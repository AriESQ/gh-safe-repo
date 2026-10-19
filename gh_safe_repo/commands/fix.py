"""gh-safe-repo fix — Show settings diff and apply corrections to an existing repo."""

import sys
from dataclasses import replace

from ..config_manager import PREFERENCE, SECURITY, is_stricter
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
    add_mutation_args,
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
    resolve_config_arg,
    warn,
)
from ..verify import Verification, verify_plugins

NAME = "fix"
HELP = "Audit an existing repo and apply safe defaults"


def add_all_arg(parser):
    """--all, identical on `audit` and `fix` (the two existing-repo verbs)."""
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Treat every differing setting as a change to make, including ones "
            "a person deliberately changed from GitHub's default (no PRESERVE rows)"
        ),
    )


def add_arguments(parser):
    parser.add_argument(
        "repo",
        metavar="OWNER/REPO",
        help="Existing repository (yours, an org's, or one you collaborate on); requires admin access",
    )
    add_mutation_args(parser)
    add_all_arg(parser)
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


def apply_merge_policy(plan, config, all_=False):
    """Three-way merge for an existing repo (docs/2026-03-28-user-stories.md).

    base   = GitHub's default for the setting  (``change.github_default``)
    theirs = the repo's current value          (``change.old``)
    ours   = the desired value                 (``change.new``)

    Rows the plugins emit are two-way (ours vs theirs). This turns an UPDATE
    into PRESERVE, or flags it ``weakened``, by asking whether a human moved
    the setting away from the base:

    - theirs == base: nobody chose it → UPDATE (unchanged).
    - theirs != base ("conflict"):
        preference tier                     → PRESERVE
        security tier, theirs stricter      → PRESERVE
        security tier, theirs weaker        → UPDATE, weakened=True
      except when the user's INI file declares the section: explicit config
      wins, the row stays UPDATE (still flagged weakened where that holds).

    ``all_`` skips the policy: every row is returned as the plugin emitted it.
    Rows without merge context (no ``tier``: SKIP rows, create-only rows,
    mock plans) pass through. Returns a new Plan; ``plan`` is not modified.
    """
    if all_:
        return plan
    out = Plan()
    for c in plan.changes:
        if c.type != ChangeType.UPDATE or c.tier is None or c.old == c.github_default:
            out.add(c)
            continue
        section = c.category.value
        weaker = c.tier == SECURITY and is_stricter(section, c.key, c.new, c.old)
        if config.is_declared(section):
            out.add(replace(c, weakened=weaker))
        elif c.tier == PREFERENCE:
            out.add(replace(c, type=ChangeType.PRESERVE, reason="changed from default"))
        elif is_stricter(section, c.key, c.old, c.new):
            out.add(replace(c, type=ChangeType.PRESERVE, reason=f"stricter than {c.new!r}"))
        else:
            out.add(replace(c, weakened=weaker))
    return out


def run(args, audit=False):
    """Audit, then apply unless args.dry_run.

    `audit=True` (the `audit` verb) is a dry run that reports drift through
    its exit code: 3 when anything would change, 0 when compliant. PRESERVE
    rows (a human's deliberate choice, kept by `apply_merge_policy`) are not
    drift; `--all` turns the policy off on both verbs.
    """
    owner, repo_name = parse_repo_arg(args.repo)
    resolve_config_arg(args)

    ctx = build_context(args, owner, require_owner_match=False)
    config = ctx.config
    client = ctx.client
    is_paid_plan = ctx.is_paid_plan
    policy = lambda plan: apply_merge_policy(plan, config, all_=args.all)

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
                "  - To create it, use `gh-safe-repo new`."
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
            plugin_plan = policy(plugin.plan(current_state=current_state))
            full_plan.merge(plugin_plan)
            plugin_plans.append((plugin, plugin_plan))
        except SafeRepoError as e:
            error(f"Planning failed: {e}")
            sys.exit(1)

    # Under --json, stdout carries exactly one document. --dry-run prints the
    # plan right away (unchanged); a real run prints it once at the end, with
    # the verification block (#77).
    counts = full_plan.count_by_type()
    actionable_count = len(full_plan.actionable_changes)
    skipped = counts.get(ChangeType.SKIP, 0)
    preserved = counts.get(ChangeType.PRESERVE, 0)

    if audit and not json_mode and actionable_count == 0 and preserved == 0:
        # Weekly-hygiene case: one line, not a table of SKIP rows. PRESERVE
        # rows are still shown: the user should see what was left alone.
        _info(_c(_GREEN, "All settings compliant."))
        sys.exit(0)

    if json_mode and args.dry_run:
        print(format_plan_json(full_plan))
    elif not json_mode:
        print_plan(full_plan)
    elif not args.yes and full_plan.actionable_changes:
        # Interactive --json: show the table on stderr so the prompt has context.
        with prose_to_stderr(json_mode):
            print_plan(full_plan)

    _info(_c(
        _BOLD + _DIM,
        f"{actionable_count} change(s) to apply, {preserved} preserved, {skipped} skipped",
    ))
    if preserved and not args.all:
        _info(_c(
            _DIM,
            "PRESERVE rows were changed from GitHub's default by a person and are "
            "left alone; declare the section in your config, or pass --all, to "
            "enforce them.",
        ))

    if audit:
        if actionable_count:
            _info(_c(_YELLOW, f"\nRun `gh-safe-repo fix {owner}/{repo_name}` to apply."))
        sys.exit(3 if actionable_count else 0)

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
        policy=policy,
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
