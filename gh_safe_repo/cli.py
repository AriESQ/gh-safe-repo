"""
gh-safe-repo — Manage safe defaults for GitHub repositories.

Usage:
    gh-safe-repo create <owner/repo> [--public] [--local PATH] [--from owner/repo] [--dry-run]
    gh-safe-repo fix <owner/repo> [--yes] [--dry-run]
    gh-safe-repo scan <path>
"""

import argparse
import sys

from .commands import create, fix, scan


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


def main():
    h, r = _heading_style()
    parser = argparse.ArgumentParser(
        prog="gh-safe-repo",
        description=f"""\
Manage safe defaults for GitHub repositories.

{h}usage:{r}
  gh-safe-repo create <owner/repo> [--public] [--local PATH | --from OWNER/REPO] [--dry-run]
  gh-safe-repo fix   <owner/repo>  [--yes] [--dry-run]
  gh-safe-repo scan  <path>

common options: --config PATH, --debug""",
        epilog=f"""\
{h}examples:{r}
  gh-safe-repo create <owner/repo>                    Create a private repo
  gh-safe-repo create <owner/repo> --public           Create a public repo
  gh-safe-repo create <owner/repo> --local ./src      Push local code to a new repo
  gh-safe-repo create <owner/repo> --from <owner/src>  Mirror code from an existing repo
  gh-safe-repo create <owner/pub> --from <owner/priv> --public
                                                      Mirror a private repo to a new public one
  gh-safe-repo fix <owner/repo>                       Audit and fix settings on an existing repo
  gh-safe-repo fix <owner/repo> --dry-run             Preview changes without applying
  gh-safe-repo scan ./src                             Scan a local directory for secrets
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="gh-safe-repo <command> [options]",
    )
    subparsers = parser.add_subparsers(dest="command", title="commands", metavar="")

    for cmd_module in (create, fix, scan):
        sub = subparsers.add_parser(
            cmd_module.NAME,
            help=cmd_module.HELP,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        cmd_module.add_arguments(sub)
        sub.set_defaults(func=cmd_module.run)
        # Move options to the end of help output (after positional args)
        _options_last(sub)

    # Move commands before options in top-level help
    _options_last(parser)

    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(2)

    args.func(args)
