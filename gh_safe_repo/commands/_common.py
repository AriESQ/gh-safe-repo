"""Shared helpers for CLI subcommands."""

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Optional

from ..config_manager import ConfigManager
from ..diff import Change, ChangeCategory, ChangeType, Plan
from ..errors import APIError, AuthError, ConfigError
from ..github_client import GitHubClient
from ..security_scanner import FindingCategory, SecurityScanner, Severity

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


@dataclass
class CLIContext:
    """Centralises auth + config + owner + plan detection for create/fix."""
    client: GitHubClient
    owner: str
    plan_name: str
    is_paid_plan: bool
    config: ConfigManager


def parse_repo_arg(arg):
    """Parse 'owner/repo' string. Returns (owner, repo). Exits on bad format."""
    if "/" not in arg:
        print(
            f"{_c(_BOLD + _RED, 'Error:')} Use owner/repo format "
            f"(e.g. myuser/{arg})",
            file=sys.stderr,
        )
        sys.exit(2)
    parts = arg.split("/", 1)
    owner, repo = parts[0], parts[1]
    if not owner or not repo:
        print(
            f"{_c(_BOLD + _RED, 'Error:')} Use owner/repo format "
            f"(e.g. myuser/my-repo)",
            file=sys.stderr,
        )
        sys.exit(2)
    return owner, repo


def build_context(args, expected_owner):
    """Authenticate, load config, validate owner, detect plan. Returns CLIContext."""
    # Load config
    try:
        config = ConfigManager(config_path=args.config)
    except ConfigError as e:
        error(str(e))
        sys.exit(1)

    # Authenticate
    try:
        client = GitHubClient(debug=args.debug)
    except AuthError as e:
        error(str(e))
        sys.exit(1)

    # Validate owner
    try:
        actual_owner = client.get_owner()
    except APIError as e:
        error(f"Could not determine GitHub user: {e}")
        sys.exit(1)

    if actual_owner != expected_owner:
        error(
            f"Owner '{expected_owner}' does not match authenticated user '{actual_owner}'"
        )
        sys.exit(1)

    # Detect plan level
    try:
        plan_name = client.get_plan_name()
    except APIError as e:
        warn(f"Could not detect GitHub plan: {e}. Assuming free.")
        plan_name = "free"

    is_paid_plan = plan_name not in ("free", "")

    return CLIContext(
        client=client,
        owner=actual_owner,
        plan_name=plan_name,
        is_paid_plan=is_paid_plan,
        config=config,
    )


def add_common_args(parser):
    """Add --debug, --config, --json to a subparser."""
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show every API call made",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Path to config file (default: ~/.config/gh-safe-repo/config.ini)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the plan as JSON instead of the ANSI table",
    )


def info(msg, *, json_mode=False):
    """Print info message; route to stderr in JSON mode."""
    if json_mode:
        print(msg, file=sys.stderr)
    else:
        print(msg)


def error(msg):
    print(f"{_c(_BOLD + _RED, 'Error:')} {msg}", file=sys.stderr)


def warn(msg):
    print(f"{_c(_YELLOW, 'Warning:')} {msg}", file=sys.stderr)


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


def format_plan_json(plan):
    """Serialise a Plan to a JSON string for --json output."""
    counts = plan.count_by_type()
    return json.dumps(
        {
            "changes": [
                {
                    "type": c.type.value,
                    "category": c.category.value,
                    "key": c.key,
                    "old": c.old,
                    "new": c.new,
                    "reason": c.reason,
                }
                for c in plan.changes
            ],
            "summary": {t.value: n for t, n in counts.items()},
        },
        indent=2,
    )


def print_success(owner, repo):
    url = f"https://github.com/{owner}/{repo}"
    https_url = f"https://github.com/{owner}/{repo}.git"
    ssh_url = f"git@github.com:{owner}/{repo}.git"
    inner = (
        f"  Repository created successfully!  \n"
        f"  {url}  \n"
        f"  \n"
        f"  HTTPS: git remote add origin {https_url}  \n"
        f"  SSH:   git remote add origin {ssh_url}  "
    )
    width = max(len(line) for line in inner.splitlines()) + 2
    top    = "╭─ Done " + "─" * (width - 7) + "╮"
    bottom = "╰" + "─" * (width + 1) + "╯"
    print(f"\n{_GREEN}{top}{_RESET}")
    for line in inner.splitlines():
        print(f"{_GREEN}│{_RESET} {line.ljust(width)} {_GREEN}│{_RESET}")
    print(f"{_GREEN}{bottom}{_RESET}\n")


def print_success_fix(owner, repo):
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
        if f.commit:
            detail = f"             commit {f.commit}"
            if f.timestamp:
                detail += f"  {f.timestamp}"
            print(_c(_DIM, detail))
        if f.match and f.match != "[redacted]":
            for match_line in f.match.splitlines():
                print(_c(_DIM, f"             {match_line}"))
    for f in warnings:
        loc = f.file_path + (f":{f.line_number}" if f.line_number else "")
        print(f"  {_c(_YELLOW, '[WARNING]')} {f.rule}")
        print(_c(_DIM, f"             in {loc}"))
        if f.commit:
            detail = f"             commit {f.commit}"
            if f.timestamp:
                detail += f"  {f.timestamp}"
            print(_c(_DIM, detail))
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


def _scan_findings_prompt(scanner, findings, config, warn_skipped_committed_dirs=False):
    """Display scan findings and prompt user. Returns True to continue, False to abort."""
    if warn_skipped_committed_dirs and scanner.skipped_committed_dirs:
        print(_c(_YELLOW, "  Warning: the following directories are committed to the repo"))
        print(_c(_YELLOW, "  and were not fully scanned (secrets/large files may be missed):"))
        for d in scanner.skipped_committed_dirs:
            print(_c(_DIM, f"    {d}/"))
        print()

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

    return answer in ("y", "yes") if has_criticals else answer not in ("n", "no")


def run_preflight_scan(client, owner, from_repo, config, debug=False, scanner=None):
    """
    Clone from_repo, scan locally, display findings, prompt user.
    Returns True to continue, False to abort. Raises APIError on clone failure.
    """
    if scanner is None:
        scanner = SecurityScanner(config, debug=debug)
    print(f"\n{_c(_BOLD, f'Running pre-flight security scan... ({scanner.scanner_description})')}")

    with tempfile.TemporaryDirectory() as tmpdir:
        scan_dir = os.path.join(tmpdir, "scan")
        client.clone_for_scan(owner, from_repo, scan_dir)
        findings = scanner.scan(scan_dir)

    return _scan_findings_prompt(scanner, findings, config, warn_skipped_committed_dirs=True)


def run_preflight_scan_local(scan_path, config, debug=False, scanner=None):
    """Scan a local path directly (no clone). Returns True to continue, False to abort."""
    if scanner is None:
        scanner = SecurityScanner(config, debug=debug)
    print(f"\n{_c(_BOLD, f'Running pre-flight security scan... ({scanner.scanner_description})')}")
    findings = scanner.scan(scan_path)
    return _scan_findings_prompt(scanner, findings, config, warn_skipped_committed_dirs=False)


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
