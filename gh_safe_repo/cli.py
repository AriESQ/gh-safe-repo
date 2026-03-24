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


def main():
    parser = argparse.ArgumentParser(
        prog="gh-safe-repo",
        description="Manage safe defaults for GitHub repositories.",
        epilog="""\
examples:
  gh-safe-repo create <owner/repo>                    Create a private repo
  gh-safe-repo create <owner/repo> --public           Create a public repo
  gh-safe-repo create <owner/repo> --local ./src      Push local code to a new repo
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

    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(2)

    args.func(args)
