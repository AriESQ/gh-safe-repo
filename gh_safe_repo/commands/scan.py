"""gh-safe-repo scan — Local secret scanning only."""

import os
import sys

from ..config_manager import ConfigManager
from ..errors import ConfigError
from ..security_scanner import SecurityScanner
from ._common import (
    _BOLD,
    _DIM,
    _GREEN,
    _YELLOW,
    _c,
    _print_findings,
    error,
)

NAME = "scan"
HELP = "Scan a local directory for secrets (no GitHub interaction)"


def add_arguments(parser):
    parser.add_argument(
        "path",
        help="Local directory path to scan",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show scanner details",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Path to config file (default: ./gh-safe-repo.ini or ~/.config/gh-safe-repo/gh-safe-repo.ini)",
    )


def run(args):
    scan_path = os.path.abspath(args.path)
    if not os.path.isdir(scan_path):
        error(f"'{args.path}' is not a directory")
        sys.exit(2)

    try:
        config = ConfigManager(config_path=args.config, require_exists=args.config is not None)
    except ConfigError as e:
        error(str(e))
        sys.exit(1)

    scanner = SecurityScanner(config, debug=args.debug)
    print(f"\n{_c(_BOLD, 'Scanning')} {scan_path}...")
    findings = scanner.scan(scan_path)

    if scanner.skipped_committed_dirs:
        print(_c(_YELLOW, "Warning: the following directories were skipped during scan:"))
        for d in scanner.skipped_committed_dirs:
            print(_c(_DIM, f"  {d}/"))
        print()

    has_criticals = _print_findings(findings, config)
    sys.exit(1 if has_criticals else 0)
