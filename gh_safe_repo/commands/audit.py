"""gh-safe-repo audit — Report settings drift on an existing repo, read-only."""

from ._common import add_common_args
from .fix import add_all_arg
from .fix import run as _fix_run

NAME = "audit"
HELP = "Report settings drift on an existing repo without changing anything"


def add_arguments(parser):
    parser.description = (
        "Read every managed setting and show what `fix` would change.\n"
        "Makes no changes and never prompts. Exits 3 when anything differs\n"
        "from the safe defaults, 0 when the repo is compliant. Settings a\n"
        "person changed away from GitHub's default are reported as PRESERVE\n"
        "(not drift) unless --all is given."
    )
    parser.add_argument(
        "repo",
        metavar="OWNER/REPO",
        help="Existing repository (yours, an org's, or one you collaborate on); requires admin access",
    )
    add_all_arg(parser)
    add_common_args(parser)
    parser.set_defaults(dry_run=True, yes=False, migrate_branch_protection=False)


def run(args):
    _fix_run(args, audit=True)
