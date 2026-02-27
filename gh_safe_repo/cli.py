"""
gh-safe-repo — Create GitHub repositories with safe defaults applied.

Usage:
    gh-safe-repo my-project              # Create private repo with safe defaults
    gh-safe-repo my-project --dry-run    # Preview without creating
    gh-safe-repo my-project --debug      # Show every API call
    gh-safe-repo my-project --no-wiki    # Override specific setting
    gh-safe-repo my-public --from my-private --public  # Public repo from private source
    gh-safe-repo my-repo --audit         # Audit existing repo and apply safe defaults
    gh-safe-repo my-repo --audit --dry-run  # Read-only audit: show diff only
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .config_manager import ConfigManager
from .diff import Change, ChangeCategory, ChangeType, Plan
from .errors import APIError, AuthError, ConfigError, RepoExistsError, SafeRepoError
from .github_client import GitHubClient
from .plugins.actions import ActionsPlugin
from .plugins.branch_protection import BranchProtectionPlugin
from .plugins.repository import RepositoryPlugin
from .plugins.security import SecurityPlugin
from .security_scanner import FindingCategory, SecurityScanner, Severity

# ANSI escape codes
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RED    = "\033[31m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"


def _c(code, text):
    """Wrap text in an ANSI code."""
    return f"{code}{text}{_RESET}"


def print_plan(plan):
    headers = ["Type", "Category", "Setting", "Value / Note"]

    rows = []
    for change in plan.changes:
        if change.type == ChangeType.SKIP:
            rows.append(("SKIP", change.category.value, change.key, change.reason, "skip"))
        elif change.type == ChangeType.ADD:
            rows.append(("ADD", change.category.value, change.key, str(change.new), "add"))
        elif change.type == ChangeType.UPDATE:
            rows.append(("UPDATE", change.category.value, change.key, f"{change.old!r} → {change.new!r}", "update"))
        elif change.type == ChangeType.DELETE:
            rows.append(("DELETE", change.category.value, change.key, str(change.old), "delete"))

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row[:4]):
            col_widths[i] = max(col_widths[i], len(cell))

    sep = "  "
    header_line = sep.join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    divider = sep.join("-" * w for w in col_widths)

    print(f"\n{_BOLD}Planned Changes{_RESET}")
    print(_c(_DIM, header_line))
    print(_c(_DIM, divider))

    for row in rows:
        type_str, cat, key, value, kind = row
        if kind == "skip":
            line = sep.join(cell.ljust(col_widths[i]) for i, cell in enumerate(row[:4]))
            print(_c(_DIM, line))
        elif kind == "add":
            print(
                _c(_GREEN, type_str.ljust(col_widths[0])) + sep
                + cat.ljust(col_widths[1]) + sep
                + key.ljust(col_widths[2]) + sep
                + value
            )
        elif kind == "update":
            print(
                _c(_YELLOW, type_str.ljust(col_widths[0])) + sep
                + cat.ljust(col_widths[1]) + sep
                + key.ljust(col_widths[2]) + sep
                + value
            )
        elif kind == "delete":
            print(
                _c(_RED, type_str.ljust(col_widths[0])) + sep
                + cat.ljust(col_widths[1]) + sep
                + key.ljust(col_widths[2]) + sep
                + value
            )
    print()


def print_success(owner, repo):
    url = f"https://github.com/{owner}/{repo}"
    inner = f"  Repository created successfully!  \n  {url}  "
    width = max(len(line) for line in inner.splitlines()) + 2
    top    = "╭─ Done " + "─" * (width - 7) + "╮"
    bottom = "╰" + "─" * (width + 1) + "╯"
    print(f"\n{_GREEN}{top}{_RESET}")
    for line in inner.splitlines():
        print(f"{_GREEN}│{_RESET} {line.ljust(width)} {_GREEN}│{_RESET}")
    print(f"{_GREEN}{bottom}{_RESET}\n")


def print_success_audit(owner, repo):
    url = f"https://github.com/{owner}/{repo}"
    inner = f"  Repository updated successfully!  \n  {url}  "
    width = max(len(line) for line in inner.splitlines()) + 2
    top    = "╭─ Done " + "─" * (width - 7) + "╮"
    bottom = "╰" + "─" * (width + 1) + "╯"
    print(f"\n{_GREEN}{top}{_RESET}")
    for line in inner.splitlines():
        print(f"{_GREEN}│{_RESET} {line.ljust(width)} {_GREEN}│{_RESET}")
    print(f"{_GREEN}{bottom}{_RESET}\n")



def _print_findings(findings, config):
    """Print scan findings with ANSI formatting. Returns True if any criticals."""
    criticals = [f for f in findings if f.severity == Severity.CRITICAL]
    warnings  = [f for f in findings if f.severity == Severity.WARNING]
    infos     = [f for f in findings if f.severity == Severity.INFO]

    if not findings:
        print(_c(_GREEN, "  No issues found."))
        return False

    for f in criticals:
        loc = f.file_path + (f":{f.line_number}" if f.line_number else "")
        print(f"  {_c(_RED, '[CRITICAL]')} {f.rule}")
        print(_c(_DIM, f"             in {loc}"))
    for f in warnings:
        loc = f.file_path + (f":{f.line_number}" if f.line_number else "")
        print(f"  {_c(_YELLOW, '[WARNING]')} {f.rule}")
        print(_c(_DIM, f"             in {loc}"))
        if f.match and f.match != "[redacted]":
            print(_c(_DIM, f"             {f.match[:80]}"))
    for f in infos:
        loc = f.file_path + (f":{f.line_number}" if f.line_number else "")
        print(_c(_DIM, f"  [INFO] {f.rule} in {loc}"))

    print()
    banned_strings = [
        s.strip()
        for s in re.split(r"[\n,]", config.get("pre_flight_scan", "banned_strings", fallback=""))
        if s.strip()
    ]
    if banned_strings and any(f.category == FindingCategory.BANNED_STRING for f in findings):
        print(_c(_BOLD, "Banned strings found. To scrub from git history, run in your source repo:"))
        replacements = "\n".join(f"literal:{s}==>***REMOVED***" for s in banned_strings)
        print(_c(_DIM, f"  git filter-repo --replace-text <(printf '{replacements}')"))
        print()

    return bool(criticals)


def run_preflight_scan(client, owner, from_repo, config, debug=False):
    """
    Clone from_repo, scan locally, display findings, prompt user.
    Returns True to continue, False to abort. Raises APIError on clone failure.
    """
    scanner = SecurityScanner(config, debug=debug)
    print(f"\n{_c(_BOLD, 'Running pre-flight security scan...')}")

    with tempfile.TemporaryDirectory() as tmpdir:
        scan_dir = os.path.join(tmpdir, "scan")
        client.clone_for_scan(owner, from_repo, scan_dir)   # raises APIError on failure
        findings = scanner.scan(scan_dir)
    # tmpdir cleaned up here

    has_criticals = _print_findings(findings, config)

    if not findings:
        return True

    if has_criticals:
        prompt = _c(_BOLD + _RED, "Critical issues found. Continue anyway? [y/N]: ")
    else:
        prompt = _c(_YELLOW, "Warnings found. Continue? [Y/n]: ")

    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if has_criticals:
        return answer in ("y", "yes")
    else:
        return answer not in ("n", "no")


def check_repo_exists(client, owner, repo):
    """Return True if the repo already exists."""
    path = client.repo_path(owner, repo)
    status, _ = client.call_api("GET", path)
    return status == 200


def _resolve_branches(config, post_default_branch=None, source_default_branch=None) -> list:
    """
    Determine the list of branches to protect, in priority order:
      1. POST /user/repos response default_branch (new repo, non-dry-run)
      2. GET /repos/{owner}/{source} default_branch (--from workflow, non-dry-run)
      3. git symbolic-ref --short HEAD (local CWD, works in dry-run too)
      4. protected_branch from config (may be "master, main" from SAFE_DEFAULTS)
    """
    if post_default_branch:
        return [post_default_branch]
    if source_default_branch:
        return [source_default_branch]
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            if branch:
                return [branch]
    except Exception:
        pass
    raw = config.get("branch_protection", "protected_branch", fallback="master, main")
    return [b.strip() for b in raw.split(",") if b.strip()]


def main():
    parser = argparse.ArgumentParser(
        prog="gh-safe-repo",
        description="Create GitHub repositories with safe defaults applied.",
    )
    parser.add_argument("repo", nargs="?", help="Name of the repository to create or audit")
    parser.add_argument(
        "--from",
        dest="from_repo",
        metavar="SOURCE_REPO",
        help="Mirror code from this existing private repo into the new public repo",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Audit an existing repo and apply safe defaults (read, show diff, prompt, apply)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be configured without creating or changing anything",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show every API call made",
    )
    parser.add_argument(
        "--no-wiki",
        action="store_true",
        help="Disable the wiki (also the default; overrides config)",
    )
    parser.add_argument(
        "--wiki",
        action="store_true",
        help="Enable the wiki",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Create a public repository instead of private",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Path to config file (default: ~/.config/gh-safe-repo/config.ini)",
    )
    parser.add_argument(
        "--scan",
        metavar="PATH",
        help="Scan a local directory for secrets and exit (no GitHub interaction)",
    )

    args = parser.parse_args()

    # --scan: standalone local scan, no GitHub interaction
    if args.scan:
        scan_path = os.path.abspath(args.scan)
        if not os.path.isdir(scan_path):
            print(f"\033[1m\033[31mError:\033[0m '{args.scan}' is not a directory", file=sys.stderr)
            sys.exit(2)
        try:
            config = ConfigManager(config_path=args.config)
        except ConfigError as e:
            print(f"\033[1m\033[31mError:\033[0m {e}", file=sys.stderr)
            sys.exit(1)
        scanner = SecurityScanner(config, debug=args.debug)
        print(f"\n{_c(_BOLD, 'Scanning')} {scan_path}...")
        findings = scanner.scan(scan_path)
        has_criticals = _print_findings(findings, config)
        sys.exit(1 if has_criticals else 0)

    if not args.repo:
        parser.error("the following arguments are required: repo")

    # --from implies --public (can't copy code to a private repo without extra work)
    if args.from_repo and not args.public:
        parser.error("--from requires --public (code copying is only for public repos)")

    # --audit and --from are mutually exclusive
    if args.audit and args.from_repo:
        parser.error("--audit and --from are mutually exclusive")

    def info(msg):
        print(msg)

    def error(msg):
        print(f"{_c(_BOLD + _RED, 'Error:')} {msg}", file=sys.stderr)

    def warn(msg):
        print(f"{_c(_YELLOW, 'Warning:')} {msg}", file=sys.stderr)

    # Load config
    try:
        config = ConfigManager(config_path=args.config)
    except ConfigError as e:
        error(str(e))
        sys.exit(1)

    # Apply CLI overrides
    overrides = {}
    if args.no_wiki:
        overrides[("repo", "has_wiki")] = "false"
    if args.wiki:
        overrides[("repo", "has_wiki")] = "true"
    if args.public:
        overrides[("repo", "private")] = "false"
    if overrides:
        config.apply_overrides(overrides)

    # Determine visibility (used by branch protection and security plugins in create mode)
    is_public = not config.getbool("repo", "private", fallback=True)

    # Authenticate
    try:
        client = GitHubClient(debug=args.debug)
    except AuthError as e:
        error(str(e))
        sys.exit(1)

    # Detect owner
    try:
        owner = client.get_owner()
    except APIError as e:
        error(f"Could not determine GitHub user: {e}")
        sys.exit(1)

    # Detect plan level
    try:
        plan_name = client.get_plan_name()
    except APIError as e:
        warn(f"Could not detect GitHub plan: {e}. Assuming free.")
        plan_name = "free"

    is_paid_plan = plan_name not in ("free", "")

    repo_name = args.repo

    # ── Audit mode ────────────────────────────────────────────────────────────
    if args.audit:
        info(f"\nAuditing {_BOLD}{owner}/{repo_name}{_RESET}...")

        # Verify repo exists
        try:
            if not check_repo_exists(client, owner, repo_name):
                error(
                    f"Repository '{owner}/{repo_name}' does not exist. "
                    "Use without --audit to create it."
                )
                sys.exit(1)
        except APIError as e:
            error(f"Failed to check if repo exists: {e}")
            sys.exit(1)

        # Derive is_public and default branch from the actual repo, not from config/flags
        try:
            repo_data = client.get_json(client.repo_path(owner, repo_name))
            is_public = not repo_data.get("private", True)
            audit_default_branch = repo_data.get("default_branch")
        except APIError as e:
            error(f"Failed to fetch repository info: {e}")
            sys.exit(1)

        audit_branches = (
            [audit_default_branch] if audit_default_branch
            else _resolve_branches(config)
        )

        # Build plugins and fetch current state per plugin
        plugins = [
            RepositoryPlugin(client, owner, repo_name, config),
            ActionsPlugin(client, owner, repo_name, config),
            BranchProtectionPlugin(
                client, owner, repo_name, config,
                is_public=is_public, is_paid_plan=is_paid_plan,
                branches=audit_branches,
            ),
            SecurityPlugin(
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
            except APIError as e:
                error(f"Failed to fetch current state: {e}")
                sys.exit(1)

        # Print plan
        print_plan(full_plan)

        counts = full_plan.count_by_type()
        actionable_count = sum(v for k, v in counts.items() if k != ChangeType.SKIP)
        skipped = counts.get(ChangeType.SKIP, 0)
        info(_c(_DIM, f"{actionable_count} change(s) to apply, {skipped} skipped"))

        if args.dry_run:
            info(_c(_YELLOW, "\nDry run — no changes made."))
            sys.exit(0)

        # Check if there is anything to do
        actionable = full_plan.actionable_changes
        if not actionable:
            info(_c(_GREEN, "\nAlready at desired state — nothing to do."))
            sys.exit(0)

        # Prompt confirmation
        try:
            answer = input(
                f"\nApply {len(actionable)} change(s) to {owner}/{repo_name}? [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if answer not in ("y", "yes"):
            info(_c(_YELLOW, "Aborted."))
            sys.exit(0)

        # Apply settings (RepositoryPlugin.apply() skips POST automatically in audit mode)
        for plugin in plugins:
            try:
                plugin.apply(full_plan)
            except APIError as e:
                warn(f"Some settings failed to apply: {e}")

        print_success_audit(owner, repo_name)
        return

    # ── Create mode ───────────────────────────────────────────────────────────
    info(f"\nConfiguring {_BOLD}{owner}/{repo_name}{_RESET}...")

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

    # Validate source repo exists (--from workflow) and capture its default branch
    source_default_branch = None
    if args.from_repo and not args.dry_run:
        try:
            if not check_repo_exists(client, owner, args.from_repo):
                error(f"Source repo '{owner}/{args.from_repo}' does not exist.")
                sys.exit(1)
            source_default_branch = client.get_default_branch(owner, args.from_repo)
        except APIError as e:
            error(f"Failed to check source repo: {e}")
            sys.exit(1)

    # Pre-flight security scan (--from workflow, non-dry-run only)
    if args.from_repo and not args.dry_run:
        try:
            should_continue = run_preflight_scan(
                client, owner, args.from_repo, config, debug=args.debug
            )
        except APIError as e:
            error(f"Pre-flight scan failed (clone error): {e}")
            sys.exit(1)
        if not should_continue:
            info(_c(_YELLOW, "\nAborted by user."))
            sys.exit(0)

    # Resolve branches to protect (priority: source default > git HEAD > config > fallback)
    branches = _resolve_branches(config, source_default_branch=source_default_branch)

    # Run each plugin's plan()
    plugins = [
        RepositoryPlugin(client, owner, repo_name, config),
        ActionsPlugin(client, owner, repo_name, config),
        BranchProtectionPlugin(client, owner, repo_name, config, is_public=is_public, is_paid_plan=is_paid_plan, branches=branches),
        SecurityPlugin(client, owner, repo_name, config, is_public=is_public, is_paid_plan=is_paid_plan),
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
        full_plan.add(Change(
            type=ChangeType.ADD,
            category=ChangeCategory.SCAN,
            key="pre_flight_scan",
            new=f"Scan {owner}/{args.from_repo} locally for secrets, emails, large files, TODOs",
        ))
        full_plan.add(Change(
            type=ChangeType.ADD,
            category=ChangeCategory.FILE,
            key="code",
            new=f"Mirror all refs from {owner}/{args.from_repo}",
        ))

    # Print the plan
    print_plan(full_plan)

    counts = full_plan.count_by_type()
    actionable = sum(v for k, v in counts.items() if k != ChangeType.SKIP)
    skipped = counts.get(ChangeType.SKIP, 0)

    info(_c(_DIM, f"{actionable} change(s) to apply, {skipped} skipped"))

    if args.dry_run:
        info(_c(_YELLOW, "\nDry run — no changes made."))
        sys.exit(0)

    # Apply changes
    repo_plugin      = plugins[0]  # RepositoryPlugin
    actions_plugin   = plugins[1]  # ActionsPlugin
    bp_plugin        = plugins[2]  # BranchProtectionPlugin
    security_plugin  = plugins[3]  # SecurityPlugin

    # Apply repo creation + settings
    try:
        repo_plugin.apply(full_plan)
    except RepoExistsError as e:
        error(str(e))
        sys.exit(1)
    except APIError as e:
        error(f"Failed to create repository: {e}")
        sys.exit(1)

    # Refine branch list from POST response (priority 1 detection)
    post_default = repo_plugin.created_default_branch
    if post_default:
        bp_plugin.branches = [post_default]

    # Apply Actions settings
    try:
        actions_plugin.apply(full_plan)
    except APIError as e:
        warn(f"Repository created but Actions settings failed: {e}")

    # Apply branch protection (public repos or paid plan private repos)
    try:
        bp_plugin.apply(full_plan)
    except APIError as e:
        warn(f"Repository created but branch protection failed: {e}")

    # Apply security settings
    try:
        security_plugin.apply(full_plan)
    except APIError as e:
        warn(f"Security settings failed: {e}")

    # Mirror code from source repo (--from workflow)
    if args.from_repo:
        info(f"\nCopying code from {_BOLD}{owner}/{args.from_repo}{_RESET}...")
        try:
            client.copy_repo(owner, args.from_repo, repo_name)
            info(_c(_GREEN, f"  Code mirrored successfully."))
        except APIError as e:
            warn(f"Code copy failed: {e}")

    print_success(owner, repo_name)
