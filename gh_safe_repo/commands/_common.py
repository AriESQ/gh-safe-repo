"""Shared helpers for CLI subcommands."""

import argparse
import contextlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from typing import Optional

from ..config_manager import ConfigManager
from ..diff import Change, ChangeCategory, ChangeType, Plan
from ..errors import APIError, AuthError, ConfigError
from ..github_client import GitHubClient
from ..security_scanner import FindingCategory, SecurityScanner, Severity, _view_hint

# ANSI escape codes. Blanked out when the output is not a terminal (piped into
# a file, a script, or an AI agent) or when NO_COLOR is set, so consumers get
# clean text instead of escape sequences.
_COLOR = not os.environ.get("NO_COLOR") and sys.stdout.isatty()

_RESET  = "\033[0m"  if _COLOR else ""
_BOLD   = "\033[1m"  if _COLOR else ""
_DIM    = "\033[2m"  if _COLOR else ""
_RED    = "\033[31m" if _COLOR else ""
_GREEN  = "\033[32m" if _COLOR else ""
_YELLOW = "\033[33m" if _COLOR else ""
_CYAN   = "\033[36m" if _COLOR else ""

# Marker for an UPDATE whose current value is a weaker security posture than
# GitHub's default (someone relaxed a control; the three-way merge in fix).
_WEAKENED = "⚠ weakened from default"


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


def build_context(args, expected_owner, require_owner_match=True):
    """Authenticate, load config, validate owner, detect plan. Returns CLIContext.

    When require_owner_match is True (default, used by create), the repo owner
    must match the authenticated user — a UX guard for multi-account systems.
    When False (used by fix), the check is skipped; callers should verify
    permissions separately (e.g. admin access on the target repo).
    """
    # Load config
    try:
        config_path = args.config or None  # "" (from --no-config) → defaults only
        config = ConfigManager(config_path=config_path, require_exists=config_path is not None)
    except ConfigError as e:
        error(str(e))
        sys.exit(1)

    if args.debug:
        print(f"[debug] config: {config.config_source}", file=sys.stderr)

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

    if require_owner_match and actual_owner.lower() != expected_owner.lower():
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


def add_common_args(parser, *, json_flag=True, json_help=None, global_defaults=False):
    """Add --debug, --config PATH and --no-config (and --json) to a parser.

    The first three are global: `cli.main` also adds them to the top-level
    parser so they work on either side of the command (`--debug create ...`
    and `create ... --debug`). On the subparsers they carry `default=SUPPRESS`
    so an unused subparser default cannot clobber a value already parsed
    before the command; the top-level copies (`global_defaults=True`) supply
    the fallback values. --json stays command-local and keeps a real default.
    """
    default = {} if global_defaults else {"default": argparse.SUPPRESS}
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show every API call made",
        **default,
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Config file to use (default: discovered from XDG dirs or the current directory)",
        **default,
    )
    parser.add_argument(
        "--no-config",
        action="store_true",
        help="Ignore config files; use built-in defaults only",
        **default,
    )
    if json_flag:
        parser.add_argument(
            "--json",
            action="store_true",
            help=json_help or "Emit the plan as JSON instead of the ANSI table",
        )


def add_mutation_args(parser):
    """--dry-run and --yes, identical on every verb that changes GitHub."""
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and make no API calls",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the confirmation prompt (required when stdin is not a terminal)",
    )


def add_create_args(parser):
    """--public plus the mutation args, identical on every verb that creates a repo."""
    parser.add_argument(
        "--public",
        action="store_true",
        help="Create a public repository instead of private",
    )
    add_mutation_args(parser)


def resolve_config_arg(args):
    """Fold --no-config into the `args.config == ""` sentinel.

    `None` means "discover a config file", `""` means "built-in defaults
    only", anything else is an explicit path. Both flags together is a usage
    error (exit 2).
    """
    if args.no_config:
        if args.config:
            error("--config and --no-config are mutually exclusive")
            sys.exit(2)
        args.config = ""
    return args.config


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
    """Print the plan table.

    PRESERVE rows (fix/audit three-way merge) show the value being kept and
    why: ``true (changed from default)`` or ``3 (stricter than 1)``. An UPDATE
    whose ``weakened`` flag is set carries the ⚠ marker after the arrow.
    """
    headers = ["Type", "Category", "Setting", "Value / Note"]

    rows = []
    for change in plan.changes:
        if change.type == ChangeType.SKIP:
            rows.append(("SKIP", change.category.value, change.key, change.reason, "skip"))
        elif change.type == ChangeType.ADD:
            rows.append(("ADD", change.category.value, change.key, str(change.new), "add"))
        elif change.type == ChangeType.UPDATE:
            value = f"{change.old!r} → {change.new!r}"
            if change.weakened:
                value += f"  {_c(_YELLOW, _WEAKENED)}"
            rows.append(("UPDATE", change.category.value, change.key, value, "update"))
        elif change.type == ChangeType.DELETE:
            rows.append(("DELETE", change.category.value, change.key, str(change.old), "delete"))
        elif change.type == ChangeType.PRESERVE:
            value = f"{change.old!r} ({change.reason})" if change.reason else repr(change.old)
            rows.append(("PRESERVE", change.category.value, change.key, value, "preserve"))

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
        elif kind == "preserve":
            print(
                _c(_CYAN, type_str.ljust(col_widths[0])) + sep
                + cat.ljust(col_widths[1]) + sep
                + key.ljust(col_widths[2]) + sep
                + value
            )
    print()


def format_plan_json(plan, verification=None):
    """Serialise a Plan to a JSON string for --json output.

    With `verification` (a verify.Verification), the document gains a
    top-level "verification" block. Non-dry-run create/fix print this once,
    at the end of the run; --dry-run never passes it.

    Every change carries the merge context: ``tier`` ("security" /
    "preference" / null), ``github_default`` (what a fresh repo holds, null
    when unknown) and ``weakened`` (true on an UPDATE that restores a
    security control someone relaxed). ``summary`` gains ``"preserve"`` when
    the plan has PRESERVE rows.
    """
    counts = plan.count_by_type()
    doc = {
        "changes": [
            {
                "type": c.type.value,
                "category": c.category.value,
                "key": c.key,
                "old": c.old,
                "new": c.new,
                "reason": c.reason,
                "tier": c.tier,
                "github_default": c.github_default,
                "weakened": bool(c.weakened),
            }
            for c in plan.changes
        ],
        "summary": {t.value: n for t, n in counts.items()},
    }
    if verification is not None:
        doc["verification"] = verification.to_dict()
    return json.dumps(doc, indent=2)


def prose_stream(json_mode):
    """Where human-readable output goes: stderr under --json, else stdout.

    Under --json, stdout is reserved for the one JSON document.
    """
    return sys.stderr if json_mode else sys.stdout


def prose_to_stderr(json_mode):
    """Context manager: route print()/input() prompts to stderr under --json.

    For shared helpers that print to stdout unconditionally (the plan table,
    the pre-flight scan report). A no-op outside --json.
    """
    if json_mode:
        return contextlib.redirect_stdout(sys.stderr)
    return contextlib.nullcontext()


def ask(prompt, *, json_mode=False):
    """input() whose prompt goes to stderr under --json (stdout stays JSON)."""
    with prose_to_stderr(json_mode):
        return input(prompt)


def confirm(prompt, *, json_mode=False, nothing_done="Nothing was changed."):
    """Ask a [y/N] confirmation. Returns True only for an explicit yes.

    An answer read from a pipe (`echo y | gh-safe-repo fix ...`) counts like a
    typed one. EOF — stdin closed, `< /dev/null`, an agent or CI subprocess
    with no input — is not an answer: it is an unattended run that forgot
    --yes, so this prints an error and exits 1 rather than letting the caller
    exit 0 with nothing done (#93; the same split `ScanDecision.blocked`
    makes). Ctrl-C at the prompt is a decline (False), as before.
    """
    try:
        answer = ask(prompt, json_mode=json_mode)
    except EOFError:
        print(file=prose_stream(json_mode))
        error(
            "Could not read an answer to the confirmation prompt (stdin closed); "
            f"--yes is required for unattended runs. {nothing_done}"
        )
        sys.exit(1)
    except KeyboardInterrupt:
        print(file=prose_stream(json_mode))
        return False
    return answer.strip().lower() in ("y", "yes")


_MARKS = {"ok": "✓", "mismatch": "✗", "unknown": "?"}


def _fmt_value(v):
    return "(unreadable)" if v is None else repr(v)


def print_verification(verification, *, file=None):
    """Print the human "Verified" section: one ✓/✗/? row per check."""
    out = file or sys.stdout
    if not verification.checks:
        return
    colours = {"ok": _GREEN, "mismatch": _RED, "unknown": _YELLOW}
    print(f"\n{_BOLD}Verified{_RESET}", file=out)
    for c in verification.checks:
        # Advisory mismatches are warnings (exit code unchanged): yellow.
        colour = _YELLOW if c.advisory else colours[c.status]
        mark = _c(colour, _MARKS[c.status])
        if c.status == "ok":
            print(f"  {mark} {c.name}", file=out)
        elif c.status == "mismatch":
            local = c.category == "local"
            expected, actual = c.expected, c.actual
            if isinstance(expected, dict) and isinstance(actual, dict):
                # Ref maps can hold hundreds of branches: show only what differs.
                diff = [k for k in expected if expected.get(k) != actual.get(k)]
                expected = {k: expected[k] for k in diff}
                actual = {k: actual.get(k) for k in diff}
            actual = ("(not set)" if local and actual is None
                      else _fmt_value(actual))
            print(
                f"  {mark} {c.name}: expected {_fmt_value(expected)}, "
                f"{'local repo has' if local else 'GitHub has'} {actual}",
                file=out,
            )
        else:
            print(f"  {mark} {c.name}: could not read it back", file=out)
    # Remediation, de-duplicated and in check order.
    seen = []
    for c in verification.checks:
        if c.status != "ok" and c.remediation and c.remediation not in seen:
            seen.append(c.remediation)
    for r in seen:
        print(_c(_DIM, f"  → {r}"), file=out)
    s = verification.summary()
    print(
        _c(_DIM, f"{s['ok']} ok, {s['mismatch']} mismatch, {s['unknown']} unknown"),
        file=out,
    )


def _print_banner(title, inner, colour, file=None):
    out = file or sys.stdout
    width = max(len(line) for line in inner.splitlines()) + 2
    top    = f"╭─ {title} " + "─" * (width - len(title) - 3) + "╮"
    bottom = "╰" + "─" * (width + 1) + "╯"
    print(f"\n{colour}{top}{_RESET}", file=out)
    for line in inner.splitlines():
        print(f"{colour}│{_RESET} {line.ljust(width)} {colour}│{_RESET}", file=out)
    print(f"{colour}{bottom}{_RESET}\n", file=out)


def format_findings_json(findings, skipped_committed_dirs=(), engine=None):
    """Serialise scan findings to a JSON string for `scan --json`.

    Severities are lower-cased so they match the casing of the change types in
    `format_plan_json`; `summary` always carries all three keys. `engine` is
    `SecurityScanner.engine_report()` and is emitted as the `engine` key when
    given.
    """
    counts = {"critical": 0, "warning": 0, "info": 0}
    for f in findings:
        counts[f.severity.value.lower()] += 1
    doc = {
        "findings": [
            {
                "severity": f.severity.value.lower(),
                "category": f.category.value,
                "file_path": f.file_path,
                "line_number": f.line_number,
                "rule": f.rule,
                "match": f.match,
                "commit": f.commit,
                "timestamp": f.timestamp,
            }
            for f in findings
        ],
        "summary": counts,
        "skipped_committed_dirs": list(skipped_committed_dirs),
    }
    if engine is not None:
        doc["engine"] = engine
    return json.dumps(doc, indent=2)


def _problem_lines(verification):
    """Banner lines naming what verification could not confirm ([] if ok)."""
    if verification is None or verification.ok:
        return []
    s = verification.summary()
    # Advisory mismatches (local wiring) are not settings and do not change
    # the exit code; the banner's next-step lines cover them.
    mismatched = s["mismatch"] - len(verification.advisories)
    lines = []
    if mismatched:
        lines.append(f"{mismatched} setting(s) did not stick.")
    if s["unknown"]:
        lines.append(f"{s['unknown']} setting(s) could not be read back.")
    return lines


def _local_next_step(verification, wiring):
    """Banner lines for the user's local repo after `push`.

    Built from the `local/origin` and `local/tracking` verification checks
    (what git config actually says), falling back to `wiring` (what
    push_local reported) when there are none. None when neither applies.
    """
    checks = {}
    if verification is not None:
        checks = {c.key: c for c in verification.checks if c.category == "local"}
    origin, tracking = checks.get("origin"), checks.get("tracking")
    branch = wiring.branch if wiring else None
    if tracking is not None:
        branch = str(tracking.expected["merge"]).split("refs/heads/", 1)[-1]
    # Explicit <b>:<b> refspec: ignores tracking config, so it can never land
    # on the default branch by accident.
    push = [f"git push -u origin {branch}:{branch}"] if branch else []
    if origin is not None and origin.status == "mismatch":
        if origin.actual:
            return ["Your repo's origin points elsewhere. To use this repo:",
                    f"git remote set-url origin {origin.expected}"] + push
        return ["Wire up your local repo:",
                f"git remote add origin {origin.expected}"] + push
    if tracking is not None and tracking.status == "ok":
        return ["Local repo wired up:", f"{branch} tracks origin/{branch}"]
    if tracking is not None and tracking.status == "mismatch":
        return ["Set your tracking branch:"] + push
    if origin is not None and origin.status == "ok" and tracking is None:
        return ["Local repo wired up:", f"origin is {origin.expected}"]
    if wiring and wiring.tracking:
        return ["Local repo wired up:", f"{branch} tracks origin/{branch}"]
    if branch:
        return ["Set your tracking branch:"] + push
    return None


def print_success(owner, repo, local_push=False, protocol="https", wiring=None,
                  file=None, verification=None):
    """Closing banner for `create`.

    With `verification`: a failed fatal check (repo missing, wrong
    visibility) prints a red "Failed" banner naming it and nothing else; any
    other mismatch or unknown turns the green "Done" into a yellow "Done with
    problems" whose first lines say how many settings did not stick.
    Advisory checks (local wiring) leave the banner green. Under
    `local_push` the next-step lines come from the `local/*` checks (what
    the user's git config actually says), falling back to `wiring`.
    """
    url = f"https://github.com/{owner}/{repo}"
    if verification is not None and verification.has_fatal:
        failed = [c for c in verification.checks if c.fatal and c.status != "ok"]
        rest = type(verification)([c for c in verification.checks if c not in failed])
        lines = ["Repository created, but failed verification:"]
        lines += [f"  {_MARKS['mismatch']} {c.name}" for c in failed]
        lines += _problem_lines(rest) + [url]
        _print_banner("Failed", "\n".join(f"  {l}  " for l in lines), _RED, file=file)
        return
    problems = _problem_lines(verification)
    headline = (
        "".join(f"  {l}  \n" for l in ["Repository created."] + problems)
        if problems else "  Repository created successfully!  \n"
    )
    https_url = f"https://github.com/{owner}/{repo}.git"
    ssh_url = f"git@github.com:{owner}/{repo}.git"
    # Order remote suggestions by the user's preferred git protocol (#19),
    # so the line they're most likely to use is listed first.
    https = ("HTTPS", https_url)
    ssh = ("SSH  ", ssh_url)
    ordered = (ssh, https) if protocol == "ssh" else (https, ssh)
    if local_push:
        steps = _local_next_step(verification, wiring) or [
            "Clone it:", f"git clone {ordered[0][1]}",
        ]
        next_step = "\n".join(f"  {l}  " for l in steps)
        inner = (
            headline
            + f"  {url}  \n"
            f"  \n"
            + next_step
        )
    else:
        inner = (
            headline
            + f"  {url}  \n"
            f"  \n"
            f"  Add remote to existing repo:  \n"
            f"  {ordered[0][0]}: git remote add origin {ordered[0][1]}  \n"
            f"  {ordered[1][0]}: git remote add origin {ordered[1][1]}  \n"
            f"  \n"
            f"  Clone fresh:  \n"
            f"  {ordered[0][0]}: git clone {ordered[0][1]}  \n"
            f"  {ordered[1][0]}: git clone {ordered[1][1]}  "
        )
    if problems:
        _print_banner("Done with problems", inner, _YELLOW, file=file)
    else:
        _print_banner("Done", inner, _GREEN, file=file)


def print_success_fix(owner, repo, verification=None, file=None):
    """Closing banner for `fix`, built from the verification result.

    Green "Done" only when every check is ok; otherwise yellow
    "Done with problems" naming how many settings did not stick or could not
    be read back.
    """
    url = f"https://github.com/{owner}/{repo}"
    if verification is None or verification.ok:
        status = "Repository updated and verified."
        if verification is None:
            status = "Repository updated."
        inner = f"  {status}  \n  {url}  "
        _print_banner("Done", inner, _GREEN, file=file)
        return
    lines = _problem_lines(verification)
    inner = "\n".join(f"  {l}  " for l in lines + [url])
    _print_banner("Done with problems", inner, _YELLOW, file=file)


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
        hint = _view_hint(f)
        if hint:
            print(_c(_DIM, f"             View with: {hint}"))
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
        hint = _view_hint(f)
        if hint:
            print(_c(_DIM, f"             View with: {hint}"))
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
        import shlex
        print(_c(_BOLD, "Banned strings found. To scrub from git history, run in your source repo:"))
        replacements = "\n".join(f"literal:{s}==>***REMOVED***" for s in banned_strings)
        print(_c(_DIM, f"  git filter-repo --replace-text <(printf %s {shlex.quote(replacements)})"))
        print()

    return bool(criticals)


@dataclass
class ScanDecision:
    """Outcome of showing pre-flight scan findings to the user.

    `blocked` distinguishes "we could not ask, or must not proceed unattended"
    from "the user looked at the findings and said no". Callers exit 1 on the
    former and 0 on the latter — an unattended run that silently exits 0 without
    creating the repo is indistinguishable from success to a script or an agent.
    """
    proceed: bool
    blocked: bool = False
    reason: str = ""


def _scan_findings_prompt(scanner, findings, config, warn_skipped_committed_dirs=False, yes=False):
    """Display scan findings and prompt the user. Returns a ScanDecision.

    With `yes` (or no TTY to prompt on), no prompt is issued: warnings and info
    findings pass through, criticals block.
    """
    if warn_skipped_committed_dirs and scanner.skipped_committed_dirs:
        print(_c(_YELLOW, "  Warning: the following directories are committed to the repo"))
        print(_c(_YELLOW, "  and were not fully scanned (secrets/large files may be missed):"))
        for d in scanner.skipped_committed_dirs:
            print(_c(_DIM, f"    {d}/"))
        print()

    has_criticals = _print_findings(findings, config)

    if not findings:
        return ScanDecision(proceed=True)

    interactive = sys.stdin.isatty()

    if yes or not interactive:
        if has_criticals:
            why = (
                "critical findings and --yes was given"
                if yes
                else "critical findings and there is no terminal to confirm on"
            )
            return ScanDecision(
                proceed=False,
                blocked=True,
                reason=f"Pre-flight scan found {why}",
            )
        if yes:
            # Warnings only: matches the [Y/n] default of the interactive prompt.
            return ScanDecision(proceed=True)
        return ScanDecision(
            proceed=False,
            blocked=True,
            reason=(
                "Pre-flight scan found issues and there is no terminal to "
                "confirm on; re-run with --yes to accept warnings"
            ),
        )

    if has_criticals:
        prompt = _c(_BOLD + _RED, "Critical issues found. Continue anyway? [y/N]: ")
    else:
        prompt = _c(_YELLOW, "Warnings found. Continue? [Y/n]: ")

    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return ScanDecision(proceed=False)

    proceed = answer in ("y", "yes") if has_criticals else answer not in ("n", "no")
    return ScanDecision(proceed=proceed)


def _engine_decision(scanner):
    """Report the engine scan() used; block if an explicit mode fell back.

    Returns a blocked ScanDecision when trufflehog_mode = native/docker could
    not run truffleHog (the run must exit 1 before anything is created), else
    None. An `auto` fallback is reported but never blocks.
    """
    print(_c(_DIM, f"  Scan engine: {scanner.engine_summary}"))
    msg = scanner.engine_error()
    if msg:
        return ScanDecision(proceed=False, blocked=True, reason=msg)
    return None


def run_preflight_scan(client, owner, from_repo, config, debug=False, scanner=None, yes=False):
    """
    Clone from_repo, scan locally, display findings, prompt user.
    Returns a ScanDecision. Raises APIError on clone failure.
    """
    if scanner is None:
        scanner = SecurityScanner(config, debug=debug)
    print(f"\n{_c(_BOLD, f'Running pre-flight security scan... ({scanner.scanner_description})')}")

    with tempfile.TemporaryDirectory() as tmpdir:
        scan_dir = os.path.join(tmpdir, "scan")
        client.clone_for_scan(owner, from_repo, scan_dir)
        findings = scanner.scan(scan_dir)

    blocked = _engine_decision(scanner)
    if blocked:
        return blocked
    return _scan_findings_prompt(
        scanner, findings, config, warn_skipped_committed_dirs=True, yes=yes
    )


def run_preflight_scan_local(scan_path, config, debug=False, scanner=None, yes=False):
    """Scan a local path directly (no clone). Returns a ScanDecision."""
    if scanner is None:
        scanner = SecurityScanner(config, debug=debug)
    print(f"\n{_c(_BOLD, f'Running pre-flight security scan... ({scanner.scanner_description})')}")
    findings = scanner.scan(scan_path)
    blocked = _engine_decision(scanner)
    if blocked:
        return blocked
    return _scan_findings_prompt(
        scanner, findings, config, warn_skipped_committed_dirs=False, yes=yes
    )


def check_repo_exists(client, owner, repo):
    """Return True if the repo already exists."""
    path = client.repo_path(owner, repo)
    status, _ = client.call_api("GET", path)
    return status == 200


def _resolve_branches(config, post_default_branch=None, source_default_branch=None) -> list:
    """
    Determine the list of branches to protect, in priority order:
      1. POST /user/repos response default_branch (new repo, non-dry-run)
      2. GET /repos/{owner}/{source} default_branch (`copy`, non-dry-run)
      3. protected_branch from config (may be "master, main" from SAFE_DEFAULTS)
    """
    if post_default_branch:
        return [post_default_branch]
    if source_default_branch:
        return [source_default_branch]
    raw = config.get("branch_protection", "protected_branch", fallback="master, main")
    return [b.strip() for b in raw.split(",") if b.strip()]
