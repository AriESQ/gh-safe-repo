"""gh-safe-repo mirror — Copy an existing repo into a new GitHub repo."""

from ._common import add_common_args, add_create_args
from .create import run as _create_run

NAME = "mirror"
HELP = "Copy an existing repo into a new repo with safe defaults (not a fork)"


def add_arguments(parser):
    parser.description = (
        "Copy an existing repository into a new one. The source is cloned\n"
        "with full history, scanned for secrets, and mirrored into the new\n"
        "repo along with its default branch, description and topics.\n"
        "The result is a new repository, not a GitHub fork: it has no\n"
        "'forked from' link."
    )
    parser.add_argument(
        "source",
        metavar="SRC-OWNER/REPO",
        help="Repository to copy (any public repo, or one you can read)",
    )
    parser.add_argument(
        "repo",
        metavar="OWNER/REPO",
        help="New repository; OWNER must be the authenticated user",
    )
    add_create_args(parser)
    add_common_args(parser)
    parser.set_defaults(local_path=None)


def run(args):
    args.from_repo = args.source
    _create_run(args)
