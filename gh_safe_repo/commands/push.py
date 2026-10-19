"""gh-safe-repo push — Create a repo and push a local git repo into it."""

from ._common import add_common_args, add_create_args
from .create import run as _create_run

NAME = "push"
HELP = "Create a repo and push a local git repo into it"


def add_arguments(parser):
    parser.description = (
        "Create a new repository and push a local git repository into it.\n"
        "The directory is scanned for secrets first; every local branch and\n"
        "tag is pushed, then origin and upstream tracking are set in the\n"
        "directory."
    )
    parser.add_argument(
        "path",
        metavar="PATH",
        help="Local git repository to push (run 'git init' first if needed)",
    )
    parser.add_argument(
        "repo",
        metavar="OWNER/REPO",
        help="New repository; OWNER must be the authenticated user",
    )
    add_create_args(parser)
    add_common_args(parser)
    parser.set_defaults(from_repo=None)


def run(args):
    args.local_path = args.path
    _create_run(args)
