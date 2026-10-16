"""
gh-safe-repo — Manage safe defaults for GitHub repositories.

Usage:
    gh-safe-repo create  <owner/repo>                 [--public] [--dry-run] [--yes] [--json]
    gh-safe-repo publish <path> <owner/repo>          [--public] [--dry-run] [--yes] [--json]
    gh-safe-repo mirror  <src-owner/repo> <owner/repo> [--public] [--dry-run] [--yes] [--json]
    gh-safe-repo audit   <owner/repo>                 [--json]
    gh-safe-repo fix     <owner/repo>                 [--dry-run] [--yes] [--json] [--migrate-branch-protection]
    gh-safe-repo scan    [path]                       [--json]
"""

import argparse
import sys

from .commands import audit, create, fix, mirror, publish, scan
from .commands._common import add_common_args


def _heading_style():
    """Return (color, reset) matching argparse's own section headings.

    Empty strings when color is unavailable (Python < 3.14) or disabled
    (NO_COLOR, non-tty), so raw description/epilog text stays plain in the
    same cases argparse itself drops color.
    """
    try:
        from _colorize import can_colorize, get_theme
    except ImportError:
        return "", ""
    if not can_colorize():
        return "", ""
    theme = get_theme(force_color=True).argparse
    return theme.heading, theme.reset


def _options_last(parser):
    """Reorder action groups so 'options' appears last in --help output."""
    # _action_groups is [positionals, optionals, ...extra]. Move optionals to end.
    groups = parser._action_groups
    for i, g in enumerate(groups):
        if g.title == "options":
            groups.append(groups.pop(i))
            break


def build_parser():
    """Build the top-level parser with its six subcommands."""
    h, r = _heading_style()
    parser = argparse.ArgumentParser(
        prog="gh-safe-repo",
        description=f"""\
Manage safe defaults for GitHub repositories.

{h}create a repo{r} (all take --public, --dry-run, --yes, --json):
  create   <owner/repo>                   Create an empty repo with safe defaults
  publish  <path> <owner/repo>            Push a local git repo into a new repo
  mirror   <src-owner/repo> <owner/repo>  Copy an existing repo into a new one (not a fork)

{h}existing repo:{r}
  audit    <owner/repo>                   Report settings drift; exit 3 if any, nothing changed
  fix      <owner/repo>                   Apply safe defaults (--dry-run, --yes, --json)

{h}local code:{r}
  scan     [path]                         Scan a directory for secrets (no GitHub calls)""",
        epilog=f"""\
{h}examples:{r}
  gh-safe-repo create alice/app --public
  gh-safe-repo publish . alice/app
  gh-safe-repo mirror upstream/lib alice/lib --public
  gh-safe-repo audit alice/app --json
  gh-safe-repo fix alice/app --yes
  gh-safe-repo scan
  gh-safe-repo --no-config create alice/app --dry-run
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="gh-safe-repo [--config PATH | --no-config] [--debug] <command> [options]",
    )
    # Global: also added to every subparser, so they work on either side of
    # the command (`--debug create ...` as well as `create ... --debug`).
    add_common_args(parser, json_flag=False, global_defaults=True)

    subparsers = parser.add_subparsers(dest="command", title="commands", metavar="")

    for cmd_module in (create, publish, mirror, audit, fix, scan):
        sub = subparsers.add_parser(
            cmd_module.NAME,
            help=cmd_module.HELP,
            description=cmd_module.HELP,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        cmd_module.add_arguments(sub)
        sub.set_defaults(func=cmd_module.run)
        # Move options to the end of help output (after positional args)
        _options_last(sub)

    # The grouped verb list in `description` replaces argparse's flat
    # "commands" section; drop that group from --help (parsing is unaffected).
    parser._action_groups = [g for g in parser._action_groups if g.title != "commands"]

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(2)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
