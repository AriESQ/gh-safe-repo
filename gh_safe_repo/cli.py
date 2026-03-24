"""
gh-safe-repo — Create GitHub repositories with safe defaults applied.

Usage:
    gh-safe-repo create owner/repo [--public] [--local PATH] [--from owner/repo] [--dry-run]
    gh-safe-repo fix owner/repo [--yes] [--dry-run]
    gh-safe-repo scan <path>
"""

import argparse
import sys

from .commands import create, fix, scan

# Temporary re-exports for test compatibility during migration
from .commands._common import _resolve_branches, format_plan_json  # noqa: F401


def main():
    parser = argparse.ArgumentParser(
        prog="gh-safe-repo",
        description="Create GitHub repositories with safe defaults applied.",
        epilog="""\
Choose a command:
  create    Create a new repo with safe defaults
  fix       Audit an existing repo and apply safe defaults
  scan      Scan a local directory for secrets

examples:
  gh-safe-repo create myuser/my-project                    Create a private repo
  gh-safe-repo create myuser/my-project --public           Create a public repo
  gh-safe-repo create myuser/my-project --local ./src      Push local code to a new repo
  gh-safe-repo create myuser/my-pub --from myuser/my-priv --public
                                                           Mirror a private repo to a new public one
  gh-safe-repo fix myuser/my-project                       Audit and fix settings on an existing repo
  gh-safe-repo fix myuser/my-project --dry-run             Preview changes without applying
  gh-safe-repo scan ./src                                  Scan a local directory for secrets
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

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
